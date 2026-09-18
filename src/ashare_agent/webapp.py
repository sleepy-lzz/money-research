"""Loopback-only web workbench with fixed CLI actions and isolated run directories."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .current_data import parse_symbols

ROOT = Path(__file__).resolve().parents[2]
BACKEND_VERSION = 9


def _pid_exists(pid):
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        ERROR_INVALID_PARAMETER = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            exit_code = ctypes.c_uint32()
            try:
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        error = ctypes.get_last_error()
        if error == ERROR_ACCESS_DENIED:
            return True
        if error == ERROR_INVALID_PARAMETER:
            return False
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_creation_id(pid):
    """Read-only process identity token used to recover interrupted jobs."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        creation = FileTime()
        exit_time = FileTime()
        kernel_time = FileTime()
        user_time = FileTime()
        try:
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            value = (creation.high << 32) | creation.low
            return f"windows:{value}"
        finally:
            kernel32.CloseHandle(handle)

    stat = Path(f"/proc/{pid}/stat")
    try:
        raw = stat.read_text(encoding="utf-8")
        after_name = raw.rfind(")") + 2
        fields = raw[after_name:].split()
        return f"linux:{fields[19]}"
    except (IndexError, OSError, UnicodeDecodeError):
        return None


def _recovery_state(job):
    """Return active, exited, or unknown without touching the target process."""
    pid = job.get("pid")
    creation_id = job.get("process_created_at")
    if not isinstance(pid, int) or not creation_id:
        return "unknown"
    current_id = _process_creation_id(pid)
    if current_id is None:
        return "unknown" if _pid_exists(pid) else "exited"
    return "active" if current_id == creation_id else "exited"


def command_plan(payload, output):
    if not isinstance(payload, dict):
        raise ValueError("请求格式错误")
    if payload.get("kind") in {"paper-check", "paper-advance"}:
        if set(payload) != {"kind", "account_id", "snapshot_id"}:
            raise ValueError("模拟账户请求字段不完整或包含不支持字段")
        for field in ("account_id", "snapshot_id"):
            value = payload[field]
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
                raise ValueError("账户或数据版本标识无效")
        return [["paper-account", "check" if payload["kind"] == "paper-check" else "advance",
                 "--id", payload["account_id"], "--snapshot-id", payload["snapshot_id"], "--output", str(output)]]
    allowed = {"kind", "network", "symbols", "top", "min_amount"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError("请求包含不支持字段：" + ", ".join(sorted(unknown)))
    kind = payload.get("kind")
    if kind not in {"screen", "doctor", "demo", "planner"}:
        raise ValueError("不支持的操作")
    if kind == "planner":
        return [["planner-review", "--output", str(output)]]
    network = payload.get("network", "direct")
    if network not in {"direct", "environment"}:
        raise ValueError("网络模式无效")
    if kind == "demo":
        return [
            ["demo-data", "--sessions", "320"],
            [
                "backtest",
                "--snapshot",
                "runtime/snapshots/demo-v2-320-42",
                "--start",
                "2025-09-25",
                "--end",
                "2025-12-31",
                "--output",
                str(output),
            ],
        ]
    if kind == "doctor":
        return [["current-doctor", "--network", network, "--output", str(output)]]
    symbols = payload.get("symbols", "")
    if not isinstance(symbols, str):
        raise ValueError("股票列表必须为文本")
    parse_symbols(symbols)
    top = payload.get("top", 20)
    amount = payload.get("min_amount", 100_000_000)
    if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= 100:
        raise ValueError("候选数量需为 1–100 的整数")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, (int, float))
        or not math.isfinite(amount)
        or not 0 <= amount <= 1e12
    ):
        raise ValueError("成交额门槛无效")
    return [
        [
            "current-screen",
            "--network",
            network,
            "--symbols",
            symbols,
            "--top",
            str(top),
            "--min-amount",
            str(amount),
            "--output",
            str(output),
        ]
    ]


