import json
import subprocess
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from ashare_agent import context_feed, daily_lab
from ashare_agent.current_data import SHANGHAI
from ashare_agent.session_calendar import calendar_manifest


class Morning(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 9, 11, 10, 0, tzinfo=SHANGHAI)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def test_daily_lock_prevents_two_writers_and_releases_after_failure(tmp_path):
    with pytest.raises(ValueError):
        with daily_lab.exclusive_run(tmp_path):
            with pytest.raises(RuntimeError, match="已在运行"):
                with daily_lab.exclusive_run(tmp_path):
                    pytest.fail("Two writers acquired the daily lock")
            raise ValueError("fixture failure")
    with daily_lab.exclusive_run(tmp_path):
        pass


def test_morning_run_writes_deferred_report_without_network_or_personal_account(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_lab, "datetime", Morning)
    monkeypatch.setattr(
        daily_lab, "check_software", lambda root, output: {"passed": True, "summary": "fixture"}
    )
    monkeypatch.setattr(
        daily_lab, "CurrentClient", lambda *a: pytest.fail("Morning must not fetch market data")
    )
    result = daily_lab.run(tmp_path)
    assert result["status"] == "deferred"
    assert (tmp_path / "runtime/daily-lab/latest.html").is_file()
    assert (Path(result["output"]) / "report.md").is_file()
    assert not (tmp_path / "runtime/planner").exists()
    assert result['runner_hash'] and result['runner_path']
    journal = json.loads((tmp_path / 'runtime/daily-lab/latest-run.json').read_text(encoding='utf-8'))
    assert journal['status'] == 'finished' and journal['outcome'] == 'deferred'


def test_journal_preserves_interruption_and_next_run_recovers(tmp_path):
    folder = tmp_path / 'lab'
    with pytest.raises(KeyboardInterrupt):
        with daily_lab.run_journal(folder, folder / 'first') as checkpoint:
            checkpoint('selection')
            raise KeyboardInterrupt()
    failed = json.loads((folder / 'first/run-state.json').read_text(encoding='utf-8'))
    assert failed['status'] == 'failed' and failed['stage'] == 'interrupted'
    # A hard process kill cannot run finally; the next lock owner detects the stale journal.
    daily_lab.atomic_text(folder / 'latest-run.json', json.dumps(dict(status='running', stage='context', output='old')))
    with daily_lab.run_journal(folder, folder / 'next'):
        daily_lab.atomic_text(folder / 'next/result.json', json.dumps({'status': 'partial'}))
    recovered = json.loads((folder / 'latest-run.json').read_text(encoding='utf-8'))
    assert recovered['previous_unfinished']['status'] == 'interrupted'
    assert recovered['previous_unfinished']['last_stage'] == 'context'
    assert recovered['outcome'] == 'partial'
    assert json.loads((folder / 'first/run-state.json').read_text(encoding='utf-8')) == failed


def test_failed_checks_block_network_and_do_not_create_frozen_samples(tmp_path, monkeypatch):
    monkeypatch.setattr(
        daily_lab, "check_software", lambda root, output: {"passed": False, "summary": "fixture failure"}
    )
    monkeypatch.setattr(
        daily_lab, "CurrentClient", lambda *a: pytest.fail("Failed tests must stop network work")
    )
    result = daily_lab.run(tmp_path)
    assert result["status"] == "blocked"
    assert result["checks"]["passed"] is False
    assert "检查未通过" in result["notes"][0]


def test_report_escapes_untrusted_news_and_preserves_historical_files(tmp_path):
    root = tmp_path / "reports"
    output = root / "first"
    output.mkdir(parents=True)
    result = {
        "status": "blocked",
        "as_of": "2026-09-11",
        "created_at": "2026-09-11T17:30:00+08:00",
        "notes": ["<script>alert('bad')</script>"],
        "lab": {"label": "<img onerror=bad>"},
    }
    daily_lab.write_report(root, output, result)
    old = (output / "report.html").read_text(encoding="utf-8")
    assert "<script>" not in old and "&lt;script&gt;" in old
    other = root / "second"
    other.mkdir()
    daily_lab.write_report(root, other, {**result, "notes": ["next day"]})
    assert (output / "report.html").read_text(encoding="utf-8") == old


