import json
from copy import deepcopy

import pandas as pd
import pytest
from test_data import _FakeTushare
from test_execution_ledger import SEC, intent, market

from ashare_agent.config import ExecutionConfig, Settings
from ashare_agent.data import Snapshot, TushareDownloader, make_demo_snapshot, validate_snapshot
from ashare_agent.engine import Engine
from ashare_agent.execution import simulate_open
from ashare_agent.hardening import (
    active_inventory,
    add_valuations,
    check_receipt,
    content_hash,
    ingest_receipt,
    session_payload,
)
from ashare_agent.strategy import _liquidity, rank_candidates


def archived(payload, day):
    return dict(
        payload=payload,
        content_hash=content_hash(payload),
        source="independently archived fixture",
        published_at=day + "T18:00:00+08:00",
        fetched_at=day + "T18:01:00+08:00",
        available_at=day + "T18:01:00+08:00",
    )


def real_contract_fixture():
    s = make_demo_snapshot(3)
    s.metadata["synthetic"] = False
    requests = []
    for endpoint in TushareDownloader.ENDPOINTS:
        keys = (
            ["status_L", "status_D", "status_P"]
            if endpoint == "stock_basic"
            else (
                [d.replace("-", "") for d in s.calendar[:-1]]
                if endpoint in TushareDownloader.DAILY_ENDPOINTS
                else ["range"]
            )
        )
        for key in keys:
            requests.append(
                dict(
                    endpoint=endpoint,
                    status="complete",
                    sha256="a" * 64,
                    revision="fixture",
                    fetched_at="2025-12-31T20:00:00+08:00",
                    logical_key=key,
                    params={"list_status": key[-1]} if endpoint == "stock_basic" else {},
                )
            )
    s.metadata["raw_download"] = dict(status="complete", request_manifest=requests)
    s.metadata["inventory_receipts"] = {}
    s.metadata["session_receipts"] = {}
    for d in s.calendar[:-1]:
        s.metadata["inventory_receipts"][d] = archived(
            dict(trade_date=d, complete_inventory=active_inventory(s, d), scope="SSE_SZSE_MAIN"), d
        )
        s.metadata["session_receipts"][d] = archived(session_payload(s, d), d)
    return s


def test_real_data_needs_archived_inputs_not_just_booleans():
    s = real_contract_fixture()
    assert validate_snapshot(s)["tradable"]
    changed = deepcopy(s)
    changed.metadata.pop("inventory_receipts")
    assert not validate_snapshot(changed)["tradable"]
    changed = deepcopy(s)
    changed.metadata["raw_download"]["request_manifest"] = [
        r
        for r in changed.metadata["raw_download"]["request_manifest"]
        if r["params"].get("list_status") != "D"
    ]
    assert not validate_snapshot(changed)["tradable"]


def test_omitting_a_historical_security_breaks_independent_inventory_receipt():
    s = real_contract_fixture()
    code = s.securities.ts_code.iloc[0]
    omitted = Snapshot(
        s.market.loc[s.market.ts_code != code],
        s.securities.loc[s.securities.ts_code != code],
        s.calendar,
        s.actions,
        s.benchmarks,
        s.metadata,
    )
    assert not validate_snapshot(omitted)["tradable"]


def test_receipts_bind_benchmark_and_actual_acquisition_time():
    s = real_contract_fixture()
    s.benchmarks.loc[0, "close"] *= 2
    assert not validate_snapshot(s)["tradable"]
    payload = dict(trade_date="2020-01-02")
    receipt = ingest_receipt(payload, "2020-01-02T18:00:00+08:00", "original source")
    with pytest.raises(ValueError, match="cutoff"):
        check_receipt(receipt, payload, "2020-01-02T23:59:59+08:00")
    s = real_contract_fixture()
    d = s.calendar[0]
    s.metadata["session_receipts"][d]["available_at"] = "2030-01-01T00:00:00Z"
    assert not validate_snapshot(s)["tradable"]


