from datetime import datetime
from decimal import Decimal

import pytest
from test_hardening import real_contract_fixture

import ashare_agent.engine as engine_module
from ashare_agent import paper_workspace as paper
from ashare_agent.data import make_demo_snapshot, save_snapshot


@pytest.fixture
def clock(monkeypatch):
    current = [datetime.fromisoformat("2025-12-29T20:00:00+08:00")]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz)

    monkeypatch.setattr(paper, "_now", lambda: current[0])
    monkeypatch.setattr(engine_module, "datetime", Clock)
    # Contract fixture only. These are not claims of actual historical ingestion.
    manifest = paper.calendar_manifest()
    for source in manifest["sources"]:
        source["fetched_at"] = "2025-01-01T00:00:00+08:00"
    monkeypatch.setattr(paper, "calendar_manifest", lambda: manifest)
    return current


def fixture_snapshot(root):
    return save_snapshot(real_contract_fixture(), root / "runtime/snapshots").name


def test_empty_reads_and_virtual_creation_never_create_trading_ledger(tmp_path):
    assert paper.list_accounts(tmp_path) == []
    assert paper.snapshot_choices(tmp_path) == []
    assert not (tmp_path / "runtime").exists()
    account = paper.create_account(tmp_path, "测试虚拟资金", "100000.01")
    assert account["cash"] == "100000.01"
    assert account["sessions_count"] == 0
    assert account["metrics"]["total_return"] is None
    assert account["status"] == "waiting_data"
    assert not list(tmp_path.rglob("*.sqlite"))
    assert not (tmp_path / "runtime/planner").exists()


@pytest.mark.parametrize("cash", [True, None, 1.1, "NaN", "Infinity", "0", "1.001", "1000000001"])
def test_virtual_capital_validation(tmp_path, cash):
    with pytest.raises(ValueError):
        paper.create_account(tmp_path, "fixture", cash)
    assert not list(tmp_path.rglob("account.json"))


@pytest.mark.parametrize("ident", ["../outside", "C:\\outside", "--replay", "", None])
def test_account_path_rejection(tmp_path, ident):
    with pytest.raises(ValueError):
        paper.account_state(tmp_path, ident)


def test_missing_and_synthetic_data_record_no_fills(tmp_path, clock):
    ident = paper.create_account(tmp_path, "fixture", "100000")["account_id"]
    missing = paper.advance(tmp_path, ident, "missing")
    assert missing["status"] == "waiting_data"
    synthetic = save_snapshot(make_demo_snapshot(3), tmp_path / "runtime/snapshots").name
    assert paper.preflight(tmp_path, ident, synthetic)["status"] == "waiting_data"
    state = paper.account_state(tmp_path, ident)
    assert state["sessions_count"] == 0 and state["fills"] == []
    assert state["cash"] == "100000.00"
    assert state["attempts"][0]["status"] == "waiting_data"
    assert not list(tmp_path.rglob("*.sqlite"))


def test_real_contract_preflight_is_read_only_and_advances_continuously(tmp_path, clock):
    ident = paper.create_account(tmp_path, "fixture", Decimal("100000"))["account_id"]
    snapshot = fixture_snapshot(tmp_path)
    checked = paper.preflight(tmp_path, ident, snapshot)
    assert checked["status"] == "ready", checked
    assert not list(tmp_path.rglob("*.sqlite"))
    first = paper.advance(tmp_path, ident, snapshot)
    assert first["status"] == "advanced", first
    assert paper.account_state(tmp_path, ident)["sessions_count"] == 1
    folder = tmp_path / "runtime/paper-accounts" / ident
    before = paper._read(folder)
    repeated_check = paper.preflight(tmp_path, ident, snapshot)
    assert repeated_check["status"] == "ready", repeated_check
    assert paper._read(folder) == before
    repeat = paper.advance(tmp_path, ident, snapshot)
    assert repeat["status"] == "already_processed"
    assert paper._read(folder) == before
    clock[0] = datetime.fromisoformat("2025-12-30T20:00:00+08:00")
    second = paper.advance(tmp_path, ident, snapshot)
    assert second["status"] == "advanced", second
    assert paper.account_state(tmp_path, ident)["sessions_count"] == 2