@pytest.mark.parametrize("failure", [None, "screen", "context", "observation", "quotes"])
def test_two_daily_runs_isolate_new_freeze_and_old_observation(tmp_path, monkeypatch, failure):
    class Clock(datetime):
        day = "2026-09-10"

        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat(cls.day + "T17:30:00+08:00")
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    def frame():
        days = [d for d in ["2026-09-10", "2026-09-11"] if d <= Clock.day]
        prices = [100.0, 104.0][: len(days)]
        return pd.DataFrame(
            {
                "trade_date": days,
                "open": [p - 1 for p in prices],
                "close": prices,
                "high": [p + 1 for p in prices],
                "low": [p - 2 for p in prices],
                "volume": [100000] * len(days),
                "amount": [p * 100000 for p in prices],
            },
            index=days,
        )

    class Client:
        def __init__(self, root):
            self.receipts = []

        def close(self):
            pass

        def bars(self, code, adjust="", end=None):
            return frame(), "测试股票"

        def quotes(self, codes):
            if Clock.day == "2026-09-11" and failure == "quotes":
                raise RuntimeError("quotes fixture failure")
            last = frame().iloc[-1]
            return {
                code: {
                    "name": "测试股票",
                    "date": Clock.day,
                    "observed_at": Clock.day + "T15:30:00+08:00",
                    **{k: float(last[k]) for k in ["open", "high", "low", "close", "volume", "amount"]},
                }
                for code in codes
            }

    def selection(output, progress=None):
        if Clock.day == "2026-09-11" and failure == "screen":
            raise RuntimeError("screen fixture failure")
        # Explicit offline fixture, not a claim of historical possession of the real bundle.
        calendar = calendar_manifest()
        for source in calendar["sources"]:
            source["fetched_at"] = "2026-09-09T16:00:00+08:00"
        return {
            "mode": "CURRENT_RESEARCH",
            "status": "complete",
            "synthetic": False,
                "source_hash": "isolated-test-source",
                "parameters": {"top": 20, "min_amount": 100000000, "momentum": [60, 120], "trend": 120},
                "calendar_manifest": calendar,
            "as_of": Clock.day,
            "started_at": Clock.day + "T16:00:00+08:00",
            "completed_at": Clock.day + "T16:20:00+08:00",
            "receipts": [
                {"available_at": Clock.day + "T16:10:00+08:00", "fetched_at": Clock.day + "T16:10:00+08:00"}
            ],
            "candidates": [
                {
                    "ts_code": "600000.SH",
                    "name": "测试股票",
                    "rank": 1,
                    "score": 1,
                    "momentum60": 0.2,
                    "momentum120": 0.3,
                }
            ],
        }

    monkeypatch.setattr(daily_lab, "datetime", Clock)
    monkeypatch.setattr(
        daily_lab, "check_software", lambda root, output: {"passed": True, "summary": "fixture"}
    )
    monkeypatch.setattr(daily_lab, "CurrentClient", Client)
    monkeypatch.setattr(daily_lab, "screen", selection)
    def context(codes, client, cutoff):
        if Clock.day == "2026-09-11" and failure == "context":
            raise RuntimeError("context fixture failure")
        return {
            "evidence": [],
            "warnings": [],
            "coverage": {c: {"news": "unknown", "community": "missing"} for c in codes},
        }

    monkeypatch.setattr(context_feed, "fetch_context", context)
    first = daily_lab.run(tmp_path)
    assert first["status"] == "partial", first["notes"]  # legacy-only fixture cannot certify v2
    assert first["research_v2"]["status"] == "blocked"
    assert first["lab"]["batches"][0]["arms"]["mechanical"]["horizons"]["1"]["pending"] == 1
    first_id = first["freeze"]["batch_id"]
    Clock.day = "2026-09-11"
    if failure == "observation":
        from ashare_agent.forward_lab import ForwardLab

        def bad_observe(*args):
            raise RuntimeError("observation fixture failure")

        monkeypatch.setattr(ForwardLab, "observe", bad_observe)
    second = daily_lab.run(tmp_path)
    assert second["status"] == "partial", second["notes"]
    old = next(batch for batch in second["lab"]["batches"] if batch["batch_id"] == first_id)
    observed = old["arms"]["mechanical"]["horizons"]["1"]
    if failure in {None, "screen", "context"}:
        assert observed["known"] == 1
        assert observed["avg_price_return"] == pytest.approx(104 / 103 - 1)
        assert observed["avg_excess_return"] == pytest.approx(0)
    elif failure == "quotes":
        assert observed["unknown"] == 1
        assert second["phases"]["observation"] == "partial"
    else:
        assert second["phases"]["observation"] == "failed"
        assert observed["known"] == 0
    artifact = json.loads((Path(second["output"]) / "observations.json").read_text(encoding="utf-8"))
    assert artifact.get("batches") or artifact.get("status") == "failed"
    if failure == "screen":
        assert "freeze" not in second
        assert len(second["lab"]["batches"]) == 1
        assert second["phases"]["selection"] == "failed"
    else:
        assert len(second["lab"]["batches"]) == 2
        assert second["freeze"]["status"] == "frozen"
    assert not (tmp_path / "runtime/planner").exists()


def test_check_timeout_preserves_partial_log_and_fails(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("pytest", 180, output=b"partial test progress")

    monkeypatch.setattr(daily_lab.subprocess, "run", timeout)
    result = daily_lab.check_software(tmp_path, tmp_path)
    assert result["passed"] is False
    log = (tmp_path / "checks.log").read_text(encoding="utf-8")
    assert "partial test progress" in log and "超时" in log