class HaltFake(_FakeTushare):
    def daily(self, **kwargs):
        frame = super().daily(**kwargs)
        return frame.loc[frame.trade_date != "20250103"]

    def suspend_d(self, **kwargs):
        if kwargs["trade_date"] == "20250103":
            return pd.DataFrame([dict(ts_code="600001.SH", trade_date="20250103", suspend_type="S")])
        return pd.DataFrame(columns=["ts_code", "trade_date", "suspend_type"])


def test_halt_spine_keeps_null_ohlc_and_separate_stale_valuation(tmp_path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    pd.DataFrame([dict(ts_code="600001.SH", trade_date="2025-01-03", full_day=True)]).to_csv(
        evidence_dir / "suspensions.csv", index=False
    )
    dl = TushareDownloader(tmp_path / "raw", client=HaltFake(), min_request_interval=0)
    s = dl.build_snapshot("2025-01-02", "2025-01-03", evidence_dir)
    row = s.market.iloc[-1]
    assert row.suspended and not row.tradeable and row.session_state_known
    assert row[["open", "high", "low", "close"]].isna().all()
    assert row.volume == 0 and row.valuation_price == 10.5 and row.valuation_date == "2025-01-02"
    unknown = dl.build_snapshot("2025-01-02", "2025-01-03")
    assert not unknown.market.iloc[-1].session_state_known and not validate_snapshot(unknown)["tradable"]
    actions = pd.DataFrame([dict(ts_code="600001.SH", ex_date="2025-01-03")])
    assert pd.isna(add_valuations(s.market, actions).iloc[-1].valuation_price)


def test_halt_zero_volume_counts_in_calendar_adv():
    rows = pd.DataFrame(dict(volume=[1000] * 20, amount=[10000] * 20, suspended=[False] * 10 + [True] * 10))
    assert _liquidity(rows) == (500, 5000)


def test_shared_product_rule_excludes_high_momentum_non_mainboard():
    s = make_demo_snapshot(260)
    code = s.securities.ts_code.iloc[0]
    before = rank_candidates(s, s.calendar[-2], Settings().strategy.model_dump())
    s.securities.loc[s.securities.ts_code == code, "board"] = "STAR"
    after = rank_candidates(s, s.calendar[-2], Settings().strategy.model_dump())
    target = after.set_index("ts_code").loc[code]
    assert not target.eligible and pd.isna(target["rank"])
    assert not simulate_open(
        intent(), market(), dict(board="STAR", exchange="SSE"), ExecutionConfig()
    ).allowed
    assert len(before) == len(after)


def test_real_fills_require_opening_evidence_not_daily_volume():
    cfg = ExecutionConfig()
    assert simulate_open(intent(), market(), SEC, cfg).allowed
    assert not simulate_open(intent(), market(volume=10**9), SEC, cfg, require_opening_evidence=True).allowed
    good = market(opening_volume=1_000_000, opening_observed_at="2024-01-03T09:25:00+08:00")
    assert simulate_open(intent(), good, SEC, cfg, require_opening_evidence=True).allowed
    assert not simulate_open(
        intent(),
        {**good, "opening_observed_at": "2024-01-03T15:00:00+08:00"},
        SEC,
        cfg,
        require_opening_evidence=True,
    ).allowed


def test_same_day_late_revision_does_not_enter_earlier_decision():
    s = make_demo_snapshot(260)
    date = s.calendar[-2]
    s.market.loc[s.market.trade_date == date, "available_at"] = date + "T22:00:00+08:00"
    result = rank_candidates(
        s, date, Settings().strategy.model_dump(), decision_cutoff=date + "T19:00:00+08:00"
    )
    assert not result.eligible.any()


def test_snapshot_with_missing_held_delisted_stock_stops_instead_of_writing_off():
    s = make_demo_snapshot(3)
    engine = Engine(s, Settings())
    d = s.calendar[1]
    code = s.securities.ts_code.iloc[0]
    engine.ledger.db.execute(
        "INSERT INTO lots VALUES(?,?,?,?,?,?,?)", ("old", code, 100, 10000, s.calendar[0], d, "BANK")
    )
    engine.market_by_date[d].pop(code)
    with pytest.raises(ValueError, match="delisting resolution"):
        engine.process_day(d)
    engine.close()


def test_failed_delisted_endpoint_cannot_be_hidden_by_successful_live_endpoint(tmp_path):
    class FailD(_FakeTushare):
        def stock_basic(self, **kwargs):
            if kwargs["list_status"] == "D":
                raise RuntimeError("fixture denied D")
            return super().stock_basic(**kwargs)

    dl = TushareDownloader(tmp_path, client=FailD(), min_request_interval=0, max_retries=1)
    result = dl.download("2025-01-02", "2025-01-03")
    assert result["status"] == "partial" and not result["endpoint_completeness"]["stock_basic"]["complete"]
    failed = [r for r in result["request_manifest"] if r.get("params", {}).get("list_status") == "D"]
    assert len(failed) == 1 and failed[0]["status"] == "failed"


def test_incomplete_calendar_cannot_silently_remove_a_session(tmp_path):
    class MissingDay(_FakeTushare):
        def trade_cal(self, **kwargs):
            return super().trade_cal(**kwargs).iloc[1:]

    dl = TushareDownloader(tmp_path, client=MissingDay(), min_request_interval=0)
    result = dl.download("2025-01-02", "2025-01-03")
    assert result["status"] == "partial"
    assert not result["endpoint_completeness"]["trade_cal"]["complete"]
    assert not result["endpoint_completeness"]["daily"]["complete"]


def test_raw_tamper_and_conflicting_benchmark_revision_fail(tmp_path):
    dl = TushareDownloader(tmp_path, client=_FakeTushare(), min_request_interval=0)
    dl.download("2025-01-02", "2025-01-03")
    r = dl._latest_record("daily", "20250102")
    r["data_path"].write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="hash mismatch"):
        dl._load_raw("daily", "2025-01-02", "2025-01-03")
    changed = _FakeTushare().index_daily()
    changed.loc[0, "close"] = 9000
    dl._write_cache("index_daily", "range_20250101_20250103", changed)
    with pytest.raises(ValueError, match="conflicting overlapping"):
        dl._load_raw("index_daily", "2025-01-02", "2025-01-03")


