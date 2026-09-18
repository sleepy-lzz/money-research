from __future__ import annotations

import copy
import json

import pandas as pd
import pytest

import ashare_agent.forward_lab as forward_lab_module
from ashare_agent.forward_lab import ForwardLab
from ashare_agent.session_calendar import calendar_manifest, next_sessions

FREEZE_TIME = "2026-09-10T16:30:00+08:00"
DEFAULT_CALENDAR = calendar_manifest()


def _calendar_with_receipts(day):
    manifest = copy.deepcopy(DEFAULT_CALENDAR)
    for source in manifest["sources"]:
        source["fetched_at"] = f"{day}T15:00:00+08:00"
    return manifest


def _selection(day="2026-09-10", count=8, *, status="complete"):
    rows = []
    for index in range(count):
        rows.append(
            {
                "rank": index + 1,
                "ts_code": f"{600000 + index:06d}.SH",
                "name": f"Demo{index}",
                "score": 1.0 - index * 0.01,
                "momentum60": 0.50 + index * 0.01,
                "momentum120": 0.40 + index * 0.02,
            }
        )
    return {
        "mode": "CURRENT_RESEARCH",
        "status": status,
        "synthetic": False,
        "as_of": day,
        "started_at": f"{day}T16:00:00+08:00",
        "completed_at": f"{day}T16:10:00+08:00",
        "source_hash": "source-current-screen-v1",
        "calendar_manifest": _calendar_with_receipts(day),
        "parameters": {
            "top": 20,
            "min_amount": 100_000_000,
            "momentum": [60, 120],
            "trend": 120,
        },
        "receipts": [
            {
                "available_at": f"{day}T16:05:00+08:00",
                "fetched_at": f"{day}T16:05:00+08:00",
            }
        ],
        "coverage": {row["ts_code"]: {"news": "unknown", "community": "missing"} for row in rows},
        "candidates": rows,
    }


def _evidence(code="600000.SH", *, quality="aggregator_timestamp", risk_flags=None):
    return {
        "evidence_id": "e-" + code,
        "ts_code": code,
        "kind": "news",
        "quality": quality,
        "risk_flags": list(risk_flags or []),
        "published_at": "2026-09-10T15:00:00+08:00",
        "available_at": "2026-09-10T16:05:00+08:00",
        "fetched_at": "2026-09-10T16:05:00+08:00",
    }


def _sessions(count=20):
    return next_sessions("2026-09-10", count, DEFAULT_CALENDAR)


def _fixture_calendar():
    manifest = _calendar_with_receipts("2023-12-29")
    manifest["covered_start"] = "2024-01-01"
    manifest["covered_end"] = "2024-12-31"
    for source in manifest["sources"]:
        source["coverage_year"] = 2024
        source["published_at"] = "2023-12-01"
    manifest["rules"]["closed_periods"] = []
    return manifest


def _ohlc(days, *, start=100.0, growth=0.5):
    close = [start + index * growth for index in range(len(days))]
    return pd.DataFrame(
        {
            "trade_date": days,
            "open": [value - 0.25 for value in close],
            "close": close,
            "high": [value + 1 for value in close],
            "low": [value - 1 for value in close],
            "volume": [100_000] * len(days),
        }
    )


def _market_data(selection, *, days=None):
    days = days or _sessions()
    benchmark = _ohlc(["2026-09-10", *days], start=200, growth=0.25)
    histories = {}
    for index, candidate in enumerate(selection["candidates"]):
        raw = _ohlc(days, start=50 + index, growth=0.4 + index * 0.01)
        qfq = raw.copy()
        histories[candidate["ts_code"]] = {"raw": raw, "qfq": qfq}
    return benchmark, histories, days


