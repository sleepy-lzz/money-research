"""Local Windows launcher; all trading/data rules remain in the public CLI."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/Scripts/python.exe"


def readable_result(raw):
    try:
        data = json.loads(raw)
    except ValueError:
        return raw[-18000:]
    lines = []
    if data.get("synthetic"):
        lines.append("【合成数据演示，非真实股票表现】")
    if "candidates" in data:
        lines.append(f"筛选日期：{data.get('as_of')}；候选结果：")
        lines.append("排名   股票代码       分数      收盘价     状态")
        for row in data["candidates"]:
            state = "通过" if row.get("eligible") else "排除：" + str(row.get("reason", ""))
            lines.append(
                f"{str(row.get('rank', '-')):5}  {row['ts_code']:12} "
                f"{str(row.get('score', '-')):8} {str(row.get('close', '-')):9} {state}"
            )
    if "metrics" in data:
        metrics = data["metrics"]
        lines.append(f"回测完成：{metrics.get('sessions', '-')} 个交易日")
        value = metrics.get("total_return")
        if value is not None:
            lines.append(f"样本区间收益：{value:.2%}（不是完整自然年收益）")
        lines.append("详细净值、持仓和交易记录请查看 HTML 报告。")
    quality = data.get("quality", data)
    if "tradable" in quality:
        lines.append("数据门禁：" + ("通过当前校验" if quality["tradable"] else "未通过，不能正式模拟"))
        lines.extend("错误：" + str(x) for x in quality.get("errors", []))
        lines.extend("说明：" + str(x) for x in quality.get("warnings", []))
    return "\n".join(lines) if lines else raw[-18000:]


class Desktop:
    def __init__(self, root):
        self.root = root
        root.title("A 股研究工作台 · 本地模拟")
        root.geometry("1050x780")
        root.minsize(860, 640)
        self.events = queue.Queue()
        self.busy = False
        self.buttons = []
        self.report = None
        self.run_dir = None
        self.snapshot = tk.StringVar(value=str(ROOT / "runtime/snapshots/demo-v2-320-42"))
        self.start = tk.StringVar(value="2025-09-25")
        self.end = tk.StringVar(value="2025-12-31")
        self.evidence = tk.StringVar(value=str(ROOT / "runtime/evidence"))
        self.status = tk.StringVar(value="就绪：首次使用请点“一键演示”。无需 Token。")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=(12, 7), font=("Microsoft YaHei UI", 10))
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        box = ttk.Frame(root, padding=18)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="A 股研究工作台", font=("Microsoft YaHei UI", 21, "bold")).pack(anchor="w")
        ttk.Label(box, text="沪深主板 · 机械策略 · 模拟账户 · 不支持真实下单").pack(anchor="w", pady=(3, 12))
        top = ttk.Frame(box)
        top.pack(fill="x")
        self.button(top, "▶ 一键演示", self.demo)
        self.button(top, "打开最近报告", self.open_report, lock=False)
        self.button(
            top, "打开结果文件夹", lambda: self.open_path(self.run_dir or ROOT / "runtime"), lock=False
        )
        self.button(top, "使用说明", lambda: self.open_path(ROOT / "使用说明.txt"), lock=False)
        tabs = ttk.Notebook(box)
        tabs.pack(fill="x", pady=12)
        research = ttk.Frame(tabs, padding=12)
        data = ttk.Frame(tabs, padding=12)
        tabs.add(research, text="  研究与回测  ")
        tabs.add(data, text="  真实数据准备  ")
        self.field(research, 0, "快照文件夹", self.snapshot, self.choose_snapshot)
        dates = ttk.Frame(research)
        dates.grid(row=1, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Label(dates, text="开始日期").pack(side="left")
        ttk.Entry(dates, textvariable=self.start, width=14).pack(side="left", padx=(8, 20))
        ttk.Label(dates, text="结束 / 筛选日期").pack(side="left")
        ttk.Entry(dates, textvariable=self.end, width=14).pack(side="left", padx=8)
        actions = ttk.Frame(research)
        actions.grid(row=2, column=0, columnspan=3, sticky="w")
        self.button(actions, "检查快照", lambda: self.run_action("doctor"))
        self.button(actions, "筛选候选", lambda: self.run_action("select"))
        self.button(actions, "运行回测", lambda: self.run_action("backtest"))
        self.button(actions, "滚动 OOS", lambda: self.run_action("rolling-oos"))
        ttk.Label(
            research,
            text="日期格式 YYYY-MM-DD。策略需要历史预热；演示日期已预填。筛选不会下单。",
            wraplength=860,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(
            data,
            text="使用研究页的起止日期下载。首次先取短区间检查接口；下载成功不代表历史 PIT 验证通过。",
            wraplength=860,
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self.field(data, 1, "历史证据目录", self.evidence, self.choose_evidence)
        actions = ttk.Frame(data)
        actions.grid(row=2, column=0, columnspan=3, sticky="w", pady=8)
        self.button(actions, "设置 Tushare Token", self.edit_env, lock=False)
        self.button(actions, "检查配置（离线）", lambda: self.run_action("config"))
        self.button(actions, "下载原始数据", lambda: self.run_action("download"))
        self.button(actions, "构建数据快照", lambda: self.run_action("build-data"))
        ttk.Label(
            data,
            text="缺少可信历史集合、状态或时间证据时，正式回测会被拒绝。配置按钮用记事本打开本地 .env。",
            wraplength=860,
        ).grid(row=3, column=0, columnspan=3, sticky="w")
        ttk.Label(box, textvariable=self.status, wraplength=960).pack(anchor="w", pady=(0, 8))
        self.progress = ttk.Progressbar(box, mode="indeterminate")
        self.progress.pack(fill="x")
        ttk.Label(box, text="结果与运行日志（完整输出同时保存在结果文件夹）").pack(anchor="w", pady=(10, 4))
        self.log = ScrolledText(
            box, font=("Microsoft YaHei UI", 10), wrap="word", height=15, state="disabled"
        )
        self.log.pack(fill="both", expand=True)
        self.append(
            "一键演示：生成合成快照 → 回测 → 保存并打开 HTML 报告。\n合成股票价格只用于软件演示，不能用于实际投资判断。\n"
        )
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def field(self, frame, row, title, variable, choose):
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=title).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(frame, textvariable=variable, width=70).grid(row=row, column=1, sticky="ew")
        button = ttk.Button(frame, text="选择…", command=choose)
        button.grid(row=row, column=2, padx=8)

    def button(self, parent, text, command, lock=True):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(side="left", padx=(0, 8))
        if lock:
            self.buttons.append(button)

    def choose_snapshot(self):
        path = filedialog.askdirectory(
            initialdir=ROOT / "runtime/snapshots", title="选择含 manifest.json 的快照目录"
        )
        if path:
            self.snapshot.set(path)

    def choose_evidence(self):
        path = filedialog.askdirectory(initialdir=ROOT / "runtime", title="选择历史证据目录")
        if path:
            self.evidence.set(path)

    def append(self, value):
        self.log.configure(state="normal")
        self.log.insert("end", value)
        self.log.see("end")
        self.log.configure(state="disabled")

    def open_path(self, path):
        path = Path(path)
        if path.exists():
            os.startfile(str(path))
        else:
            messagebox.showinfo("尚无文件", "请先运行相应功能。")

    def edit_env(self):
        path = ROOT / ".env"
        if not path.exists():
            path.write_text("TUSHARE_TOKEN=\n", encoding="utf-8")
        subprocess.Popen(["notepad.exe", str(path)])

    def open_report(self):
        reports = list((ROOT / "runtime").glob("**/report.html"))
        path = self.report or (max(reports, key=lambda p: p.stat().st_mtime) if reports else None)
        if path:
            self.open_path(path)
        else:
            messagebox.showinfo("尚无报告", "请先点“一键演示”或运行回测。")

    def new_run(self):
        return ROOT / "runtime/workbench" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6])

    def demo(self):
        snapshot = ROOT / "runtime/snapshots/demo-v2-320-42"
        self.snapshot.set(str(snapshot))
        self.start.set("2025-09-25")
        self.end.set("2025-12-31")
        output = self.new_run()
        self.launch(
            [
                ["demo-data", "--sessions", "320"],
                [
                    "backtest",
                    "--snapshot",
                    str(snapshot),
                    "--start",
                    self.start.get(),
                    "--end",
                    self.end.get(),
                    "--output",
                    str(output),
                ],
            ],
            output,
            show_report=True,
        )

    def run_action(self, action):
        try:
            start, end = self.start.get().strip(), self.end.get().strip()
            if date.fromisoformat(start) > date.fromisoformat(end):
                raise ValueError("开始日期不能晚于结束日期")
            output = self.new_run()
            snap = str(Path(self.snapshot.get()).resolve())
            if action in {"doctor", "select", "backtest", "rolling-oos"}:
                if not (Path(snap) / "manifest.json").is_file():
                    raise ValueError("请先运行一键演示，或选择含 manifest.json 的快照文件夹")
            if action == "config":
                command = ["doctor", "--output", str(output / "quality.json")]
            elif action == "doctor":
                command = ["doctor", "--snapshot", snap, "--output", str(output / "quality.json")]
            elif action == "select":
                command = [
                    "select",
                    "--snapshot",
                    snap,
                    "--date",
                    end,
                    "--output",
                    str(output / "selection.json"),
                ]
            elif action in {"backtest", "rolling-oos"}:
                command = [
                    action,
                    "--snapshot",
                    snap,
                    "--start",
                    start,
                    "--end",
                    end,
                    "--output",
                    str(output),
                ]
                if action == "rolling-oos":
                    command += ["--test-sessions", "21"]
            else:
                command = [action, "--start", start, "--end", end]
                if action == "build-data":
                    command += ["--evidence-dir", self.evidence.get()]
            self.launch([command], output, show_report=action == "backtest")
        except ValueError as exc:
            messagebox.showerror("输入需要调整", str(exc))

    def launch(self, commands, output, show_report=False):
        if self.busy:
            return
        output.mkdir(parents=True, exist_ok=True)
        self.busy = True
        self.run_dir = output
        self.status.set("正在运行，请稍候；窗口保持响应，耗时步骤完成后会显示结果。")
        for button in self.buttons:
            button.configure(state="disabled")
        self.progress.start(12)
        self.append(f"\n结果目录：{output}\n")

        def worker():
            try:
                for index, args in enumerate(commands):
                    self.events.put(("log", f"\n执行：{' '.join(args)}\n"))
                    result = subprocess.run(
                        [str(PYTHON), "-m", "ashare_agent", *args],
                        cwd=ROOT,
                        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    (output / f"{index}-{args[0]}.stdout.json").write_text(result.stdout, encoding="utf-8")
                    (output / f"{index}-{args[0]}.stderr.log").write_text(result.stderr, encoding="utf-8")
                    self.events.put(("log", readable_result(result.stdout) + result.stderr[-6000:]))
                    if result.returncode:
                        raise RuntimeError(
                            "命令未完成。请查看上方原因；日期、数据完整性或历史证据不足需要先处理。"
                        )
                    try:
                        payload = json.loads(result.stdout)
                        quality = payload.get("quality", payload)
                        if quality.get("tradable") is False or payload.get("status") == "partial":
                            self.events.put(
                                ("log", "\n注意：数据检查未通过或下载不完整，不能据此开始正式模拟。\n")
                            )
                    except (ValueError, AttributeError):
                        pass
                self.events.put(("done", (True, show_report, "已完成。查看下方结果或打开结果文件夹。")))
            except Exception as exc:
                self.events.put(("done", (False, False, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.append(value + "\n")
                else:
                    success, show_report, message = value
                    self.busy = False
                    self.progress.stop()
                    for button in self.buttons:
                        button.configure(state="normal")
                    self.status.set(message)
                    self.append(message + "\n")
                    if success and show_report and (self.run_dir / "report.html").is_file():
                        self.report = self.run_dir / "report.html"
                        self.open_report()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self):
        if self.busy:
            messagebox.showinfo("任务正在运行", "请等待当前任务完成后关闭，以保留完整日志和结果。")
            return
        self.root.destroy()


def main():
    os.chdir(ROOT)
    root = tk.Tk()
    Desktop(root)
    root.mainloop()


if __name__ == "__main__":
    main()