def test_expired_metadata_refresh_failure_is_not_success(tmp_path):
    dl = TushareDownloader(tmp_path, client=_FakeTushare(), min_request_interval=0, max_retries=1)
    dl.download("2025-01-02", "2025-01-03")
    latest = dl._latest_record("stock_basic", "status_L")
    manifest = latest["manifest"]
    manifest["fetched_at"] = "2000-01-01T00:00:00+00:00"
    latest["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")

    class Denied(_FakeTushare):
        def stock_basic(self, **kwargs):
            raise RuntimeError("fixture cannot refresh")

    dl._client = Denied()
    result = dl.download("2025-01-02", "2025-01-03")
    assert result["status"] == "partial"
    assert dl._latest_record("stock_basic", "status_L")["manifest"]["status"] == "failed"
    assert len(dl._revision_records("stock_basic", "status_L")) == 2


def test_stock_pages_cover_all_rows_and_bound_nonadvancing_provider(tmp_path):
    class Paged(_FakeTushare):
        def stock_basic(self, **kwargs):
            rows = pd.DataFrame(
                [dict(ts_code=f"60000{i}.SH", list_date="20000101", delist_date=None) for i in range(5)]
            )
            return rows.iloc[kwargs["offset"] : kwargs["offset"] + kwargs["limit"]]

    dl = TushareDownloader(tmp_path, client=Paged(), row_limit=2, min_request_interval=0)
    assert len(dl._stock_pages("2025-01-02", "2025-01-03", "L")) == 5
    assert len(dl._request_trace) == 3
    dl._client = _FakeTushare()
    dl.row_limit = 1
    with pytest.raises(RuntimeError, match="did not advance"):
        dl._stock_pages("2025-01-02", "2025-01-03", "L")
