"""Exercise public CLI commands against a labelled fixture, without credentials or network."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("runtime/acceptance"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    output = args.output.resolve()
    logs = output / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    completed = []

    def call(name: str, *arguments: str) -> dict:
        command = [sys.executable, "-m", "ashare_agent", *map(str, arguments)]
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", env=environment, check=False
        )
        (logs / f"{name}.stdout.json").write_text(result.stdout, encoding="utf-8")
        (logs / f"{name}.stderr.log").write_text(result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"{name} failed ({result.returncode}); inspect {logs}")
        payload = json.loads(result.stdout)
        completed.append(name)
        print(f"PASS {name}", flush=True)
        return payload

    snapshot = call("demo", "demo-data", "--sessions", "320")["snapshot"]
    quality = call("doctor-snapshot", "doctor", "--snapshot", snapshot, "--output", output / "quality.json")
    assert quality["tradable"] and not quality["errors"]
    capability = call("doctor-offline", "doctor", "--output", output / "capabilities.json")
    assert capability["network_checked"] is False
    call(
        "select",
        "select",
        "--snapshot",
        snapshot,
        "--date",
        "2025-12-31",
        "--output",
        output / "selection.json",
    )

    backtest_args = (
        "backtest",
        "--snapshot",
        snapshot,
        "--start",
        "2025-09-25",
        "--end",
        "2025-12-31",
        "--output",
        output / "backtest",
    )
    result = call("backtest", *backtest_args)
    first = json.loads((output / "backtest/run.json").read_text(encoding="utf-8"))
    assert result["fills"] > 0 and result["sessions"] == 70 and first["synthetic"] is True
    call("backtest-repeat", *backtest_args)
    repeated = json.loads((output / "backtest/run.json").read_text(encoding="utf-8"))
    assert first == repeated, "Repeated backtest must not rewrite the account or its as-of record"

    # Each verification invocation starts a new replay account, while duplicate
    # command checks below intentionally reuse that same account within the run.
    paper_root = output / "paper"
    paper_root.mkdir(exist_ok=True)
    ledger = Path(tempfile.mkdtemp(prefix="run-", dir=paper_root)) / "account.sqlite"
    paper_args = ("paper", "--snapshot", snapshot, "--replay", "--ledger", ledger)
    call("paper-first", *paper_args, "--date", "2025-09-26")
    paper = call("paper-second", *paper_args, "--date", "2025-09-29")
    assert paper["fills"] > 0, "Paper acceptance must exercise real simulated fills"
    duplicate = call("paper-repeat", *paper_args, "--date", "2025-09-29")
    assert paper == duplicate
    call("report", "report", "--input", output / "backtest/run.json", "--output", output / "report-copy")

    windows = call(
        "walk-forward",
        "walk-forward",
        "--snapshot",
        snapshot,
        "--start",
        "2024-10-10",
        "--end",
        "2025-12-31",
        "--train-sessions",
        "252",
        "--test-sessions",
        "21",
        "--output",
        output / "walk-forward",
    )
    stress = call(
        "robustness",
        "robustness",
        "--snapshot",
        snapshot,
        "--start",
        "2025-09-25",
        "--end",
        "2025-12-31",
        "--output",
        output / "robustness",
    )
    assert len(windows["folds"]) == 3  # Only complete 21-session windows; five trailing days excluded.
    assert set(stress["scenarios"]) == {"baseline", "cost_x2", "capacity_half"}
    summary = dict(
        status="passed",
        synthetic=True,
        completed=completed,
        sessions=result["sessions"],
        fills=result["fills"],
        paper_fills=paper["fills"],
        paper_ledger=str(ledger),
        walk_forward_folds=len(windows["folds"]),
        walk_forward_tail_sessions=5,
        stress_scenarios=list(stress["scenarios"]),
        source_hash=first["provenance"]["source_hash"],
        network_checked=False,
        live_trading=False,
        report=str(output / "backtest/report.html"),
        limitation="Software fixture validation only; no real market or strategy alpha validation",
    )
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
