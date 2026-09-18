"""Reproducible command-line entry points. No real-order commands are provided."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import sqlite3
import sys
from contextlib import closing
from datetime import date as Date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from . import __version__
from .config import load_settings
from .data import TushareDownloader, load_snapshot, make_demo_snapshot, save_snapshot, validate_snapshot
from .engine import Engine, records, source_fingerprint
from .models import stable_id
from .research import research_candidates
from .strategy import market_regime, rank_candidates


def write_json(path: Path, payload: object):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ashare", description="A股日频研究与模拟；不支持实盘")
    p.add_argument("--config", type=Path, default=None, help="YAML configuration (defaults to frozen v1)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create local runtime directories and example config")
    init.add_argument("--root", type=Path, default=Path("runtime"))
    demo = sub.add_parser("demo-data", help="Create labelled synthetic fixture snapshot")
    demo.add_argument("--root", type=Path, default=Path("runtime/snapshots"))
    demo.add_argument("--sessions", type=int, default=520)
    demo.add_argument("--seed", type=int, default=42)
    plan = sub.add_parser("planner-review", help="Refresh conditional plans; no real orders")
    plan.add_argument("--output", type=Path, required=True)
    paper_accounts = sub.add_parser("paper-account", help="Isolated virtual account; strict forward data required")
    paper_accounts.add_argument("action", choices=("create", "list", "state", "check", "advance"))
    paper_accounts.add_argument("--id")
    paper_accounts.add_argument("--name")
    paper_accounts.add_argument("--initial-cash", help="Virtual starting cash in CNY, never brokerage funds")
    paper_accounts.add_argument("--snapshot-id")
    paper_accounts.add_argument("--output", type=Path)
    lab = sub.add_parser(
        "daily-lab", help="Automatic prospective factor observations; no fills or real orders"
    )
    lab.add_argument("--output", type=Path)
    lab.add_argument("--check-only", action="store_true", help="Run software checks without market requests")
    overlay = sub.add_parser("overlay-import", help="Retired v1 write command; use research-import")
    overlay.add_argument("--lab-root", type=Path, default=Path("runtime/daily-lab"))
    overlay.add_argument("--input", type=Path, required=True, help="Codex structured JSON file")
    overlay.add_argument("--batch-id", required=False, help="默认使用最新冻结批次")
    export = sub.add_parser("overlay-export", help="Export the immutable evidence packet for Codex review")
    export.add_argument("--lab-root", type=Path, default=Path("runtime/daily-lab"))
    export.add_argument("--batch-id", required=False, help="默认使用最新冻结批次")
    export.add_argument("--output", type=Path, required=True)
    for name in ("current-screen", "current-doctor"):
        current = sub.add_parser(name, help="Current research only; no historical PIT claim or orders")
        current.add_argument("--output", type=Path, required=True)
        current.add_argument("--network", choices=("direct", "environment"), default="direct")
        if name == "current-screen":
            current.add_argument(
                "--symbols", default="", help="Comma-separated MAIN codes; empty uses source directory"
            )
            current.add_argument("--top", type=int, default=20)
            current.add_argument("--min-amount", type=float, default=100_000_000)
    for name in ("download", "update"):
        q = sub.add_parser(name, help="Download cached raw Tushare data; not automatically trading-ready")
        q.add_argument("--root", type=Path, default=Path("runtime/tushare"))
        q.add_argument("--start", required=True)
        q.add_argument("--end", required=True)
        q.add_argument("--refresh", action="store_true", help="Explicitly refresh historical raw cache")
    build = sub.add_parser("build-data", help="Normalize downloaded data with point-in-time evidence")
    build.add_argument("--root", type=Path, default=Path("runtime/tushare"))
    build.add_argument("--start", required=True)
    build.add_argument("--end", required=True)
    build.add_argument("--evidence-dir", type=Path)
    build.add_argument("--output", type=Path, default=Path("runtime/snapshots"))
    doctor = sub.add_parser("doctor", help="Offline snapshot validation or token/cache inspection")
    doctor.add_argument("--snapshot", type=Path)
    doctor.add_argument("--date", default=Date.today().isoformat())
    doctor.add_argument("--root", type=Path, default=Path("runtime/tushare"))
    doctor.add_argument("--output", type=Path, default=Path("runtime/data-quality.json"))
    select = sub.add_parser("select", help="Mechanical ranking; no orders")
    select.add_argument("--snapshot", type=Path, required=True)
    select.add_argument("--date", required=True)
    select.add_argument("--output", type=Path, default=Path("runtime/selection.json"))
    for name in ("backtest", "rolling-oos", "walk-forward", "robustness"):
        q = sub.add_parser(name)
        q.add_argument("--snapshot", type=Path, required=True)
        q.add_argument("--start", required=True)
        q.add_argument("--end", required=True)
        q.add_argument("--output", type=Path)
        if name in {"walk-forward", "rolling-oos"}:
            q.add_argument("--train-sessions", type=int, default=252)
            q.add_argument("--test-sessions", type=int, default=63)
    for name in ("paper", "daily"):
        q = sub.add_parser(
            name, help="Settle existing intents, update risk, freeze next-session intents, report"
        )
        q.add_argument("--snapshot", type=Path, required=True)
        q.add_argument("--date", required=True)
        q.add_argument("--ledger", type=Path, default=Path("runtime/paper/account.sqlite"))
        q.add_argument("--output", type=Path)
        q.add_argument(
            "--replay", action="store_true", help="Explicit historical/synthetic replay; not forward evidence"
        )
        q.add_argument("--evidence", type=Path, help="JSON list of dated research source records")
    report = sub.add_parser("report", help="Re-render a saved immutable run payload")
    report.add_argument("--input", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    from .research_commands import register_commands
    register_commands(sub)
    return p


def _assert_legacy_ledger(path):
    path = Path(path).resolve()
    if path.is_relative_to(Path.cwd().resolve() / "runtime/paper-accounts") or (path.parent / "account.json").exists():
        raise ValueError("独立模拟账户必须通过 paper-account 推进，禁止混用旧命令")
    if path.is_file():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
                if db.execute("SELECT value FROM meta WHERE key='workspace_identity'").fetchone():
                    raise ValueError("此账本由独立模拟账户管理，请使用 paper-account")


def _run_backtest(snapshot, settings, start, end, output: Path):
    from .reporting import write_report

    _assert_legacy_ledger(output / "account.sqlite")
    output.mkdir(parents=True, exist_ok=True)
    engine = Engine(snapshot, settings, output / "account.sqlite")
    try:
        if engine.ledger.get_meta("paper_mode"):
            raise ValueError("Paper and backtest runs must use separate ledgers")
        spec = json.dumps(dict(start=start, end=end, snapshot_id=snapshot.snapshot_id), sort_keys=True)
        old = engine.ledger.get_meta("run_spec")
        if old is not None and old != spec:
            raise ValueError("Output directory belongs to another backtest; choose a new output")
        engine.ledger.set_meta("run_spec", spec)
        last = engine.run(start, end)
        run_id = stable_id(
            "backtest", snapshot.snapshot_id, settings.fingerprint, engine.source_hash, start, end
        )
        payload = engine.payload(last, "BACKTEST", run_id)
        payload["research"] = [
            dict(status="disabled_for_backtest", reason="Historical LLM reasoning excluded")
        ]
        payload["environment"] = {
            "python": sys.version.split()[0],
            "ashare_agent": __version__,
            "config_hash": settings.fingerprint,
        }
        write_json(output / "run.json", payload)
        paths = write_report(output, payload)
        return dict(
            run_id=run_id,
            output=str(output.resolve()),
            reports=paths,
            metrics=payload["metrics"],
            synthetic=payload["synthetic"],
            fills=len(payload["fills"]),
            sessions=len(payload["equity"]),
        )
    finally:
        engine.close()


def execute(args) -> dict:
    if args.command.startswith("research-"):
        from .research_commands import execute_research
        return execute_research(args)
    if args.command == "overlay-import":
        raise ValueError("旧 overlay-import 已停用写入：旧记录只读保留；前瞻使用 research-import，晚到材料使用其 --retrospective 参数。")
    if args.command in {"paper", "daily"}:
        _assert_legacy_ledger(args.ledger)
    if args.command == "paper-account":
        from . import paper_workspace

        if args.config:
            raise ValueError("模拟账户使用创建时固定的配置，不接受运行时覆盖")
        root = Path.cwd()
        if args.action == "create":
            result = paper_workspace.create_account(root, args.name, args.initial_cash)
        elif args.action == "list":
            result = {"accounts": paper_workspace.list_accounts(root), "snapshots": paper_workspace.snapshot_choices(root)}
        elif args.action == "state":
            result = paper_workspace.account_state(root, args.id)
        elif args.action == "check":
            result = paper_workspace.preflight(root, args.id, args.snapshot_id)
        else:
            result = paper_workspace.advance(root, args.id, args.snapshot_id)
        if args.output:
            write_json(args.output / "result.json", result)
        return result
    if args.command == "daily-lab":
        from .daily_lab import run

        result = run(
            Path.cwd(),
            args.output,
            args.check_only,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
        return {k: result[k] for k in ("mode", "status", "as_of", "output", "notes")}
    if args.command in {"overlay-export", "overlay-import"}:
        from .ai_overlay import OverlayLedger
        lab_db = args.lab_root / "observations" / "forward-lab.sqlite"
        if not lab_db.exists():
            raise ValueError("找不到前瞻账本；请先运行 daily-lab")
        with closing(sqlite3.connect(lab_db)) as db:
            db.row_factory = sqlite3.Row
            batch = db.execute("SELECT * FROM batches WHERE batch_id=?", (args.batch_id,)).fetchone() if args.batch_id else db.execute("SELECT * FROM batches ORDER BY as_of DESC, created_at DESC LIMIT 1").fetchone()
            if not batch:
                raise ValueError("batch_id 不存在")
            args.batch_id = batch["batch_id"]
            selection = json.loads(batch["selection_json"])
            codes = {str(r["ts_code"]).upper() for r in selection.get("candidates", [])}
            evidence_rows = json.loads(batch["evidence_json"] or "[]")
            evidence = {str(r.get("evidence_id")) for r in evidence_rows if r.get("evidence_id")}
        if args.command == "overlay-export":
            write_json(args.output, {"schema_version": 1, "batch_id": args.batch_id, "as_of": batch["as_of"], "input_hash": batch["input_hash"], "candidates": selection.get("candidates", []), "evidence": evidence_rows, "instructions": "只引用 evidence；仅输出 decisions，allowed_action=keep/deprioritize/unknown；不得输出仓位、价格或订单。"})
            return {"status": "exported", "batch_id": args.batch_id, "output": str(args.output.resolve()), "evidence_count": len(evidence_rows)}
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        ledger = OverlayLedger(lab_db)
        try:
            result = ledger.record(payload, batch_id=args.batch_id, as_of=batch["as_of"], batch_codes=codes, evidence_ids=evidence, expected_input_hash=batch["input_hash"])
        finally:
            ledger.close()
        return {"status": "imported", "result_id": result["result_id"], "batch_id": args.batch_id, "mode": result["mode"], "mechanical_fallback": True, "trading_weight": 0}
    if args.command == "planner-review":
        from .planner import review

        return review(
            Path.cwd(), args.output, progress=lambda message: print(message, file=sys.stderr, flush=True)
        )
    settings = load_settings(args.config)
    if args.command in {"current-screen", "current-doctor"}:
        from .current_screen import doctor, screen

        def progress(value):
            print(value, file=sys.stderr, flush=True)

        if args.command == "current-doctor":
            return doctor(args.output, args.network, progress)
        return screen(args.output, args.network, args.symbols, args.top, args.min_amount, progress)
    if args.command == "init":
        import yaml

        for name in ("snapshots", "tushare", "runs", "paper", "research-cache"):
            (args.root / name).mkdir(parents=True, exist_ok=True)
        config = args.root / "settings.yaml"
        if not config.exists():
            config.write_text(yaml.safe_dump(settings.model_dump(), sort_keys=False), encoding="utf-8")
        deps = {
            name: importlib.metadata.version(name)
            for name in ("ashare-agent", "pandas", "numpy", "duckdb", "pyarrow", "pydantic", "tushare")
        }
        write_json(args.root / "environment.json", dict(python=sys.version, dependencies=deps))
        return dict(root=str(args.root.resolve()), config=str(config.resolve()), live_execution="unsupported")
    if args.command == "demo-data":
        snapshot = make_demo_snapshot(args.sessions, args.seed)
        path = save_snapshot(snapshot, args.root)
        return dict(snapshot=str(path.resolve()), synthetic=True, quality=validate_snapshot(snapshot))
    if args.command in {"download", "update"}:
        return TushareDownloader(args.root).download(args.start, args.end, refresh=args.refresh)
    if args.command == "build-data":
        downloader = TushareDownloader(args.root)
        snapshot = downloader.build_snapshot(args.start, args.end, evidence_dir=args.evidence_dir)
        path = save_snapshot(snapshot, args.output)
        return dict(snapshot=str(path.resolve()), quality=validate_snapshot(snapshot))
    if args.command == "doctor":
        result = (
            validate_snapshot(load_snapshot(args.snapshot))
            if args.snapshot
            else TushareDownloader(args.root).capabilities(args.date)
        )
        write_json(args.output, result)
        return result
    if args.command == "report":
        from .reporting import write_report

        return write_report(args.output, json.loads(args.input.read_text(encoding="utf-8")))
    snapshot = load_snapshot(args.snapshot)
    if args.command == "select":
        quality = validate_snapshot(snapshot)
        if quality["errors"]:
            raise ValueError(f"Invalid snapshot: {quality['errors']}")
        ranking = rank_candidates(snapshot, args.date, settings.strategy.model_dump())
        result = dict(
            as_of=args.date,
            snapshot_id=snapshot.snapshot_id,
            synthetic=bool(snapshot.metadata.get("synthetic")),
            quality=quality,
            regime=market_regime(snapshot, args.date, settings.data.benchmark),
            candidates=records(ranking),
        )
        write_json(args.output, result)
        return result
    if args.command == "backtest":
        ident = stable_id(
            snapshot.snapshot_id, settings.fingerprint, source_fingerprint(), args.start, args.end
        )
        return _run_backtest(
            snapshot, settings, args.start, args.end, args.output or Path("runtime/runs") / ident
        )
    if args.command in {"walk-forward", "rolling-oos"}:
        from .analytics import walk_forward_windows

        available = sorted(snapshot.market.trade_date.unique().tolist())
        windows = walk_forward_windows(
            available, args.start, args.end, args.train_sessions, args.test_sessions
        )
        if not windows:
            raise ValueError("Insufficient sessions for requested walk-forward windows")
        ident = stable_id(
            "wf",
            snapshot.snapshot_id,
            settings.fingerprint,
            args.start,
            args.end,
            args.train_sessions,
            args.test_sessions,
        )
        output = args.output or Path("runtime/runs") / ident
        results = []
        for index, window in enumerate(windows):
            run = _run_backtest(
                snapshot, settings, window["test_start"], window["test_end"], output / f"fold-{index + 1:02d}"
            )
            results.append(dict(window=window, result=run))
        payload = dict(
            strategy="frozen rule baseline; no model fitting or automatic tuning",
            folds=results,
            warning="各测试窗口独立初始资金；不拼接成连续实盘曲线。已看过的窗口不能再次称为未见测试。",
            synthetic=bool(snapshot.metadata.get("synthetic")),
        )
        write_json(output / "walk-forward.json", payload)
        return dict(output=str(output.resolve()), **payload)
    if args.command == "robustness":
        ident = stable_id("robustness", snapshot.snapshot_id, settings.fingerprint, args.start, args.end)
        output = args.output or Path("runtime/runs") / ident
        results = {}
        # Predeclared cost/capacity stress scenarios, never selected for best return.
        for name, slip, commission, capacity in (
            ("baseline", 1, 1, 1),
            ("cost_x2", 2, 2, 1),
            ("capacity_half", 1, 1, 0.5),
        ):
            cfg = settings.model_dump()
            cfg["execution"]["slippage_bps"] *= slip
            cfg["execution"]["commission_rate"] *= commission
            cfg["execution"]["minimum_commission"] *= commission
            cfg["execution"]["adv_participation"] *= capacity
            from .config import Settings

            scenario = Settings.model_validate(cfg)
            results[name] = _run_backtest(snapshot, scenario, args.start, args.end, output / name)
        write_json(output / "robustness.json", results)
        return dict(output=str(output.resolve()), scenarios=results)
    if args.command in {"paper", "daily"}:
        from .reporting import write_report

        mode = "PAPER_REPLAY" if args.replay else "PAPER"
        engine = Engine(snapshot, settings, args.ledger)
        try:
            if engine.ledger.get_meta("run_spec"):
                raise ValueError("Paper and backtest runs must use separate ledgers")
            existing_mode = engine.ledger.get_meta("paper_mode")
            if existing_mode and existing_mode != mode:
                raise ValueError("Forward and replay records must use different ledgers")
            engine.ledger.set_meta("paper_mode", mode)
            last = engine.process_day(args.date, forward=not args.replay)
            output = args.output or args.ledger.parent / "reports" / args.date
            evidence = json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence else None
            # Never send a historical replay to LLM; research is explanatory only.
            cfg = settings.research.model_dump()
            if args.replay:
                cfg["enabled"] = False
            research = research_candidates(
                pd.DataFrame(last["candidates"]),
                args.date,
                cfg,
                args.ledger.parent / "research-cache",
                evidence,
                decision_cutoff=last["decision_cutoff"],
            )
            run_id = stable_id(
                mode, settings.fingerprint, engine.source_hash, snapshot.snapshot_id, args.date
            )
            payload = engine.payload(last, mode, run_id, research)
            write_json(output / "run.json", payload)
            reports = write_report(output, payload)
            return dict(
                run_id=run_id,
                mode=mode,
                reports=reports,
                risk=last["risk"],
                cash=engine.broker.get_cash(),
                orders=len(payload["orders"]),
                fills=len(payload["fills"]),
            )
        finally:
            engine.close()
    raise ValueError("Unknown command")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        result = execute(parser().parse_args())
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        logging.error("%s: %s", type(exc).__name__, exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
