from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from ashare_agent.data import (
    ACTION_COLUMNS,
    Snapshot,
    TushareDownloader,
    load_snapshot,
    make_demo_snapshot,
    save_snapshot,
    validate_snapshot,
)


def test_demo_has_history_and_future_calendar_session() -> None:
    snapshot = make_demo_snapshot()
    result = validate_snapshot(snapshot)

    assert result["errors"] == []
    assert result["tradable"] is True
    assert len(snapshot.calendar) == 521
    assert snapshot.calendar[-1] > snapshot.market["trade_date"].max()
    assert snapshot.market["ts_code"].nunique() >= 12
    assert snapshot.market["state_known"].all()
    assert snapshot.market["industry_known"].all()


def test_save_load_hashes_and_duckdb_query(tmp_path: Path) -> None:
    snapshot = make_demo_snapshot(sessions=12)
    path = save_snapshot(snapshot, tmp_path / "snapshots")
    loaded = load_snapshot(path)

    assert loaded.snapshot_id == snapshot.snapshot_id
    result = loaded.sql("SELECT COUNT(*) AS n FROM market")
    assert int(result.loc[0, "n"]) == len(snapshot.market)
    assert save_snapshot(snapshot, tmp_path / "snapshots") == path

    market_path = path / "market.parquet"
    market_path.write_bytes(market_path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_snapshot(path)


@pytest.mark.parametrize("offset", [-1, -100])
def test_missing_benchmark_session_blocks_trading_and_current_regime(offset):
    from ashare_agent.strategy import market_regime

    snapshot = make_demo_snapshot(sessions=260)
    original = snapshot.benchmarks
    missing_day = sorted(original.trade_date.unique())[offset]
    changed = replace(snapshot, benchmarks=original.loc[original.trade_date != missing_day])
    quality = validate_snapshot(changed)
    assert not quality["tradable"]
    assert any("benchmark" in warning and "missing" in warning for warning in quality["warnings"])
    assert market_regime(changed, str(snapshot.market.trade_date.max()))["regime"] == "unknown"


def test_benchmark_not_yet_available_is_not_replaced_with_yesterday():
    from ashare_agent.strategy import market_regime

    snapshot = make_demo_snapshot(sessions=260)
    day = str(snapshot.market.trade_date.max())
    snapshot.benchmarks.loc[snapshot.benchmarks.trade_date == day, "available_at"] = day + "T22:00:00+08:00"
    result = market_regime(snapshot, day, decision_cutoff=day + "T17:00:00+08:00")
    assert result["regime"] == "unknown"


def test_unknown_status_can_be_saved_as_research_only(tmp_path: Path) -> None:
    snapshot = make_demo_snapshot(sessions=3)
    market = snapshot.market.copy()
    market.loc[0, "state_known"] = False
    unknown = Snapshot(
        market=market,
        securities=snapshot.securities,
        calendar=snapshot.calendar,
        actions=snapshot.actions,
        benchmarks=snapshot.benchmarks,
        metadata={**snapshot.metadata, "snapshot_id": "unknown-status"},
    )

    result = validate_snapshot(unknown)
    assert result["tradable"] is False
    path = save_snapshot(unknown, tmp_path / "snapshots")
    assert load_snapshot(path).snapshot_id == "unknown-status"


def test_missing_active_rows_are_a_quality_gap_not_suspensions() -> None:
    snapshot = make_demo_snapshot(sessions=3)
    market = snapshot.market.loc[
        ~(
            (snapshot.market["ts_code"] == "600001.SH")
            & (snapshot.market["trade_date"] == snapshot.market["trade_date"].min())
        )
    ].copy()
    partial = Snapshot(
        market=market,
        securities=snapshot.securities,
        calendar=snapshot.calendar,
        actions=snapshot.actions,
        benchmarks=snapshot.benchmarks,
        metadata=snapshot.metadata,
    )
    result = validate_snapshot(partial)
    assert result["tradable"] is False
    assert any(
        "missing" in warning and "active security/session" in warning for warning in result["warnings"]
    )


def test_action_without_known_at_disables_formal_trading() -> None:
    snapshot = make_demo_snapshot(sessions=3)
    actions = pd.DataFrame(
        [
            {
                "action_id": "a1",
                "ts_code": "600001.SH",
                "record_date": "2025-12-01",
                "ex_date": "2025-12-02",
                "pay_date": "2025-12-03",
                "share_list_date": "2025-12-04",
                "cash_per_share": 0.1,
                "bonus_ratio": 0.0,
                "known_at": None,
            }
        ],
        columns=ACTION_COLUMNS,
    )
    with_action = Snapshot(
        market=snapshot.market,
        securities=snapshot.securities,
        calendar=snapshot.calendar,
        actions=actions,
        benchmarks=snapshot.benchmarks,
        metadata=snapshot.metadata,
    )
    result = validate_snapshot(with_action)
    assert result["tradable"] is False
    assert any("known_at" in warning for warning in result["warnings"])


def test_downloader_without_token_does_not_claim_ready(tmp_path: Path) -> None:
    downloader = TushareDownloader(tmp_path / "data", token=None)
    result = downloader.download("2025-01-02", "2025-01-03")

    assert result["status"] == "unavailable"
    assert result["trading_ready"] is False
    assert result["files"] == {}
    assert downloader.capabilities("2025-01-02")["token_configured"] is False


class _FakeTushare:
    def daily(self, **_: object) -> pd.DataFrame:
        frame = pd.DataFrame(
            [
                {
                    "ts_code": "600001.SH",
                    "trade_date": "20250102",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 1000,
                    "amount": 20,
                },
                {
                    "ts_code": "600001.SH",
                    "trade_date": "20250103",
                    "open": 10.5,
                    "high": 11,
                    "low": 10,
                    "close": 10.8,
                    "vol": 1200,
                    "amount": 22,
                },
            ]
        )
        return frame.loc[frame.trade_date.eq(_.get("trade_date"))] if _.get("trade_date") else frame

    def adj_factor(self, **_: object) -> pd.DataFrame:
        frame = pd.DataFrame(
            {
                "ts_code": ["600001.SH", "600001.SH"],
                "trade_date": ["20250102", "20250103"],
                "adj_factor": [1.0, 1.0],
            }
        )
        return frame.loc[frame.trade_date.eq(_.get("trade_date"))] if _.get("trade_date") else frame

    def stk_limit(self, **_: object) -> pd.DataFrame:
        frame = pd.DataFrame(
            {
                "ts_code": ["600001.SH", "600001.SH"],
                "trade_date": ["20250102", "20250103"],
                "up_limit": [11, 11.55],
                "down_limit": [9, 9.45],
            }
        )
        return frame.loc[frame.trade_date.eq(_.get("trade_date"))] if _.get("trade_date") else frame

    def suspend_d(self, **_: object) -> pd.DataFrame:
        return pd.DataFrame(columns=["ts_code", "trade_date"])

    def trade_cal(self, **_: object) -> pd.DataFrame:
        dates = pd.date_range(str(_["start_date"]), str(_["end_date"]))
        return pd.DataFrame(
            {"cal_date": dates.strftime("%Y%m%d"), "is_open": (dates.weekday < 5).astype(int)}
        )

    def stock_basic(self, **_: object) -> pd.DataFrame:
        frame = pd.DataFrame(
            {"ts_code": ["600001.SH"], "list_date": ["20100101"], "delist_date": [None], "name": ["Demo"]}
        )
        return frame if _.get("list_status", "L") == "L" else frame.iloc[:0]

    def dividend(self, **_: object) -> pd.DataFrame:
        return pd.DataFrame(
            columns=[
                "ts_code",
                "record_date",
                "ex_date",
                "pay_date",
                "div_list_date",
                "cash_div_tax",
                "stk_div",
                "ann_date",
            ]
        )

    def index_daily(self, **_: object) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "ts_code": ["000300.SH", "000300.SH"],
                "trade_date": ["20250102", "20250103"],
                "close": [4000, 4010],
            }
        )