def test_freeze_is_strict_idempotent_and_builds_single_factor_arms(tmp_path):
    selection = _selection(count=8)
    evidence = [
        _evidence(risk_flags=["重大风险"]),
        _evidence("600001.SH"),
        _evidence("600002.SH", quality="manual_unverified", risk_flags=["手工风险"]),
    ]
    lab = ForwardLab(tmp_path)
    try:
        first = lab.freeze(selection, FREEZE_TIME, evidence)
        same = lab.freeze(copy.deepcopy(selection), FREEZE_TIME, evidence)
        changed = copy.deepcopy(selection)
        changed["candidates"][1]["score"] = 0.01
        other = lab.freeze(changed, FREEZE_TIME, evidence)

        assert first["status"] == "frozen"
        assert same["status"] == "frozen" and same["idempotent"] is True
        assert other["status"] == "already_frozen"
        assert other["batch_id"] == first["batch_id"]
        arms = json.loads(lab.db.execute("SELECT arms_json FROM batches").fetchone()[0])
        assert "600000.SH" in arms["mechanical"]["codes"]
        assert "600000.SH" not in arms["news_guard"]["codes"]
        assert "600002.SH" in arms["news_guard"]["codes"]
        assert arms["mechanical"]["rows"][0]["source_coverage"]["news"] == "unknown"
        saved_selection = json.loads(lab.db.execute("SELECT selection_json FROM batches").fetchone()[0])
        assert saved_selection["receipts"] == selection["receipts"]
    finally:
        lab.close()


def test_rules_manifest_groups_parameters_and_context_identity_without_dates_or_data(tmp_path, monkeypatch):
    lab = ForwardLab(tmp_path)
    try:
        first_selection = _selection("2026-09-10", count=1)
        first = lab.freeze(first_selection, FREEZE_TIME)

        reordered = _selection("2026-09-11", count=1)
        reordered["parameters"] = {
            "trend": 120,
            "momentum": [60, 120],
            "min_amount": 100_000_000,
            "top": 20,
        }
        same_rules = lab.freeze(reordered, "2026-09-11T16:30:00+08:00")
        assert same_rules["rules_hash"] == first["rules_hash"]

        changed_parameters = _selection("2026-09-14", count=1)
        changed_parameters["parameters"]["top"] = 10
        changed = lab.freeze(changed_parameters, "2026-09-14T16:30:00+08:00")
        assert changed["rules_hash"] != first["rules_hash"]

        monkeypatch.setattr(forward_lab_module, "_context_rules_hash", lambda: "context-rules-v2")
        context_changed = _selection("2026-09-15", count=1)
        context_batch = lab.freeze(context_changed, "2026-09-15T16:30:00+08:00")
        assert context_batch["rules_hash"] not in {first["rules_hash"], changed["rules_hash"]}

        manifest = context_batch["rules_manifest"]
        assert manifest["selection_parameters"]["top"] == 20
        assert manifest["context_rules_hash"] == "context-rules-v2"
        assert manifest["forward_rules_code_hash"]
        assert manifest["calendar_code_hash"]
        assert manifest["calendar_manifest"]["revision"] == DEFAULT_CALENDAR["revision"]
        assert "as_of" not in json.dumps(manifest, ensure_ascii=False)
        stored_first = lab.db.execute(
            "SELECT rules_hash,rules_manifest_json FROM batches WHERE batch_id=?", (first["batch_id"],)
        ).fetchone()
        assert stored_first["rules_hash"] == first["rules_hash"]
        assert json.loads(stored_first["rules_manifest_json"])["context_rules_hash"] != "context-rules-v2"
        summaries = lab.summary()["batches"]
        assert all("rules_manifest" in batch and batch["legacy_rules"] is False for batch in summaries)
    finally:
        lab.close()