class _WindowsJob:
    """Own child processes as a Windows Job Object with kill-on-close."""

    _KILL_ON_JOB_CLOSE = 0x2000
    _EXTENDED_LIMIT_INFO = 9

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes

        self.ctypes = ctypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_bool
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_bool
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        self.kernel32 = kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            self._EXTENDED_LIMIT_INFO,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            self.handle = None
            raise error

    def assign(self, process):
        if self.handle is None:
            return
        try:
            process_handle = self.ctypes.c_void_p(int(process._handle))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("无法确认子进程句柄，拒绝启动未受控任务") from exc
        if not self.kernel32.AssignProcessToJobObject(self.handle, process_handle):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def terminate(self):
        if self.handle is not None and not self.kernel32.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self):
        if self.handle is not None:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


class Jobs:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.folder = self.root / "runtime/web-runs"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = None
        self.process = None
        self.process_job = None
        self.worker_thread = None
        self.cancelled = set()
        self.closed = False
        self.recovery_blocked = False
        self.process_owner = _WindowsJob()
        for path in sorted(self.folder.glob("*/job.json"))[-100:]:
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job.get("id") != path.parent.name:
                    continue
                recovery = None
                if job.get("status") == "running" or job.get("recovery_required"):
                    recovery = _recovery_state(job)
                if job["status"] == "running":
                    if recovery == "exited":
                        job.update(
                            status="failed",
                            error="工作台已重启，上次任务进程已退出，完成状态未确认",
                            recovery_required=False,
                        )
                    else:
                        job.update(
                            status="failed",
                            error=(
                                "工作台已重启，确认上次任务进程仍活跃；为安全起见禁止继续运行新任务"
                                if recovery == "active"
                                else "工作台已重启，未确认上次任务的进程状态；为安全起见禁止继续运行新任务"
                            ),
                            recovery_required=True,
                        )
                        self.recovery_blocked = True
                    path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
                elif job.get("recovery_required"):
                    if recovery == "exited":
                        job.update(
                            recovery_required=False,
                            error="已确认遗留进程退出，恢复门禁已解除；此前结果仍未确认",
                        )
                        path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
                    else:
                        self.recovery_blocked = True
                self.jobs[job["id"]] = job
            except (ValueError, KeyError, OSError):
                continue

    def persist(self, job):
        path = self.folder / job["id"] / "job.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def snapshot(self, ident):
        with self.lock:
            if ident not in self.jobs:
                raise KeyError("没有该任务")
            job = dict(self.jobs[ident])
            output = self.folder / ident
            completed = job.get("status") == "completed"
            job["report_url"] = (
                f"/reports/{ident}/report.html" if completed and (output / "report.html").is_file() else None
            )
            job["result"] = None
            if not completed:
                return job
            for name in ("result.json", "cli-result.json"):
                file = output / name
                if file.is_file():
                    try:
                        value = json.loads(file.read_text(encoding="utf-8"))
                        value.pop("receipts", None)  # full receipts stay in the downloadable result
                        job["result"] = value
                        break
                    except (ValueError, OSError):
                        pass
            return job

    def start(self, payload):
        ident = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
        output = self.folder / ident
        commands = command_plan(payload, output)
        with self.lock:
            if self.closed:
                raise RuntimeError("工作台正在关闭，不能启动新任务")
            if self.recovery_blocked:
                raise RuntimeError(
                    "检测到上次未确认进程状态的运行记录，已安全阻止新任务；请先人工核查遗留进程"
                )
            if self.active:
                raise RuntimeError("已有任务运行中，请等待或停止当前任务")
            output.mkdir()
            job = dict(
                id=ident,
                kind=payload["kind"],
                status="running",
                logs="任务已启动\n",
                error=None,
                created_at=datetime.now().astimezone().isoformat(),
                request=payload,
                pid=None,
                process_created_at=None,
            )
            self.jobs[ident] = job
            self.active = ident
            self.persist(job)
            self.worker_thread = threading.Thread(target=self.worker, args=(ident, commands), daemon=True)
            thread = self.worker_thread
        thread.start()
        return self.snapshot(ident)

    def append(self, ident, text):
        with self.lock:
            self.jobs[ident]["logs"] = (self.jobs[ident]["logs"] + text)[-30000:]

    def _clear_process(self, ident, process=None):
        with self.lock:
            if self.process_job != ident or (process is not None and self.process is not process):
                return
            self.process = None
            self.process_job = None
            if ident in self.jobs:
                self.jobs[ident]["pid"] = None
                self.jobs[ident]["process_created_at"] = None
                self.persist(self.jobs[ident])

    def _terminate_owned_process(self, ident):
        with self.lock:
            if self.process_job not in {None, ident}:
                return
            process = self.process
            if process is not None and process.poll() is None:
                process.terminate()
            self.process_owner.terminate()

    def worker(self, ident, commands):
        output = self.folder / ident
        outcome, error = "completed", None
        python = Path(sys.executable).with_name("python.exe") if os.name == "nt" else Path(sys.executable)
        process = None
        try:
            for index, args in enumerate(commands):
                with self.lock:
                    if ident in self.cancelled or self.closed:
                        outcome = "cancelled"
                        break
                with (output / f"{index}-stdout.json").open("w", encoding="utf-8") as stdout:
                    with self.lock:
                        if ident in self.cancelled or self.closed:
                            outcome = "cancelled"
                            break
                        process = subprocess.Popen(
                            [str(python), "-m", "ashare_agent", *args],
                            cwd=self.root,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                            stdout=stdout,
                            stderr=subprocess.PIPE,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        try:
                            self.process_owner.assign(process)
                        except Exception:
                            process.terminate()
                            process.wait(timeout=2)
                            raise
                        self.process = process
                        self.process_job = ident
                        pid = getattr(process, "pid", None)
                        self.jobs[ident]["pid"] = pid
                        self.jobs[ident]["process_created_at"] = _process_creation_id(pid)
                        self.persist(self.jobs[ident])
                    with (output / f"{index}-stderr.log").open("w", encoding="utf-8") as log:
                        for line in process.stderr:
                            log.write(line)
                            log.flush()
                            self.append(ident, line)
                    code = process.wait()
                self._clear_process(ident, process)
                process = None
                with self.lock:
                    cancelled = ident in self.cancelled or self.closed
                if cancelled:
                    outcome = "cancelled"
                    break
                if code:
                    raise RuntimeError("任务未完成，请查看日志中的输入或数据错误")
                raw = (output / f"{index}-stdout.json").read_text(encoding="utf-8")
                value = json.loads(raw)
                (output / "cli-result.json").write_text(
                    json.dumps(value, ensure_ascii=False), encoding="utf-8"
                )
                if value.get("status") == "blocked":
                    self.append(ident, "数据未就绪：" + "；".join(value.get("warnings", [])) + "\n")
                elif value.get("status") == "partial":
                    self.append(ident, "部分股票数据缺失，结果只代表成功检查的子集。\n")
        except Exception as exc:
            outcome, error = "failed", str(exc)
            self._terminate_owned_process(ident)
            self.append(ident, error + "\n")
        finally:
            with self.lock:
                if ident in self.cancelled:
                    outcome = "cancelled"
                self.jobs[ident].update(
                    status=outcome, error=error, finished_at=datetime.now().astimezone().isoformat()
                )
                self.jobs[ident]["pid"] = None
                self.jobs[ident]["process_created_at"] = None
                self.persist(self.jobs[ident])
                if self.process_job == ident:
                    self.process = None
                    self.process_job = None
                if self.active == ident:
                    self.active = None
                if self.worker_thread is threading.current_thread():
                    self.worker_thread = None

    def cancel(self, ident):
        with self.lock:
            if self.active != ident:
                raise ValueError("任务已结束或不存在")
            self.cancelled.add(ident)
            process = self.process if self.process_job in {None, ident} else None
            if process is not None and process.poll() is None:
                process.terminate()
            self.process_owner.terminate()
            self.append(ident, "已请求停止，现有缓存与日志将保留。\n")

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            ident = self.active
            thread = self.worker_thread
            process = self.process if self.process_job == ident else None
            if ident:
                self.cancelled.add(ident)
                self.append(ident, "工作台正在关闭，任务已请求停止。\n")
        if process is not None and process.poll() is None:
            process.terminate()
        self.process_owner.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self.lock:
            if ident and ident in self.jobs and self.jobs[ident].get("status") == "running":
                self.jobs[ident].update(
                    status="failed",
                    error="工作台已关闭，任务进程已停止但完成状态未确认",
                    finished_at=datetime.now().astimezone().isoformat(),
                    pid=None,
                )
                self.persist(self.jobs[ident])
                self.active = None


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, jobs):
        from .intraday import Monitor

        self._ready = False
        self.jobs = jobs
        self.monitor = Monitor(jobs.root)
        super().__init__(address, Handler)
        self.csrf = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self._ready = True

    def server_close(self):
        try:
            if self._ready:
                self.monitor.close()
                self.jobs.close()
        finally:
            self._ready = False
            super().server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def valid_host(self):
        return self.headers.get("Host") in {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }

    def send(self, body, status=200, mime="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.valid_host():
            return self.send({"error": "Invalid Host"}, 403)
        path = urlsplit(self.path).path
        jobs = self.server.jobs
        try:
            if path == '/research':
                return self.send(Path(__file__).with_name('web').joinpath('research.html').read_bytes(), mime='text/html; charset=utf-8')
            if path == '/api/research/state' or path.startswith('/api/research/packet/') or path == '/api/research/report':
                from .research_commands import db_path, require_study
                from .research_lab import ResearchLab
                if path == '/api/research/state' and not db_path(jobs.root).is_file():
                    return self.send({'status': 'not_initialized', 'batches': [], 'csrf': self.server.csrf})
                require_study(jobs.root)
                lab = ResearchLab(jobs.root)
                try:
                    if path.startswith('/api/research/packet/'):
                        return self.send(lab.packet(path.rsplit('/', 1)[-1]))
                    if path == '/api/research/report':
                        report = lab.report()
                        return self.send(Path(report['html']).read_bytes(), mime='text/html; charset=utf-8')
                    return self.send({**lab.summary(), 'csrf': self.server.csrf})
                finally:
                    lab.close()
            if path == '/api/ai-review/state':
                from .review_center import ReviewCenter
                center = ReviewCenter(jobs.root)
                try:
                    return self.send({**center.state(), 'csrf': self.server.csrf})
                finally:
                    center.close()
            if path.startswith('/api/ai-review/packet/'):
                from .review_center import ReviewCenter
                center = ReviewCenter(jobs.root)
                try:
                    return self.send(center.packet(path.rsplit('/', 1)[-1]))
                finally:
                    center.close()
            if path == "/ai-review":
                return self.send(Path(__file__).with_name("web").joinpath("review_center.html").read_bytes(), mime="text/html; charset=utf-8")
            if path == "/intraday":
                return self.send(Path(__file__).with_name("web").joinpath("intraday.html").read_bytes(), mime="text/html; charset=utf-8")
            if path == "/api/intraday/state":
                return self.send({**self.server.monitor.state(), "intraday_api_version": 1, "csrf": self.server.csrf})
            if path == "/paper":
                return self.send(
                    Path(__file__).with_name("web").joinpath("paper.html").read_bytes(),
                    mime="text/html; charset=utf-8",
                )
            if path == "/api/paper/state":
                from .paper_workspace import list_accounts, snapshot_choices

                return self.send({"paper_api_version": 1, "csrf": self.server.csrf,
                                  "accounts": list_accounts(jobs.root), "snapshots": snapshot_choices(jobs.root),
                                  "active_job": jobs.active})
            if path.startswith("/api/paper/accounts/"):
                from .paper_workspace import account_state

                try:
                    return self.send(account_state(jobs.root, path.removeprefix("/api/paper/accounts/")))
                except ValueError as exc:
                    return self.send({"error": str(exc)}, 404)
            if path == "/planner":
                return self.send(
                    Path(__file__).with_name("web").joinpath("planner.html").read_bytes(),
                    mime="text/html; charset=utf-8",
                )
            if path == "/api/planner/state":
                from .planner import PlanStore

                book = PlanStore(jobs.root / "runtime/planner")
                try:
                    return self.send(
                        {
                            **book.state(),
                            "planner_api_version": 3,
                            "csrf": self.server.csrf,
                            "active_job": jobs.active
                            if jobs.active and jobs.jobs[jobs.active]["kind"] == "planner"
                            else None,
                        }
                    )
                finally:
                    book.close()
            if path.startswith("/api/planner/version/"):
                from .planner import PlanStore

                book = PlanStore(jobs.root / "runtime/planner")
                try:
                    row = book.db.execute(
                        "SELECT body FROM reviews WHERE id=?", (path.rsplit("/", 1)[-1],)
                    ).fetchone()
                    value = json.loads(row[0]) if row else None
                    if not value or not book.visible(value):
                        return self.send({"error": "版本不存在或尚未完成"}, 404)
                    return self.send(value)
                finally:
                    book.close()
            if path == "/":
                return self.send(
                    Path(__file__).with_name("web").joinpath("index.html").read_bytes(),
                    mime="text/html; charset=utf-8",
                )
            if path == "/api/state":
                with jobs.lock:
                    history = [jobs.snapshot(k) for k in sorted(jobs.jobs, reverse=True)[:30]]
                    return self.send(
                        dict(
                            app="ashare-workbench",
                            backend_version=BACKEND_VERSION,
                            csrf=self.server.csrf,
                            active_job=jobs.active,
                            last_job=history[0]["id"] if history else None,
                            jobs=[
                                {k: v for k, v in j.items() if k not in {"logs", "result", "request"}}
                                for j in history
                            ],
                        )
                    )
            if path.startswith("/api/jobs/"):
                return self.send(jobs.snapshot(path.removeprefix("/api/jobs/")))
            if path.startswith("/reports/"):
                parts = path.split("/")
                if (
                    len(parts) != 4
                    or parts[2] not in jobs.jobs
                    or parts[3] not in {"report.html", "result.json", "candidates.csv"}
                ):
                    return self.send({"error": "Invalid artifact"}, 404)
                if jobs.jobs[parts[2]].get("status") != "completed":
                    return self.send({"error": "任务尚未完成，正式结果暂不可访问"}, 404)
                target = jobs.folder / parts[2] / parts[3]
                if not target.is_file():
                    return self.send({"error": "报告尚未生成"}, 404)
                mime = (
                    "text/html; charset=utf-8"
                    if parts[3].endswith("html")
                    else "application/json; charset=utf-8"
                    if parts[3].endswith("json")
                    else "text/csv; charset=utf-8"
                )
                return self.send(target.read_bytes(), mime=mime)
            return self.send({"error": "Not found"}, 404)
        except sqlite3.Error:
            return self.send({'error': '账本暂时忙或不可读，请稍后重试'}, 503)
        except (KeyError, OSError, ValueError):
            return self.send({"error": "结果不存在或暂不可读"}, 404)

    def do_POST(self):
        max_body = 1100000 if self.path in {'/api/research/import', '/api/research/validate', '/api/research/retrospective', '/api/ai-review/import', '/api/ai-review/validate'} else 16000
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        body = b""
        if length >= 0:
            body = self.rfile.read(min(length, max_body))
            if length > max_body:
                self.close_connection = True
        else:
            self.close_connection = True
        origin = self.headers.get("Origin")
        allowed_origins = {self.server.origin, f"http://localhost:{self.server.server_port}"}
        if (
            not self.valid_host()
            or origin not in allowed_origins
            or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf)
        ):
            return self.send({"error": "请求来源校验失败，请刷新工作台"}, 403)
        try:
            if not 0 < length <= max_body:
                raise ValueError("请求过大或为空")
            # The inner result remains RAW text so duplicate keys cannot disappear
            # during generic web JSON parsing before the strict importer sees them.
            from .research_protocol import strict_loads
            body = strict_loads(body)
            if self.path.startswith('/api/research/'):
                from .research_commands import require_study
                from .research_lab import ResearchLab
                action = self.path.rsplit('/', 1)[-1]
                if action not in {'init', 'import', 'validate', 'retrospective', 'export'}:
                    return self.send({'error':'Not found'}, 404)
                if action == 'init':
                    if not isinstance(body, dict) or set(body) != {'modes'}:
                        raise ValueError('初始化只接受 modes 数组')
                    lab = ResearchLab(self.server.jobs.root, modes=body['modes'])
                else:
                    require_study(self.server.jobs.root)
                    lab = ResearchLab(self.server.jobs.root)
                try:
                    if action == 'init':
                        return self.send({'status':'registered','study':lab.registry})
                    if action == 'export':
                        if not isinstance(body, dict) or set(body) != {'batch_id','mode'}:
                            raise ValueError('导出只接受 batch_id 和 mode（null 表示所有已登记模式）')
                        return self.send(lab.export(body['batch_id'], body['mode']))
                    if not isinstance(body, dict) or set(body) != {'batch_id','result_text'}:
                        raise ValueError('上传只接受 batch_id 和原始 JSON 文本 result_text')
                    result = lab.submit_text(body['batch_id'], body['result_text'],
                                             validate_only=action=='validate', retrospective=action=='retrospective')
                    if action != 'validate':
                        result['report'] = lab.report()
                    return self.send(result)
                finally:
                    lab.close()
            if self.path in {'/api/ai-review/import', '/api/ai-review/validate'}:
                from .review_center import ReviewCenter
                if not isinstance(body, dict) or set(body) != {'batch_id', 'results'}:
                    raise ValueError('请选择批次并提供 JSON 结果')
                center = ReviewCenter(self.server.jobs.root)
                try:
                    return self.send(center.import_results(body['batch_id'], body['results'], save=self.path.endswith('/import')))
                finally:
                    center.close()
            if self.path == "/api/intraday/start":
                return self.send(self.server.monitor.start(body))
            if self.path == "/api/intraday/stop":
                if body != {}:
                    raise ValueError("停止监控不接受额外参数")
                return self.send(self.server.monitor.stop())
            if self.path == "/api/paper/create":
                from .paper_workspace import create_account

                if not isinstance(body, dict) or set(body) != {"name", "initial_cash"}:
                    raise ValueError("请仅提供账户名称和虚拟初始资金")
                with self.server.jobs.lock:
                    if self.server.jobs.active or self.server.jobs.recovery_blocked:
                        raise ValueError("有任务运行或等待恢复，请稍后创建模拟账户")
                    return self.send(create_account(self.server.jobs.root, body["name"], body["initial_cash"]))
            if self.path.startswith("/api/planner/"):
                from .planner import PlanStore

                if self.path == "/api/planner/review":
                    return self.send(self.server.jobs.start({"kind": "planner"}), 202)
                with self.server.jobs.lock:
                    if self.server.jobs.active:
                        raise ValueError("任务运行期间暂停修改账户与证据，请先等待或取消")
                    book = PlanStore(self.server.jobs.root / "runtime/planner")
                    try:
                        routes = {
                            "/api/planner/profile": book.save_profile,
                            "/api/planner/position": book.save_position,
                            "/api/planner/evidence": book.manual_evidence,
                            "/api/planner/account-confirmation": book.confirm_account,
                        }
                        if self.path not in routes:
                            return self.send({"error": "Not found"}, 404)
                        return self.send(routes[self.path](body))
                    finally:
                        book.close()
            if self.path == "/api/run":
                return self.send(self.server.jobs.start(body), 202)
            if self.path == "/api/cancel":
                self.server.jobs.cancel(body["id"])
                return self.send({"ok": True})
            return self.send({"error": "Not found"}, 404)
        except sqlite3.Error:
            return self.send({'error': '账本暂时忙或不可写，请稍后重试'}, 503)
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            return self.send({"error": str(exc)}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    import httpx

    url = f"http://127.0.0.1:{args.port}"
    try:
        state = httpx.get(url + "/api/state", trust_env=False, timeout=2).json()
        if state.get("app") == "ashare-workbench" and state.get("backend_version") == BACKEND_VERSION:
            if not args.no_browser:
                webbrowser.open(url)
            return
    except Exception:
        pass

    jobs = Jobs()
    try:
        server = AppServer(("127.0.0.1", args.port), jobs)
    except OSError:
        # The first probe already rejected an unrelated port owner.
        server = AppServer(("127.0.0.1", 0), jobs)
    if not args.no_browser:
        webbrowser.open(server.origin)
    print(server.origin, flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
