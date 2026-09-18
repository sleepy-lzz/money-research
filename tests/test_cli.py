from __future__ import annotations

import json
from pathlib import Path

import pytest

from ashare_agent.cli import execute, parser
from ashare_agent.data import make_demo_snapshot, save_snapshot


@pytest.fixture(scope="module")
def snapshot_path(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[str]]:
    root = tmp_path_factory.mktemp("cli-snapshot")
    snapshot = make_demo_snapshot(sessions=285, seed=42)
    path = save_snapshot(snapshot, root / "snapshots")
    dates = sorted(snapshot.market["trade_date"].unique().tolist())
    return path, dates[-3:]


def _args(*values: str):
    return parser().parse_args(list(values))


def test_backtest_repeat_is_idempotent_and_report_rerenders(snapshot_path, tmp_path: Path):
    snapshot, dates = snapshot_path
    output = tmp_path / "backtest"
    command = (
        "backtest",
        "--snapshot",
        str(snapshot),
        "--start",
        dates[0],
        "--end",
        dates[-1],
        "--output",
        str(output),
    )

    first = execute(_args(*command))
    first_payload = json.loads((output / "run.json").read_text(encoding="utf-8"))
    first_fills = list(first_payload["fills"])
    first_orders = list(first_payload["orders"])
    assert first["sessions"] == 3
    assert (output / "run.json").exists()

    second = execute(_args(*command))
    second_payload = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert second["fills"] == first["fills"]
    assert second_payload["fills"] == first_fills
    assert second_payload["orders"] == first_orders

    rerendered = tmp_path / "rerendered"
    report_result = execute(_args("report", "--input", str(output / "run.json"), "--output", str(rerendered)))
    assert set(report_result) >= {"markdown", "html", "json"}
    assert all(Path(path).exists() for path in report_result.values())


def test_paper_cannot_borrow_backtest_ledger_or_reverse(snapshot_path, tmp_path: Path):
    snapshot, dates = snapshot_path
    backtest_output = tmp_path / "backtest-ledger"
    backtest_args = _args(
        "backtest",
        "--snapshot",
        str(snapshot),
        "--start",
        dates[0],
        "--end",
        dates[-1],
        "--output",
        str(backtest_output),
    )
    execute(backtest_args)
    with pytest.raises(ValueError, match="separate ledgers"):
        execute(
            _args(
                "paper",
                "--snapshot",
                str(snapshot),
                "--date",
                dates[0],
                "--ledger",
                str(backtest_output / "account.sqlite"),
                "--replay",
                "--output",
                str(tmp_path / "paper-from-backtest"),
            )
        )

    paper_ledger = tmp_path / "paper-ledger" / "account.sqlite"
    execute(
        _args(
            "paper",
            "--snapshot",
            str(snapshot),
            "--date",
            dates[0],
            "--ledger",
            str(paper_ledger),
            "--replay",
            "--output",
            str(tmp_path / "paper-report"),
        )
    )
    with pytest.raises(ValueError, match="separate ledgers"):
        execute(
            _args(
                "backtest",
                "--snapshot",
                str(snapshot),
                "--start",
                dates[0],
                "--end",
                dates[-1],
                "--output",
                str(paper_ledger.parent),
            )
        )


def test_snapshot_doctor_and_select_cli_paths(snapshot_path, tmp_path: Path):
    snapshot, dates = snapshot_path
    doctor_output = tmp_path / "doctor.json"
    doctor_result = execute(
        _args(
            "doctor",
            "--snapshot",
            str(snapshot),
            "--date",
            dates[-1],
            "--output",
            str(doctor_output),
        )
    )
    assert doctor_result["errors"] == []
    assert doctor_result["tradable"] is True
    assert json.loads(doctor_output.read_text(encoding="utf-8"))["errors"] == []

    selection_output = tmp_path / "selection.json"
    selection = execute(
        _args(
            "select",
            "--snapshot",
            str(snapshot),
            "--date",
            dates[-1],
            "--output",
            str(selection_output),
        )
    )
    assert selection["synthetic"] is True
    assert selection["candidates"]
    assert json.loads(selection_output.read_text(encoding="utf-8"))["snapshot_id"] == selection["snapshot_id"]
