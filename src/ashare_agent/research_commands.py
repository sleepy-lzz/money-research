"""Executable local research workflow; no provider API and no broker access."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import platform
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from .research_protocol import MODES, PROTOCOL, VERSION, canonical, strict_loads


def register_commands(sub):
    for name in ("init", "export", "import", "report", "claim", "doctor", "audit", "demo"):
        command=sub.add_parser("research-"+name, help="Two-layer research v2; no orders or personal accounts")
        command.add_argument("--project-root",type=Path,default=Path.cwd())
        if name=="init":
            command.add_argument("--modes",nargs="+",choices=MODES,default=list(MODES))
        if name in {"export","import","claim"}:
            command.add_argument("--batch-id", required=name in {"import","claim"})
        if name in {"export","claim"}:
            command.add_argument("--mode",choices=MODES, required=name=="claim")
        if name=="import":
            command.add_argument("--input",type=Path,required=True)
            command.add_argument("--retrospective",action="store_true")
            command.add_argument("--validate-only",action="store_true")
        if name=="audit":
            command.add_argument("--runtime",type=Path,help="Read-only runtime path; default project/runtime")
        if name in {"audit","doctor"}:
            command.add_argument("--output",type=Path)


def db_path(root):
    return Path(root)/"runtime/research-v2/observations/forward-lab.sqlite"


def require_study(root):
    if not db_path(root).is_file():
        raise ValueError("第二版尚未初始化/冻结。先 research-init；daily-lab 在真实收盘后采集，不从旧批次补做。")


def audit_runtime(runtime):
    """Known tables only, URI read-only; no private account amounts are exported."""
    runtime=Path(runtime).resolve()
    result=dict(runtime_path=str(runtime), version=VERSION, read_only=True, databases={}, legacy_batches=[], legacy_overlays=[])
    paths={"forward":runtime/"daily-lab/observations/forward-lab.sqlite", "planner":runtime/"planner/plans.sqlite"}
    tables={"forward":["batches","observations","overlay_results","review_imports"],
            "planner":["settings","positions","audit","evidence","reviews","account_confirmations","intraday_confirmations","ticks","alerts"]}
    for name,path in paths.items():
        if not path.is_file():
            result["databases"][name]={"status":"not_found"}
            continue
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        # Standard read-only mode includes an existing WAL, if any. Never use an immutable URI on a live WAL database.
        db=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)
        db.row_factory=sqlite3.Row
        try:
            available={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            counts={t:db.execute('SELECT COUNT(*) FROM "'+t+'"').fetchone()[0] for t in tables[name] if t in available}
            item=dict(path=str(path), sha256=before, counts=counts, integrity_check=db.execute("PRAGMA quick_check").fetchone()[0])
            if name=="forward" and "batches" in available:
                byid={}
                for r in db.execute("SELECT * FROM batches ORDER BY as_of"):
                    row={k:r[k] for k in ("batch_id","as_of","created_at","input_hash","rules_hash")}
                    arms=json.loads(r["arms_json"])
                    evidence=json.loads(r["evidence_json"])
                    row.update(arms=list(arms), evidence_count=len(evidence),primary_evidence_count=sum(e.get("quality")=="verified_primary" for e in evidence))
                    result["legacy_batches"].append(row)
                    byid[r["batch_id"]]=row
                if "overlay_results" in available:
                    for r in db.execute("SELECT * FROM overlay_results ORDER BY created_at"):
                        payload=json.loads(r["payload_json"])
                        batch=byid.get(r["batch_id"])
                        claimed=payload.get("available_at")
                        entry=dict(batch_id=r["batch_id"],mode=r["mode"],local_received_at=r["created_at"],model_reported_at=claimed,
                                   prospective_eligible=False, reason="legacy_storage_only; not_v2_prospective_evidence")
                        if batch and claimed:
                            entry["claimed_before_freeze"]=datetime.fromisoformat(claimed)<datetime.fromisoformat(batch["created_at"])
                            entry["received_after_signal_date"]=datetime.fromisoformat(r["created_at"]).date().isoformat()>batch["as_of"]
                        result["legacy_overlays"].append(entry)
        finally:
            db.close()
        item["unchanged_after_read"]=before==hashlib.sha256(path.read_bytes()).hexdigest()
        result["databases"][name]=item
    for key,relative in (("latest_run","daily-lab/latest-run.json"),("latest_report","daily-lab/latest.json")):
        path=runtime/relative
        if path.is_file():
            value=json.loads(path.read_text(encoding="utf-8"))
            result[key]={k:value.get(k) for k in ("status","outcome","as_of","started_at","updated_at","created_at","phases")}
    result["numeric_cache_validation"]="not_performed_by_metadata_audit"
    result["scheduler_status"]="runtime_logs_do_not_prove_current_scheduling_or_success"
    return result


def doctor(root):
    root=Path(root).resolve()
    dependencies={name:importlib.util.find_spec(name) is not None for name in ("pandas","numpy","pyarrow","pytest","httpx")}
    result=dict(version=VERSION, project_root=str(root),dependencies=dependencies,codex_cli=shutil.which("codex"),
                platform=platform.system(),paid_model_api_added=False,
                handoff="daily-lab -> immutable outbox -> Codex conversation -> JSON -> research-import -> observe/report",
                local_automation="requires_running_Codex_app_or_an_actually_configured_local_runner_and_granted_project_access",
                scheduler_probe={"status":"not_probed_on_non_Windows"},
                current_study_exists=db_path(root).is_file(), no_model_call_performed=True)
    if platform.system()=="Windows":
        code="$ErrorActionPreference='Stop'; Get-ScheduledTask | Where-Object {$_.TaskName -match 'Money|ashare|daily.lab'} | ForEach-Object {$i=$_ | Get-ScheduledTaskInfo; [pscustomobject]@{Name=$_.TaskName;State=[string]$_.State;LastRunTime=[string]$i.LastRunTime;LastTaskResult=$i.LastTaskResult;NextRunTime=[string]$i.NextRunTime}} | ConvertTo-Json -Depth 3"
        try:
            proc=subprocess.run(["powershell","-NoProfile","-NonInteractive","-Command",code],capture_output=True,text=True,timeout=15)
            result["scheduler_probe"]={"status":"completed" if proc.returncode==0 else "failed","return_code":proc.returncode,
                                       "output":proc.stdout,"error":proc.stderr,
                                       "limitation":"name-filtered Windows tasks only; Codex app automations must be checked in its UI"}
        except (OSError,subprocess.TimeoutExpired) as exc:
            result["scheduler_probe"]={"status":"failed","reason":str(exc)}
    return result


def execute_research(args):
    root=args.project_root.resolve()
    name=args.command.removeprefix("research-")
    if getattr(args,"config",None):
        raise ValueError("研究协议不接受主策略 --config 的隐式覆盖")
    if name=="demo":
        from .research_demo import run_demo
        return run_demo(root)
    if name in {"audit","doctor"}:
        result=audit_runtime(args.runtime or root/"runtime") if name=="audit" else doctor(root)
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(canonical(result),encoding="utf-8")
        return result
    from .research_lab import ResearchLab
    if name!="init":
        require_study(root)
    lab=ResearchLab(root,modes=args.modes if name=="init" else None)
    try:
        if name=="init":
            return dict(status="registered",study=lab.registry,protocol=PROTOCOL)
        if name=="export":
            return lab.export(args.batch_id,args.mode)
        if name=="claim":
            return lab.claim(args.batch_id,args.mode)
        if name=="import":
            result=lab.submit_text(args.batch_id,args.input.read_text(encoding="utf-8-sig"),
                                   retrospective=args.retrospective,validate_only=args.validate_only)
            if not args.validate_only:
                result["report"]=lab.report()
            return result
        return lab.report()
    finally:
        lab.close()