def test_legacy_batch_manifest_is_explicit_and_not_rewritten(tmp_path):
    lab = ForwardLab(tmp_path)
    try:
        lab.db.execute(
            "INSERT INTO batches(batch_id,as_of,created_at,input_hash,rules_hash,selection_json,arms_json,evidence_json,rules_manifest_json) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "legacy", "2026-09-01", "2026-09-01T16:30:00+08:00", "input", "old-rules", "{}", "{}", "[]",
                '{"legacy":true,"legacy_reason":"rules manifest unavailable in legacy batch"}',
            ),
        )
        lab.db.commit()
        before = lab.db.execute("SELECT rules_hash,rules_manifest_json FROM batches WHERE batch_id='legacy'").fetchone()
        summary = lab.summary()["batches"][0]
        after = lab.db.execute("SELECT rules_hash,rules_manifest_json FROM batches WHERE batch_id='legacy'").fetchone()
        assert summary["legacy_rules"] is True
        assert summary["rules_manifest"]["legacy"] is True
        assert (after["rules_hash"], after["rules_manifest_json"]) == (before["rules_hash"], before["rules_manifest_json"])
    finally:
        lab.close()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update(synthetic=True),
        lambda value: value.update(as_of="2026-09-09"),
        lambda value: value.update(source_hash=""),
        lambda value: value["receipts"][0].update(fetched_at="2026-09-11T16:00:00+08:00"),
        lambda value: value.update(completed_at="2026-09-10T16:40:00+08:00"),
    ],
)
def test_freeze_rejects_non_pit_or_unfinished_selection(tmp_path, mutator):
    selection = _selection()
    mutator(selection)
    lab = ForwardLab(tmp_path)
    try:
        with pytest.raises(ValueError):
            lab.freeze(selection, FREEZE_TIME)
    finally:
        lab.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fetched_at", "2026-09-10T16:31:00+08:00"),
        ("published_at", "2026-09-11"),
    ],
)
def test_freeze_rejects_calendar_source_unavailable_at_signal_time(tmp_path, field, value):
    selection = _selection()
    selection["calendar_manifest"]["sources"][0][field] = value
    lab = ForwardLab(tmp_path)
    try:
        with pytest.raises(ValueError, match="calendar_manifest 来源"):
            lab.freeze(selection, FREEZE_TIME)
    finally:
        lab.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("download_status", "not_downloaded"),
        ("content_hash", None),
        ("raw_html_path", None),
        ("fetched_at", None),
    ],
)
def test_freeze_rejects_incomplete_calendar_source_receipt(tmp_path, field, value):
    selection = _selection()
    selection["calendar_manifest"]["sources"][0][field] = value
    lab = ForwardLab(tmp_path)
    try:
        with pytest.raises(ValueError, match="calendar_manifest 来源"):
            lab.freeze(selection, FREEZE_TIME)
    finally:
        lab.close()


def test_freeze_allows_empty_complete_selection_and_empty_watch_pool(tmp_path):
    lab = ForwardLab(tmp_path)
    try:
        selection = _selection(count=0)
        frozen = lab.freeze(selection, FREEZE_TIME)
        assert frozen["status"] == "frozen"
        assert lab.watch_codes() == []
        summary = lab.summary()["batches"][0]
        assert summary["arms"]["mechanical"]["horizons"]["20"]["samples"] == 0
    finally:
        lab.close()


def test_observe_uses_open_to_close_prices_keeps_pending_denominator_and_summarises_overlap(tmp_path):
    selection = _selection(count=6)
    lab = ForwardLab(tmp_path)
    try:
        lab.freeze(selection, FREEZE_TIME)
        benchmark, histories, days = _market_data(selection)
        before_observation = lab.summary()["batches"][0]["arms"]["mechanical"]["horizons"]["20"]
        assert before_observation["samples"] == 5 and before_observation["pending"] == 5
        pending = lab.observe(days[3], benchmark.iloc[:5], histories, f"{days[3]}T16:30:00+08:00")
        assert pending["batches"][0]["observations"]
        assert {row["status"] for row in pending["batches"][0]["observations"]} == {"known", "pending"}
        assert lab.watch_codes()

        observed = lab.observe(days[-1], benchmark, histories, f"{days[-1]}T16:30:00+08:00")
        rows = observed["batches"][0]["observations"]
        assert all(row["status"] == "known" for row in rows)
        assert all(row["evaluation_rules_hash"] for row in rows)
        one = next(row for row in rows if row["arm"] == "mechanical" and row["horizon"] == 1)
        assert one["entry_day"] == days[0] and one["exit_day"] == days[0]
        assert one["entry_price"] == pytest.approx(histories[one["ts_code"]]["raw"].iloc[0]["open"])
        benchmark_entry = benchmark.iloc[1]
        assert one["benchmark_return"] == pytest.approx(benchmark_entry["close"] / benchmark_entry["open"] - 1)
        assert lab.watch_codes() == []
        arm = lab.summary()["batches"][0]["arms"]["mechanical"]
        assert set(arm["horizons"]["20"]) == {
            "samples", "known", "unknown", "pending", "avg_price_return", "avg_excess_return"
        }
        assert arm["horizons"]["20"]["known"] == arm["horizons"]["20"]["samples"]
        assert lab.summary()["batches"][0]["overlap_samples"]["mechanical|momentum60"]["count"] > 0
        assert lab.summary()["batches"][0]["evaluation_rules_hashes"]
        assert lab.summary()["alpha_claim"] is False
    finally:
        lab.close()


