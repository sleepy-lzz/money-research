"""Isolated virtual accounts. Preflight executes only against a disposable ledger copy."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import Settings
from .daily_lab import atomic_text, exclusive_run
from .data import load_snapshot, validate_snapshot
from .engine import Engine, source_fingerprint
from .session_calendar import calendar_manifest, next_sessions, sessions

SHANGHAI = ZoneInfo("Asia/Shanghai")


class MissedSession(ValueError):
    pass


def _continuity(completed):
    if completed and _now().date().isoformat() > next_sessions(completed[-1]["trade_date"], 1)[0]:
        raise MissedSession("账户漏过交易日，不能通过重放补成前瞻；请保留旧账本并新建账户")


def _now():
    return datetime.now(SHANGHAI)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _rules():
    return _hash({"engine": source_fingerprint(), "workspace": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError("账户或快照标识无效")
    return value


def _path(root, area, ident=None):
    root = Path(root).resolve()
    target = root / "runtime" / area
    if ident is not None:
        target /= _identifier(ident)
    current = root
    for part in target.relative_to(root).parts:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("模拟账户不接受符号链接或目录联接")
    if not target.resolve().is_relative_to(root / "runtime" / area):
        raise ValueError("路径超出指定运行目录")
    return target


def _file(folder, name):
    target = folder / name
    if target.is_symlink() or not target.resolve().is_relative_to(folder.resolve()):
        raise ValueError("账户文件路径无效")
    return target


def _manifest(root, ident):
    folder = _path(root, "paper-accounts", ident)
    path = _file(folder, "account.json")
    if not path.is_file():
        raise ValueError("模拟账户不存在")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    identity = {k: v for k, v in manifest.items() if k != "identity_hash"}
    if manifest.get("identity_hash") != _hash(identity) or manifest.get("account_id") != ident:
        raise ValueError("模拟账户身份或配置已被修改")
    if manifest.get("mode") != "PAPER" or manifest.get("synthetic") is not False:
        raise ValueError("账户不是独立真实数据前瞻模拟账户")
    return folder, manifest


def create_account(root, name, initial_cash):
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("请输入 1–80 字的账户名称")
    if isinstance(initial_cash, bool) or not isinstance(initial_cash, (str, int, Decimal)):
        raise ValueError("虚拟资金请使用精确金额文本")
    try:
        cash = Decimal(initial_cash)
        if not cash.is_finite() or not Decimal("0.01") <= cash <= Decimal("1000000000") or cash != cash.quantize(Decimal("0.01")):
            raise ValueError("虚拟资金须为 0.01 至 10 亿之间的金额，最多两位小数")
    except InvalidOperation as exc:
        raise ValueError("虚拟资金金额无效") from exc
    settings = Settings()
    settings.risk.initial_cash = float(cash)
    settings = Settings.model_validate(settings.model_dump())
    ident = "paper-" + uuid4().hex
    folder = _path(root, "paper-accounts", ident)
    folder.mkdir(parents=True, exist_ok=False)
    manifest = dict(account_id=ident, name=name.strip(), created_at=_now().isoformat(),
                    mode="PAPER", synthetic=False, virtual_funds=True, initial_cash_cents=int(cash * 100),
                    settings=settings.model_dump(), config_hash=settings.fingerprint, source_hash=_rules())
    manifest["identity_hash"] = _hash(manifest)
    _file(folder, "account.json").write_text(_json(manifest), encoding="utf-8")
    return account_state(root, ident)


def _read(folder):
    path = _file(folder, "account.sqlite")
    if not path.exists():
        return {"meta": {}, "orders": [], "fills": [], "lots": [], "sessions": []}
    # Never construct Ledger/Engine merely to read an account.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        result = {table: [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                  for table in ("orders", "fills", "lots", "sessions")}
        result["meta"] = dict(db.execute("SELECT key,value FROM meta"))
        return result


def _identity_reason(manifest, meta):
    settings = Settings.model_validate(manifest["settings"])
    if manifest["source_hash"] != _rules() or manifest["config_hash"] != settings.fingerprint:
        return "规则或配置版本已变化，请创建新的对照账户；旧账户保留只读"
    if meta and (meta.get("workspace_identity") != manifest["identity_hash"]
                 or meta.get("paper_mode") != "PAPER" or meta.get("run_spec")
                 or meta.get("config_hash") != manifest["config_hash"]):
        return "账本身份与虚拟账户不一致，不能混用回测、重放或其他账户"
    return None


def account_state(root, ident):
    folder, manifest = _manifest(root, ident)
    ledger = _read(folder)
    reason = _identity_reason(manifest, ledger["meta"])
    completed = ledger["sessions"]
    payload = json.loads(ledger["meta"].get("workspace_payload", "null"))
    status = "version_blocked" if reason else "active" if completed else "waiting_data"
    if not reason:
        try:
            _continuity(completed)
        except MissedSession as exc:
            status, reason = "missed_session", str(exc)
        except ValueError as exc:
            status, reason = "waiting_data", str(exc)
    if completed and (not payload or payload.get("workspace_day") != completed[-1]["trade_date"]):
        status, reason, payload = "version_blocked", "账本与保存报告的交易日不一致，停止推进并核查写入来源", None
    cash = int(ledger["meta"].get("cash_cents", manifest["initial_cash_cents"]))
    reserved = sum(r["reserved_cents"] for r in ledger["orders"])
    positions = {}
    for lot in ledger["lots"]:
        if lot["quantity"] <= 0:
            continue
        row = positions.setdefault(lot["ts_code"], {"ts_code": lot["ts_code"], "quantity": 0, "available_quantity": 0})
        row["quantity"] += lot["quantity"]
        if lot["available_on"] <= _now().date().isoformat():
            row["available_quantity"] += lot["quantity"]
    attempts = []
    for path in sorted(folder.glob("attempt-*.json"), reverse=True)[:20]:
        attempts.append(json.loads(_file(folder, path.name).read_text(encoding="utf-8")))
    return dict(account_id=ident, name=manifest["name"], mode="PAPER", virtual_funds=True,
                status=status,
                reasons=[reason] if reason else [] if completed else ["尚未处理交易日；等待通过预检的真实数据快照"],
                cash=f"{Decimal(cash - reserved) / 100:.2f}", cash_cents=cash, reserved_cents=reserved,
                initial_cash_cents=manifest["initial_cash_cents"], sessions_count=len(completed),
                last_session=completed[-1]["trade_date"] if completed else None,
                positions=list(positions.values()), orders=ledger["orders"], fills=ledger["fills"],
                metrics=payload.get("metrics") if payload and completed else {"total_return": None},
                valuation_as_of=completed[-1]["trade_date"] if completed else None,
                attempts=attempts, config_hash=manifest["config_hash"], source_hash=manifest["source_hash"])


def list_accounts(root):
    folder = _path(root, "paper-accounts")
    if not folder.exists():
        return []
    result = []
    for path in sorted(folder.iterdir()):
        if path.is_dir():
            try:
                state = account_state(root, path.name)
                result.append({k: state[k] for k in ("account_id", "name", "status", "last_session")})
            except (ValueError, OSError, sqlite3.Error) as exc:
                result.append({"account_id": path.name, "name": path.name, "status": "blocked", "reasons": [str(exc)]})
    return result


def snapshot_choices(root):
    folder = _path(root, "snapshots")
    if not folder.exists():
        return []
    result = []
    for path in sorted(folder.iterdir()):
        if path.is_dir() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", path.name):
            result.append({"snapshot_id": path.name, "status": "unverified", "note": "必须单独预检，列出不代表可执行"})
    return result


def _input(root, ident, snapshot_id, day=None):
    folder, manifest = _manifest(root, ident)
    ledger = _read(folder)
    reason = _identity_reason(manifest, ledger["meta"])
    if reason:
        raise ValueError(reason)
    now = _now()
    today = now.date().isoformat()
    if day is not None and day != today:
        raise ValueError("只接受今天的前瞻推进，不补做历史交易日")
    if now.hour < 16:
        raise ValueError("请在北京时间交易日 16:00 后推进")
    calendar = calendar_manifest()
    if any(datetime.fromisoformat(source["fetched_at"]) > now for source in calendar["sources"]):
        raise ValueError("独立日历在本次决策时点尚不可见")
    if sessions(today, today) != [today]:
        raise ValueError("今天不是已知交易日")
    _continuity(ledger["sessions"])
    if ledger["sessions"]:
        last = ledger["sessions"][-1]["trade_date"]
        if last != today and next_sessions(last, 1)[0] != today:
            raise MissedSession("账户漏过交易日，不能通过重放补成前瞻；请保留旧账本并新建账户")
    path = _path(root, "snapshots", snapshot_id)
    if not path.is_dir():
        raise ValueError("数据快照不存在，等待可信数据")
    # Prevent parquet/metadata links from bypassing the allowed snapshot directory.
    for file in path.rglob("*"):
        if file.is_symlink() or (hasattr(file, "is_junction") and file.is_junction()):
            raise ValueError("数据快照不接受链接文件")
    snapshot = load_snapshot(path)
    quality = validate_snapshot(snapshot)
    if snapshot.metadata.get("synthetic") is not False or quality["errors"] or not quality["tradable"]:
        raise ValueError("快照不能用于真实前瞻模拟：" + _json(quality))
    upcoming = next_sessions(today, 1)[0]
    actual = sorted(snapshot.calendar)
    if [d for d in actual if today <= d <= upcoming] != [today, upcoming]:
        raise ValueError("快照交易日历与独立交易所日历不一致")
    return folder, manifest, snapshot, today


def _engine(folder, manifest, snapshot, target):
    engine = Engine(snapshot, Settings.model_validate(manifest["settings"]), target)
    try:
        if engine.ledger.get_meta("paper_mode") not in (None, "PAPER") or engine.ledger.get_meta("run_spec"):
            raise ValueError("不能混入历史重放或回测账本")
        with engine.ledger.atomic():
            engine.ledger.set_meta("paper_mode", "PAPER")
            engine.ledger.set_meta("workspace_identity", manifest["identity_hash"])
        return engine
    except BaseException:
        engine.close()
        raise


def preflight(root, ident, snapshot_id, day=None):
    result = dict(account_id=ident, snapshot_id=snapshot_id, status="waiting_data", reasons=[], checked_at=_now().isoformat())
    try:
        folder, manifest, snapshot, today = _input(root, ident, snapshot_id, day)
        # Existing cash/orders/lots are copied consistently using SQLite backup. All
        # constructor/process_day writes happen in this disposable copy, not the account.
        with TemporaryDirectory(prefix="ashare-paper-check-") as temp:
            target = Path(temp) / "account.sqlite"
            existing = _file(folder, "account.sqlite")
            if existing.exists():
                with closing(sqlite3.connect(existing.as_uri() + "?mode=ro", uri=True)) as source:
                    with closing(sqlite3.connect(target)) as destination:
                        source.backup(destination)
            engine = _engine(folder, manifest, snapshot, target)
            try:
                engine.process_day(today, forward=True)
            finally:
                engine.close()
        result.update(status="ready", day=today, reasons=["临时账本已通过完整前瞻处理检查；实际推进仍会重新校验"])
    except MissedSession as exc:
        result.update(status="missed_session", reasons=[str(exc)])
    except (ValueError, OSError, KeyError, RuntimeError, sqlite3.Error) as exc:
        result["reasons"] = [str(exc)]
    return result


def _report(folder, payload):
    archived = _file(folder, "run-" + payload["workspace_day"] + ".json")
    if archived.exists():
        if json.loads(archived.read_text(encoding="utf-8")) != payload:
            raise ValueError("当日归档报告内容冲突，保留原文件并停止")
    else:
        atomic_text(archived, _json(payload))
    path = _file(folder, "latest-run.json")
    atomic_text(path, _json(payload))
    return str(path)


def advance(root, ident, snapshot_id, output=None):
    folder, manifest = _manifest(root, ident)
    _identifier(snapshot_id)
    with exclusive_run(folder):
        attempt_id = "attempt-" + _now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid4().hex
        attempt_path = _file(folder, attempt_id + ".json")
        attempt = dict(account_id=ident, snapshot_id=snapshot_id, recorded_at=_now().isoformat(),
                       status="running", session_committed=False, reasons=[])
        atomic_text(attempt_path, _json(attempt))
        engine = None
        try:
            existing = _read(folder)
            reason = _identity_reason(manifest, existing["meta"])
            if reason:
                attempt.update(status="version_blocked", reasons=[reason])
            else:
                saved = json.loads(existing["meta"].get("workspace_payload", "null"))
                if saved and saved.get("workspace_day") == _now().date().isoformat():
                    if saved.get("workspace_snapshot_id") != snapshot_id:
                        raise ValueError("今天已使用另一快照提交，请查看已保存账户记录")
                    attempt.update(status="already_processed", session_committed=True,
                                   day=saved["workspace_day"], reasons=["读取已提交记录，本次没有重新生成订单或成交"])
                    attempt["report"] = _report(folder, saved)
                else:
                    # Recover a previous committed day's report before overwriting
                    # the latest payload with another session.
                    if saved:
                        try:
                            _report(folder, saved)
                        except Exception:
                            attempt.update(session_committed=True, day=saved["workspace_day"])
                            raise
                    checked = preflight(root, ident, snapshot_id)
                    if checked["status"] != "ready":
                        attempt.update(status=checked["status"], reasons=checked["reasons"])
                    else:
                        folder, manifest, snapshot, today = _input(root, ident, snapshot_id)
                        engine = _engine(folder, manifest, snapshot, _file(folder, "account.sqlite"))
                        with engine.ledger.atomic():
                            last = engine.process_day(today, forward=True)
                            payload = engine.payload(last, "PAPER", ident + "-" + today)
                            payload.update(workspace_day=today, workspace_snapshot_id=snapshot_id)
                            engine.ledger.set_meta("workspace_payload", _json(payload))
                        attempt.update(status="advanced", day=today, session_committed=True)
                        attempt["report"] = _report(folder, payload)
        except Exception as exc:
            attempt.update(status="report_failed" if attempt["session_committed"] else "failed", reasons=[str(exc)])
        finally:
            if engine:
                engine.close()
            attempt["finished_at"] = _now().isoformat()
            atomic_text(attempt_path, _json(attempt))
        if output:
            atomic_text(Path(output) / "result.json", _json(attempt))
        return attempt