class _RecordingFake(_FakeTushare):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _record(self, name: str, kwargs: dict[str, object]) -> None:
        self.calls.append((name, kwargs))

    def daily(self, **kwargs: object) -> pd.DataFrame:
        self._record("daily", kwargs)
        return super().daily(**kwargs)

    def adj_factor(self, **kwargs: object) -> pd.DataFrame:
        self._record("adj_factor", kwargs)
        return super().adj_factor(**kwargs)

    def stk_limit(self, **kwargs: object) -> pd.DataFrame:
        self._record("stk_limit", kwargs)
        return super().stk_limit(**kwargs)

    def suspend_d(self, **kwargs: object) -> pd.DataFrame:
        self._record("suspend_d", kwargs)
        return super().suspend_d(**kwargs)

    def trade_cal(self, **kwargs: object) -> pd.DataFrame:
        self._record("trade_cal", kwargs)
        return super().trade_cal(**kwargs)

    def stock_basic(self, **kwargs: object) -> pd.DataFrame:
        self._record("stock_basic", kwargs)
        return super().stock_basic(**kwargs)

    def dividend(self, **kwargs: object) -> pd.DataFrame:
        self._record("dividend", kwargs)
        return super().dividend(**kwargs)

    def index_daily(self, **kwargs: object) -> pd.DataFrame:
        self._record("index_daily", kwargs)
        return super().index_daily(**kwargs)