def test_observe_marks_missing_volume_qfq_factor_and_date_unknown_and_preserves_revision(tmp_path):
    selection = _selection(count=6)
    lab = ForwardLab(tmp_path)
    try:
        lab.freeze(selection, FREEZE_TIME)
        benchmark, histories, days = _market_data(selection)
        lab.observe(days[-1], benchmark, histories, f"{days[-1]}T16:30:00+08:00")
        target = selection["candidates"][0]["ts_code"]
        changed = copy.deepcopy(histories)
        changed[target]["raw"] = changed[target]["raw"].copy()
        changed[target]["raw"].iloc[5, changed[target]["raw"].columns.get_loc("close")] += 2
        revised = lab.observe(days[-1], benchmark, changed, f"{days[-1]}T16:30:00+08:00")
        assert any(row["revision"] == 2 for row in revised["batches"][0]["observations"])

        factor_bad = copy.deepcopy(histories)
        factor_bad[target]["qfq"] = factor_bad[target]["qfq"].copy()
        factor_bad[target]["qfq"].iloc[0, factor_bad[target]["qfq"].columns.get_loc("close")] *= 1.01
        factor_result = lab.observe(days[-1], benchmark, factor_bad, f"{days[-1]}T16:30:00+08:00")
        factor_rows = [row for row in factor_result["batches"][0]["observations"] if row["ts_code"] == target]
        assert factor_rows and all(row["status"] == "unknown" for row in factor_rows if row["horizon"] >= 5)

        bad = copy.deepcopy(histories)
        bad[target]["raw"] = bad[target]["raw"].copy()
        bad[target]["raw"].loc[days[0], "volume"] = 0
        bad[target]["qfq"] = bad[target]["qfq"].iloc[:-1].copy()
        bad_result = lab.observe(days[-1], benchmark, bad, f"{days[-1]}T16:30:00+08:00")
        target_rows = [row for row in bad_result["batches"][0]["observations"] if row["ts_code"] == target]
        assert target_rows and all(row["status"] == "unknown" for row in target_rows)
        assert target in lab.watch_codes()

        missing = dict(histories)
        missing.pop(target)
        before = lab.summary()
        lab.observe(days[-1], benchmark, missing, f"{days[-1]}T16:30:00+08:00")
        assert lab.summary()["batches"][0]["arms"] == before["batches"][0]["arms"]
    finally:
        lab.close()


def test_observe_requires_real_future_cutoff_and_empty_lab_is_safe(tmp_path):
    empty = ForwardLab(tmp_path / "empty")
    benchmark = _ohlc(_sessions(2), start=100, growth=1)
    try:
        empty_day = _sessions(2)[-1]
        assert empty.observe(empty_day, benchmark, {}, f"{empty_day}T16:30:00+08:00")["batches"] == []
    finally:
        empty.close()

    lab = ForwardLab(tmp_path / "frozen")
    try:
        lab.freeze(_selection(count=1), FREEZE_TIME)
        with pytest.raises(ValueError, match="16:00"):
            lab.observe(_sessions(1)[-1], benchmark, {}, "2026-09-11T15:59:00+08:00")
        with pytest.raises(ValueError, match="最后实际"):
            lab.observe(_sessions(1)[-1], benchmark, {}, "2026-09-11T16:30:00+08:00")
    finally:
        lab.close()