def test_report_failure_recovers_without_duplicate_session(tmp_path, clock, monkeypatch):
    ident = paper.create_account(tmp_path, "fixture", "100000")["account_id"]
    snapshot = fixture_snapshot(tmp_path)
    report = paper._report
    monkeypatch.setattr(paper, "_report", lambda *args: (_ for _ in ()).throw(OSError("fixture report failed")))
    result = paper.advance(tmp_path, ident, snapshot)
    assert result["status"] == "report_failed", result
    assert result["session_committed"] is True
    before = paper._read(tmp_path / "runtime/paper-accounts" / ident)
    monkeypatch.setattr(paper, "_report", report)
    recovered = paper.advance(tmp_path, ident, snapshot)
    assert recovered["status"] == "already_processed"
    assert paper._read(tmp_path / "runtime/paper-accounts" / ident) == before


def test_missed_session_and_version_change_do_not_touch_ledger(tmp_path, clock, monkeypatch):
    ident = paper.create_account(tmp_path, "fixture", "100000")["account_id"]
    snapshot = fixture_snapshot(tmp_path)
    assert paper.advance(tmp_path, ident, snapshot)["status"] == "advanced"
    folder = tmp_path / "runtime/paper-accounts" / ident
    before = paper._read(folder)
    clock[0] = datetime.fromisoformat("2025-12-31T20:00:00+08:00")
    skipped = paper.advance(tmp_path, ident, snapshot)
    assert skipped["status"] == "missed_session"
    assert paper.account_state(tmp_path, ident)["status"] == "missed_session"
    assert "漏过" in skipped["reasons"][0]
    monkeypatch.setattr(paper, "_rules", lambda: "changed")
    assert paper.advance(tmp_path, ident, snapshot)["status"] == "version_blocked"
    assert paper._read(folder) == before


def test_actual_advance_revalidates_after_advisory_check(tmp_path, clock, monkeypatch):
    ident = paper.create_account(tmp_path, "fixture", "100000")["account_id"]
    monkeypatch.setattr(paper, "preflight", lambda *args: {"status": "ready"})
    result = paper.advance(tmp_path, ident, "missing")
    assert result["status"] == "failed"
    assert not list(tmp_path.rglob("*.sqlite"))


def test_account_exclusive_lock_rejects_second_writer(tmp_path, clock):
    ident = paper.create_account(tmp_path, "fixture", "100000")["account_id"]
    folder = tmp_path / "runtime/paper-accounts" / ident
    with paper.exclusive_run(folder):
        with pytest.raises(RuntimeError):
            paper.advance(tmp_path, ident, "missing")
    assert paper.account_state(tmp_path, ident)["sessions_count"] == 0


def test_legacy_cli_cannot_create_or_advance_owned_account(tmp_path, clock, monkeypatch):
    from ashare_agent.cli import execute, parser

    monkeypatch.chdir(tmp_path)
    ident = paper.create_account(tmp_path, "fixture", "100000")["account_id"]
    snapshot = fixture_snapshot(tmp_path)
    folder = tmp_path / "runtime/paper-accounts" / ident
    args = parser().parse_args(["paper", "--snapshot", str(tmp_path / "runtime/snapshots" / snapshot),
                               "--date", "2025-12-29", "--ledger", str(folder / "account.sqlite")])
    with pytest.raises(ValueError, match="paper-account"):
        execute(args)
    assert not (folder / "account.sqlite").exists()
    elsewhere = tmp_path / "another-working-directory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    with pytest.raises(ValueError, match="paper-account"):
        execute(args)
    assert not (folder / "account.sqlite").exists()
    assert paper.advance(tmp_path, ident, snapshot)["status"] == "advanced"
    before = paper._read(folder)
    with pytest.raises(ValueError, match="paper-account"):
        execute(args)
    assert paper._read(folder) == before


def test_each_day_keeps_immutable_payload_archive(tmp_path, clock):
    ident = paper.create_account(tmp_path, "fixture", "100000")["account_id"]
    snapshot = fixture_snapshot(tmp_path)
    assert paper.advance(tmp_path, ident, snapshot)["status"] == "advanced"
    folder = tmp_path / "runtime/paper-accounts" / ident
    first = (folder / "run-2025-12-29.json").read_bytes()
    clock[0] = datetime.fromisoformat("2025-12-30T20:00:00+08:00")
    assert paper.advance(tmp_path, ident, snapshot)["status"] == "advanced"
    assert (folder / "run-2025-12-29.json").read_bytes() == first
    assert (folder / "run-2025-12-30.json").is_file()