def test_build_snapshot_requires_explicit_pit_evidence(tmp_path: Path) -> None:
    downloader = TushareDownloader(
        tmp_path / "data", token="test-token", client=_FakeTushare(), min_request_interval=0
    )
    snapshot = downloader.build_snapshot("2025-01-02", "2025-01-03")

    result = validate_snapshot(snapshot)
    assert result["tradable"] is False
    assert snapshot.market["state_known"].eq(False).all()
    assert snapshot.market["industry_known"].eq(False).all()
    assert snapshot.metadata["actions_complete"] is False
    assert set(snapshot.actions.columns) == set(ACTION_COLUMNS)
    assert snapshot.metadata["benchmark_kind"] == "price"
    assert snapshot.benchmarks["close"].tolist() == [4000, 4010]

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    pd.DataFrame(
        {
            "ts_code": ["600001.SH", "600001.SH"],
            "trade_date": ["2025-01-02", "2025-01-03"],
            "is_st": [False, False],
        }
    ).to_csv(evidence / "state.csv", index=False)
    pd.DataFrame(
        {
            "ts_code": ["600001.SH", "600001.SH"],
            "trade_date": ["2025-01-02", "2025-01-03"],
            "industry": ["TECH", "TECH"],
        }
    ).to_csv(evidence / "industry.csv", index=False)
    pd.DataFrame(
        {
            "ts_code": ["600001.SH", "600001.SH"],
            "trade_date": ["2025-01-02", "2025-01-03"],
            "available_at": ["2025-01-02T18:00:00+08:00", "2025-01-03T18:00:00+08:00"],
        }
    ).to_csv(evidence / "availability.csv", index=False)
    (evidence / "actions_complete.json").write_text(json.dumps(True), encoding="utf-8")
    evidenced = downloader.build_snapshot("2025-01-02", "2025-01-03", evidence)
    assert validate_snapshot(evidenced)["tradable"] is False
    assert any("receipt" in item for item in validate_snapshot(evidenced)["warnings"])
    assert evidenced.metadata["availability_evidence"] is True
    assert "available_at" in evidenced.market.columns


def test_dividend_units_and_implementation_fields_are_normalized(tmp_path: Path) -> None:
    downloader = TushareDownloader(tmp_path / "data")
    raw = pd.DataFrame(
        [
            {
                "ts_code": "600001.SH",
                "div_proc": "实施",
                "record_date": "20250601",
                "ex_date": "20250602",
                "pay_date": "20250603",
                "div_listdate": "20250603",
                "cash_div": 0.20,
                "cash_div_tax": 0.30,
                "stk_div": 0.50,
                "imp_ann_date": "20250520",
            }
        ]
    )
    actions = downloader._normalise_actions(raw, None)

    assert actions.loc[0, "cash_per_share"] == pytest.approx(0.20)
    assert actions.loc[0, "bonus_ratio"] == pytest.approx(0.50)
    assert actions.loc[0, "share_list_date"] == "2025-06-03"
    assert actions.loc[0, "known_at"] == "2025-05-20"
    assert downloader._last_action_issues == []

    gross_only = raw.drop(columns=["cash_div"])
    downloader._normalise_actions(gross_only, None)
    assert any("cash_div" in issue for issue in downloader._last_action_issues)

    mixed = pd.concat([raw, raw.assign(div_proc="预案")], ignore_index=True)
    implemented = downloader._normalise_actions(mixed, None)
    assert len(implemented) == 1
    assert any("div_proc" in issue for issue in downloader._last_action_issues)

    missing_pay = downloader._normalise_actions(raw.assign(pay_date=None), None)
    assert pd.isna(missing_pay.loc[0, "pay_date"])
    assert any("pay date" in issue for issue in downloader._last_action_issues)

    absent_proc = downloader._normalise_actions(raw.drop(columns=["div_proc"]), None)
    assert absent_proc.empty
    assert any("div_proc is absent" in issue for issue in downloader._last_action_issues)


def test_download_uses_day_partitions_and_status_specific_stock_basic(tmp_path: Path) -> None:
    client = _RecordingFake()
    downloader = TushareDownloader(
        tmp_path / "data", token="test-token", client=client, min_request_interval=0
    )
    first = downloader.download("2025-01-02", "2025-01-03")
    second = downloader.download("2025-01-02", "2025-01-03")

    daily_calls = [kwargs for name, kwargs in client.calls if name == "daily"]
    assert len(daily_calls) == 2
    assert all("trade_date" in kwargs and "start_date" not in kwargs for kwargs in daily_calls)
    status_calls = [kwargs["list_status"] for name, kwargs in client.calls if name == "stock_basic"]
    assert status_calls == ["L", "D", "P"]
    assert all("trade_date" in kwargs for name, kwargs in client.calls if name == "suspend_d")
    assert all("ex_date" in kwargs for name, kwargs in client.calls if name == "dividend")
    assert first["status"] == "complete"
    assert any(key.startswith("trade_cal:") for key in second["cached"])
    assert any(key.startswith("daily:") for key in second["cached"])


def test_row_limit_response_is_left_uncached_and_reported_incomplete(tmp_path: Path) -> None:
    client = _RecordingFake()
    downloader = TushareDownloader(
        tmp_path / "data", token="test-token", client=client, row_limit=1, min_request_interval=0
    )
    result = downloader.download("2025-01-02", "2025-01-02")

    assert result["status"] in {"partial", "unavailable"}
    assert "daily" not in result["files"]
    assert any("partial" in warning or "pagination" in warning for warning in result["warnings"])
    assert not list((tmp_path / "data" / "raw" / "daily").glob("*.parquet"))