def test_missing_calendar_expected_benchmark_session_is_unknown_without_row_shift(tmp_path):
    selection = _selection(count=1)
    lab = ForwardLab(tmp_path)
    try:
        lab.freeze(selection, FREEZE_TIME)
        expected = _sessions()
        observed_days = ["2026-09-10", *[day for day in expected if day != expected[1]]]
        benchmark = _ohlc(observed_days, start=200, growth=0.25)
        _, histories, _ = _market_data(selection)
        result = lab.observe(expected[-1], benchmark, histories, f"{expected[-1]}T16:30:00+08:00")
        rows = result["batches"][0]["observations"]
        horizon5 = next(row for row in rows if row["arm"] == "mechanical" and row["horizon"] == 5)
        assert horizon5["status"] == "unknown"
        assert horizon5["entry_day"] == expected[0]
        assert horizon5["exit_day"] == expected[4]
        assert "benchmark 缺少日历预期交易日" in horizon5["reason"]
    finally:
        lab.close()


def test_calendar_coverage_limits_only_the_uncovered_horizon(tmp_path):
    selection = _selection("2026-12-15", count=1)
    lab = ForwardLab(tmp_path)
    try:
        lab.freeze(selection, "2026-12-15T16:30:00+08:00")
        days = next_sessions("2026-12-15", 5, selection["calendar_manifest"])
        benchmark = _ohlc(["2026-12-15", *days], start=200, growth=0.25)
        code = selection["candidates"][0]["ts_code"]
        raw = _ohlc(days, start=50, growth=0.4)
        result = lab.observe(days[-1], benchmark, {code: {"raw": raw, "qfq": raw.copy()}}, f"{days[-1]}T16:30:00+08:00")
        rows = [row for row in result["batches"][0]["observations"] if row["arm"] == "mechanical"]
        by_horizon = {row["horizon"]: row for row in rows}
        assert by_horizon[1]["status"] == "known"
        assert by_horizon[5]["status"] == "known"
        assert by_horizon[20]["status"] == "unknown"
        assert by_horizon[20]["entry_day"] == days[0]
        assert by_horizon[20]["exit_day"] is None
        assert "calendar_unknown" in by_horizon[20]["reason"]

        late_benchmark = _ohlc(["2026-12-15", *days, "2027-01-05"], start=200, growth=0.25)
        late = lab.observe(
            "2027-01-05", late_benchmark, {code: {"raw": raw, "qfq": raw.copy()}}, "2027-01-05T16:30:00+08:00"
        )
        late_rows = {
            row["horizon"]: row
            for row in late["batches"][0]["observations"]
            if row["arm"] == "mechanical"
        }
        assert late_rows[1]["status"] == "known"
        assert late_rows[5]["status"] == "known"
        assert late_rows[20]["status"] == "unknown"
    finally:
        lab.close()


def test_batch_without_calendar_manifest_cannot_publish_known(tmp_path):
    selection = _selection(count=1)
    lab = ForwardLab(tmp_path)
    try:
        lab.freeze(selection, FREEZE_TIME)
        batch = lab.db.execute("SELECT batch_id FROM batches").fetchone()[0]
        legacy_selection = copy.deepcopy(selection)
        legacy_selection.pop("calendar_manifest")
        lab.db.execute(
            "UPDATE batches SET selection_json=?,rules_manifest_json=? WHERE batch_id=?",
            (json.dumps(legacy_selection, ensure_ascii=False, sort_keys=True), '{"legacy":true,"legacy_reason":"calendar unavailable"}', batch),
        )
        lab.db.commit()
        benchmark, histories, days = _market_data(selection)
        result = lab.observe(days[-1], benchmark, histories, f"{days[-1]}T16:30:00+08:00")
        rows = result["batches"][0]["observations"]
        assert rows and all(row["status"] == "unknown" for row in rows)
        assert all("calendar_unknown" in row["reason"] for row in rows)
    finally:
        lab.close()


def test_pending_observation_rechecks_missing_history_on_next_session(tmp_path):
    lab = ForwardLab(tmp_path)
    try:
        selection = _selection(count=1)
        lab.freeze(selection, FREEZE_TIME)
        benchmark = _ohlc(["2026-09-10", "2026-09-11"], start=100, growth=1)
        first = lab.observe("2026-09-10", benchmark.iloc[:1], {}, "2026-09-10T16:30:00+08:00")
        assert {row["status"] for row in first["batches"][0]["observations"]} == {"pending"}

        next_day = lab.observe("2026-09-11", benchmark, {}, "2026-09-11T16:30:00+08:00")
        rows = next_day["batches"][0]["observations"]
        assert {row["status"] for row in rows if row["horizon"] == 1} == {"unknown"}
        assert {row["status"] for row in rows if row["horizon"] in {5, 20}} == {"pending"}
    finally:
        lab.close()


def test_observe_restores_a_previous_data_revision_after_an_intermediate_change(tmp_path):
    selection = _selection(count=1)
    lab = ForwardLab(tmp_path)
    try:
        lab.freeze(selection, FREEZE_TIME)
        benchmark, histories, days = _market_data(selection)
        lab.observe(days[-1], benchmark, histories, f"{days[-1]}T16:30:00+08:00")

        changed = copy.deepcopy(histories)
        target = selection["candidates"][0]["ts_code"]
        changed[target]["raw"] = changed[target]["raw"].copy()
        changed[target]["raw"].iloc[5, changed[target]["raw"].columns.get_loc("close")] += 2
        lab.observe(days[-1], benchmark, changed, f"{days[-1]}T16:30:00+08:00")
        restored = lab.observe(days[-1], benchmark, histories, f"{days[-1]}T16:30:00+08:00")

        row = next(
            item
            for item in restored["batches"][0]["observations"]
            if item["arm"] == "mechanical" and item["ts_code"] == target and item["horizon"] == 20
        )
        assert row["revision"] == 3
        assert lab.db.execute(
            "SELECT count(*) FROM observations WHERE arm='mechanical' AND ts_code=? AND horizon=20",
            (target,),
        ).fetchone()[0] == 3
    finally:
        lab.close()


def test_completed_old_batch_without_watch_preserves_known_same_code_and_new_batch(tmp_path):
    lab = ForwardLab(tmp_path)
    try:
        old_selection = _selection("2024-01-01", count=1)
        old_selection["calendar_manifest"] = _fixture_calendar()
        old_freeze = lab.freeze(old_selection, "2024-01-01T16:30:00+08:00")
        old_days = next_sessions("2024-01-01", 20, _fixture_calendar())
        old_benchmark = _ohlc(["2024-01-01", *old_days], start=100, growth=0.2)
        old_code = old_selection["candidates"][0]["ts_code"]
        old_histories = {old_code: {"raw": _ohlc(old_days), "qfq": _ohlc(old_days)}}
        old_observation = lab.observe(old_days[-1], old_benchmark, old_histories, f"{old_days[-1]}T16:30:00+08:00")
        assert old_observation["batches"][0]["batch_id"] == old_freeze["batch_id"]
        assert lab.watch_codes() == []

        new_selection = _selection("2026-09-10", count=1)
        new_freeze = lab.freeze(new_selection, FREEZE_TIME)
        new_benchmark, new_histories, new_days = _market_data(new_selection)
        current = lab.observe(new_days[-1], new_benchmark, new_histories, f"{new_days[-1]}T16:30:00+08:00")

        by_batch = {batch["batch_id"]: batch for batch in current["batches"]}
        assert set(by_batch) == {old_freeze["batch_id"], new_freeze["batch_id"]}
        old_h1 = next(
            row for row in by_batch[old_freeze["batch_id"]]["observations"]
            if row["arm"] == "mechanical" and row["horizon"] == 1
        )
        assert old_h1["status"] == "known"
        assert all(row["status"] == "known" for row in by_batch[new_freeze["batch_id"]]["observations"])
    finally:
        lab.close()
