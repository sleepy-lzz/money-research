from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from ashare_agent import context_feed, financial_context, market_context, planner, session_calendar
from ashare_agent.current_data import SHANGHAI
from ashare_agent.planner import PlanStore, fee, holding_review, risk_quantity, stress_risk, weighted_interval


def test_risk_quantity_respects_cash_allocation_risk_and_lot_budget():
    quantity, spent, loss = risk_quantity(
        entry=100,
        stop=94,
        cash=20_000,
        allocation=10_000,
        risk_budget=1_000,
        adv=10_000_000,
    )

    assert quantity == 100
    assert quantity % 100 == 0
    assert spent <= Decimal("20_000")
    assert Decimal(quantity) * Decimal("100") <= Decimal("10_000")
    assert loss <= Decimal("1_000")


def test_stress_risk_uses_the_same_buffer_and_fees_for_holdings_and_new_entries():
    mark, stop, quantity = Decimal("100"), Decimal("94"), 100
    exit_notional = stop * Decimal(".99") * quantity
    expected_holding = (
        mark * quantity - exit_notional + fee(exit_notional) + exit_notional * Decimal(".0005")
    )

    holding_loss = stress_risk(mark, stop, quantity)
    new_loss = stress_risk(mark, stop, quantity, entry_fee=True)

    assert holding_loss == expected_holding
    assert new_loss == expected_holding + fee(mark * quantity)
    assert risk_quantity(100, 94, 20_000, 10_000, 1_000, 10_000_000)[2] == stress_risk(
        100, 94, 100, entry_fee=True
    )


def test_weighted_interval_exposes_uncertainty_for_missing_context():
    low, high = weighted_interval(
        {"mechanical": 0.8, "market": 0.5, "news": None, "community": None},
        {"mechanical": "0.7", "market": "0.2", "news": "0.1", "community": "0"},
    )

    assert low == pytest.approx(0.66)
    assert high == pytest.approx(0.76)


def test_position_update_keeps_existing_stop_and_target(tmp_path):
    store = PlanStore(tmp_path)
    try:
        first = store.save_position(
            {"ts_code": "600000.SH", "quantity": 100, "cost_price": "100", "buy_date": "2026-09-01"}
        )
        store.save_profile({"stop_pct": "0.10"})
        second = store.save_position(
            {"ts_code": "600000.SH", "quantity": 100, "cost_price": "100", "buy_date": "2026-09-01"}
        )

        assert second["stop_price"] == first["stop_price"]
        assert second["take_profit_price"] == first["take_profit_price"]
        assert second["max_hold_sessions"] == first["max_hold_sessions"]
    finally:
        store.close()


def test_profile_change_blocks_latest_plan_without_rewriting_history(tmp_path):
    store = PlanStore(tmp_path)
    try:
        as_of = datetime.now(SHANGHAI).date().isoformat()
        original = {
            "version_id": "stable-version",
            "created_at": "2026-09-10T16:30:00+08:00",
            "as_of": as_of,
            "status": "review",
            "entries": [{"ts_code": "000001.SZ", "quantity": 100, "status": "条件计划"}],
            "holdings": [{"ts_code": "600000.SH", "sell_quantity": 100, "action": "止损触发"}],
            "warnings": [],
            "account_revision": 0,
            "source_hash": planner.current_rule_hash(),
        }
        store.publish(original)
        before = store.state()

        store.save_profile({"stop_pct": "0.08"})
        after = store.state()

        assert after["latest"]["status"] == "blocked"
        assert after["latest"]["entries"][0]["quantity"] == 0
        assert after["latest"]["holdings"][0]["sell_quantity"] == 0
        assert after["history"] == before["history"] == [
            {"version_id": "stable-version", "created_at": original["created_at"], "as_of": as_of, "status": "review"}
        ]
        assert before["latest"]["status"] == "review"
    finally:
        store.close()


def _published_plan(store, **overrides):
    plan = {
        "version_id": "freshness-version",
        "created_at": "2026-09-11T16:30:00+08:00",
        "as_of": datetime.now(SHANGHAI).date().isoformat(),
        "status": "review",
        "entries": [{"ts_code": "000001.SZ", "quantity": 100, "status": "条件计划"}],
        "holdings": [{"ts_code": "600000.SH", "sell_quantity": 100, "action": "止损触发"}],
        "warnings": [],
        "account_revision": store.account_revision(),
        "source_hash": planner.current_rule_hash(),
    }
    plan.update(overrides)
    store.publish(plan)
    return plan


def test_rule_change_blocks_current_plan_without_rewriting_history(tmp_path):
    store = PlanStore(tmp_path)
    try:
        original = _published_plan(store, source_hash="old-rules")
        state = store.state()

        assert state["latest"]["status"] == "blocked"
        assert state["latest"]["stale"] is True
        assert state["latest"]["entries"][0]["quantity"] == 0
        assert state["latest"]["holdings"][0]["sell_quantity"] == 0
        assert "规则版本" in state["latest"]["warnings"][0]
        assert state["history"] == [
            {
                "version_id": original["version_id"],
                "created_at": original["created_at"],
                "as_of": original["as_of"],
                "status": "review",
            }
        ]
    finally:
        store.close()


@pytest.mark.parametrize("source_hash", [None, "not-a-hash"])
def test_plan_without_reliable_rule_identity_is_not_executable(tmp_path, source_hash):
    store = PlanStore(tmp_path)
    try:
        overrides = {"source_hash": source_hash} if source_hash is not None else {"source_hash": None}
        _published_plan(store, **overrides)
        state = store.state()

        assert state["latest"]["status"] == "blocked"
        assert state["latest"]["entries"][0]["quantity"] == 0
        assert state["latest"]["holdings"][0]["sell_quantity"] == 0
        assert "规则" in state["latest"]["warnings"][0]
    finally:
        store.close()


def test_same_rule_identity_does_not_block_current_plan(tmp_path):
    store = PlanStore(tmp_path)
    try:
        _published_plan(store)
        state = store.state()

        assert state["latest"]["status"] == "review"
        assert state["latest"]["entries"][0]["quantity"] == 100
        assert state["latest"]["holdings"][0]["sell_quantity"] == 100
    finally:
        store.close()


def test_calendar_manifest_change_invalidates_plan_identity(tmp_path, monkeypatch):
    store = PlanStore(tmp_path)
    try:
        _published_plan(store)
        original_manifest = session_calendar.calendar_manifest()
        changed_manifest = {**original_manifest, "revision": "test-calendar-revision"}
        monkeypatch.setattr(session_calendar, "calendar_manifest", lambda: changed_manifest)

        state = store.state()

        assert state["latest"]["status"] == "blocked"
        assert state["latest"]["entries"][0]["quantity"] == 0
        assert "规则版本" in state["latest"]["warnings"][0]
    finally:
        store.close()


def test_invalid_calendar_blocks_current_display_but_keeps_history(tmp_path, monkeypatch):
    store = PlanStore(tmp_path)
    try:
        original = _published_plan(store)

        def invalid_calendar():
            raise ValueError("calendar fixture invalid")

        monkeypatch.setattr(session_calendar, "calendar_manifest", invalid_calendar)
        state = store.state()

        assert state["latest"]["status"] == "blocked"
        assert state["latest"]["entries"][0]["quantity"] == 0
        assert "日历" in state["latest"]["warnings"][0]
        assert state["history"] == [
            {
                "version_id": original["version_id"],
                "created_at": original["created_at"],
                "as_of": original["as_of"],
                "status": "review",
            }
        ]
    finally:
        store.close()


def test_publish_rechecks_account_revision_before_commit(tmp_path):
    store = PlanStore(tmp_path)
    try:
        result = {
            "version_id": "cas-version",
            "created_at": "2026-09-11T16:30:00+08:00",
            "as_of": datetime.now(SHANGHAI).date().isoformat(),
            "status": "review",
            "entries": [],
            "holdings": [],
            "warnings": [],
            "account_revision": store.account_revision(),
            "source_hash": planner.current_rule_hash(),
        }
        store.save_profile({"stop_pct": "0.08"})

        with pytest.raises(ValueError, match="复核期间"):
            store.publish(result, expected_account_revision=0)
        assert store.state()["latest"] is None
    finally:
        store.close()


def _holding_fixture(
    close: float, *, quote_date: str = "2026-09-10", max_hold: int = 20, name: str = "Demo"
):
    dates = ["2026-09-08", "2026-09-09", "2026-09-10"]
    raw = pd.DataFrame(
        {
            "trade_date": dates,
            "open": [100.0, 100.0, close],
            "close": [100.0, 100.0, close],
            "high": [101.0, 101.0, close + 1],
            "low": [99.0, 99.0, max(1.0, close - 1)],
            "volume": [1_000_000.0] * 3,
            "amount": [100_000_000.0] * 3,
        },
        index=dates,
    )
    last = raw.iloc[-1]
    quote = {
        "name": name,
        "date": quote_date,
        "observed_at": quote_date + "T15:30:00+08:00",
        **{field: float(last[field]) for field in ("open", "close", "high", "low", "volume", "amount")},
    }
    position = {
        "ts_code": "600000.SH",
        "quantity": 100,
        "cost_price": "100.00",
        "buy_date": "2026-09-09",
        "stop_price": "94.00",
        "take_profit_price": "112.00",
        "max_hold_sessions": max_hold,
    }
    benchmark = pd.DataFrame(index=dates)
    cutoff = datetime.fromisoformat("2026-09-10T16:00:00+08:00")
    return position, raw, quote, benchmark, cutoff


@pytest.mark.parametrize(
    ("close", "max_hold", "expected"),
    [
        (93.0, 20, "止损触发"),
        (113.0, 20, "止盈触发"),
        (100.0, 2, "到期复核"),
        (100.0, 20, "继续观察"),
    ],
)
def test_holding_review_evaluates_stop_target_holding_period_and_continue(close, max_hold, expected):
    position, raw, quote, benchmark, cutoff = _holding_fixture(close, max_hold=max_hold)

    result = holding_review(position, raw, quote, "Demo", benchmark, cutoff)

    assert result["action"] == expected
    assert result["held_sessions"] == 2
    assert result["close"] == f"{close:.2f}"


@pytest.mark.parametrize(
    ("close", "max_hold", "expected"),
    [
        (93.0, 20, "止损触发"),
        (113.0, 20, "止盈触发"),
        (100.0, 2, "到期复核"),
    ],
)
def test_risk_name_preserves_exit_trigger_but_requires_manual_sell_review(close, max_hold, expected):
    position, raw, quote, benchmark, cutoff = _holding_fixture(
        close, max_hold=max_hold, name="*ST Demo"
    )

    result = holding_review(position, raw, quote, "*ST Demo", benchmark, cutoff)

    assert result["action"] == expected
    assert result["exit_trigger"] == expected
    assert result["trigger_reason"]
    assert result["sell_quantity"] == 0
    assert "风险" in result["risk_warning"]
    assert "人工" in result["sell_condition"]


def test_holding_review_keeps_trigger_but_applies_t_plus_one(tmp_path):
    position, raw, quote, benchmark, cutoff = _holding_fixture(93.0)
    position["buy_date"] = "2026-09-10"

    result = holding_review(position, raw, quote, "Demo", benchmark, cutoff)

    assert result["action"] == "止损触发"
    assert result["exit_trigger"] == "止损触发"
    assert result["sell_quantity"] == 0
    assert "T+1" in result["sell_condition"]


def test_holding_review_refuses_stale_cross_source_quote():
    position, raw, quote, benchmark, cutoff = _holding_fixture(93.0, quote_date="2026-09-09")

    result = holding_review(position, raw, quote, "Demo", benchmark, cutoff)

    assert result["action"] == "数据待核验"
    assert result["close"] is None
    assert "日期/时间" in result["reason"]
    assert result["exit_trigger"] is None
    assert result["trigger_reason"] == ""


def _planner_frame(code: str, *, adjusted: bool = False, variant: str = "normal"):
    dates = ["2026-09-08", "2026-09-09", "2026-09-10"]
    if adjusted and variant == "last-date":
        dates = ["2026-09-07", "2026-09-08", "2026-09-09"]
    close = [100.0, 101.0, 102.0]
    if adjusted and variant == "factor":
        close = [110.0, 111.0, 125.0]
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": close,
            "close": close,
            "high": [value + 1 for value in close],
            "low": [value - 1 for value in close],
            "volume": [1_000_000.0] * 3,
            "amount": [100_000_000.0] * 3,
        },
        index=dates,
    )


class _PlannerClient:
    def __init__(self, root, *, qfq_variant="normal"):
        self.root = Path(root)
        self.qfq_variant = qfq_variant
        self.receipts = []

    def close(self):
        return None

    def quotes(self, codes):
        names = {"sh600000": "持仓股", "sz000001": "候选股"}
        result = {}
        for code in codes:
            frame = _planner_frame(code)
            last = frame.iloc[-1]
            result[code] = {
                "name": names[code],
                "date": "2026-09-10",
                "observed_at": "2026-09-10T15:30:00+08:00",
                "open": float(last.open),
                "close": float(last.close),
                "high": float(last.high),
                "low": float(last.low),
                "volume": float(last.volume),
                "amount": float(last.amount),
            }
        return result

    def bars(self, code, adjust="", end=None):
        if adjust == "qfq" and self.qfq_variant == "missing":
            raise ValueError("缺少 qfq 行情")
        return _planner_frame(code, adjusted=adjust == "qfq", variant=self.qfq_variant), {
            "sh600000": "持仓股",
            "sz000001": "候选股",
        }[code]


class _FixedPlannerDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 9, 10, 16, 30, tzinfo=SHANGHAI)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def _write_planner_selection(root: Path):
    from ashare_agent.current_screen import screen_rule_hash

    source = root / "selection"
    source.mkdir(parents=True)
    calendar = session_calendar.calendar_manifest()
    # Offline receipt times for this isolated historical fixture only.
    for item in calendar["sources"]:
        item["fetched_at"] = "2026-09-09T16:00:00+08:00"
    days = session_calendar.sessions("2025-01-01", "2026-09-10", calendar)[-251:]
    benchmark = pd.DataFrame(
        {
            "trade_date": days,
            "close": [100.0] * (len(days) - 2) + [105.0, 110.0],
        }
    )
    benchmark.to_parquet(source / "benchmark.parquet", index=False)
    selection = {
        "status": "complete",
        "as_of": "2026-09-10",
        "synthetic": False,
        "started_at": "2026-09-10T16:00:00+08:00",
        "completed_at": "2026-09-10T16:20:00+08:00",
        "receipts": [{"fetched_at": "2026-09-10T16:10:00+08:00", "available_at": "2026-09-10T16:10:00+08:00"}],
        "calendar_manifest": calendar,
        "source_hash": screen_rule_hash(calendar),
        "parameters": {
            "top": 20, "min_amount": 100000000, "momentum": [60, 120], "trend": 120,
            "symbols": None, "universe_policy": "sina_current_mainboard_directory",
        },
        "candidates": [
            {
                "ts_code": "000001.SZ",
                "name": "候选股",
                "close": 102.0,
                "amount20": 100_000_000.0,
                "score": 0.9,
            }
        ],
    }
    return source, selection


def _fake_context(codes, client, cutoff):
    return {
        "evidence": [],
        "warnings": [],
        "coverage": {code: {"news": "missing", "community": "missing"} for code in codes},
    }


def test_macro_and_financial_context_archived_without_changing_plan_math(tmp_path, monkeypatch):
    root = tmp_path / "root"
    source, selection = _write_planner_selection(root)
    monkeypatch.setattr(planner, "datetime", _FixedPlannerDatetime)
    monkeypatch.setattr(planner, "latest_screen", lambda ignored: (source, selection))
    monkeypatch.setattr(context_feed, "fetch_context", _fake_context)
    monkeypatch.setattr(planner, "CurrentClient", _PlannerClient)
    store = PlanStore(root / "runtime/planner")
    try:
        store.save_profile({"capital": "1000000", "cash": "1000000"})
        store.confirm_account(_confirmation_payload(store))
    finally:
        store.close()
    macro = dict(schema_version=1, evidence=[], coverage={}, as_of="2026-09-10T16:20:00+08:00")
    financial = dict(schema_version=1, evidence=[], coverage={}, as_of="2026-09-10T16:20:00+08:00")
    monkeypatch.setattr(market_context, "fetch_market_context", lambda client: macro)
    monkeypatch.setattr(financial_context, "fetch_financial_context", lambda codes, client: financial)
    planner.review(root, tmp_path / "baseline")
    first = json.loads((tmp_path / "baseline/planner-result.json").read_text(encoding="utf-8"))
    macro["evidence"] = [dict(title="利好利空混合事件", direction="未判定", trading_eligible=False)]
    financial["evidence"] = [dict(ts_code="000001.SZ", metrics={"PARENTNETPROFIT": "-1000000"}, trading_eligible=False)]
    planner.review(root, tmp_path / "enriched")
    second = json.loads((tmp_path / "enriched/planner-result.json").read_text(encoding="utf-8"))
    assert first["entries"][0]["quantity"] > 0
    for key in ("entries", "holdings", "factors", "evidence"):
        assert first[key] == second[key]
    assert first["version_id"] != second["version_id"]
    assert second["market_context"] == macro and second["financial_context"] == financial


@pytest.mark.parametrize("fault", ["legacy", "rule_hash", "parameters", "benchmark_gap", "future_receipt"])
def test_review_rejects_old_or_unverifiable_screen_before_any_new_plan(tmp_path, monkeypatch, fault):
    root = tmp_path / "root"
    source, selection = _write_planner_selection(root)
    if fault == "legacy":
        selection.pop("calendar_manifest")
    elif fault == "rule_hash":
        selection["source_hash"] = "old-rules"
    elif fault == "parameters":
        selection.pop("parameters")
    elif fault == "future_receipt":
        selection["receipts"][0]["available_at"] = "2026-09-11T17:00:00+08:00"
    else:
        path = source / "benchmark.parquet"
        frame = pd.read_parquet(path)
        frame.iloc[-3:].to_parquet(path, index=False)
    monkeypatch.setattr(planner, "datetime", _FixedPlannerDatetime)
    monkeypatch.setattr(planner, "latest_screen", lambda ignored: (source, selection))

    class Client:
        def __init__(self, *args):
            pass

        def quotes(self, *args):
            pytest.fail("Unverified screen must be rejected before gathering new inputs")

        def close(self):
            pass

    monkeypatch.setattr(planner, "CurrentClient", Client)
    with pytest.raises(ValueError):
        planner.review(root, tmp_path / "output")
    store = PlanStore(root / "runtime/planner")
    try:
        assert store.state()["latest"] is None
    finally:
        store.close()


def _save_planner_position(root: Path):
    store = PlanStore(root / "runtime" / "planner")
    try:
        store.save_profile({"capital": "100000", "cash": "100000"})
        store.save_position(
            {
                "ts_code": "600000.SH",
                "quantity": 100,
                "cost_price": "102",
                "buy_date": "2026-09-09",
            }
        )
    finally:
        store.close()


def _confirmation_payload(store, **changes):
    return {
        "as_of": "2026-09-10T15:30:00+08:00", "frozen_cash": "0", "other_assets": "0",
        "other_assets_note": "", "positions_complete": True,
        "expected_account_revision": store.account_revision(), **changes,
    }


@pytest.mark.parametrize("changes", [
    {"positions_complete": False}, {"positions_complete": "true"},
    {"expected_account_revision": True}, {"as_of": "2026-09-10T15:30:00"},
    {"as_of": "2026-09-10T17:30:00+08:00"}, {"as_of": "2026-09-06T15:30:00+08:00"},
    {"as_of": "2026-09-10T14:30:00+08:00"}, {"recorded_at": "2026-09-10T15:30:00+08:00"},
    {"total_assets": "1000000"}, {"other_assets": "1", "other_assets_note": " "},
])
def test_account_confirmation_rejects_untrusted_claims(tmp_path, monkeypatch, changes):
    monkeypatch.setattr(planner, "datetime", _FixedPlannerDatetime)
    store = PlanStore(tmp_path)
    try:
        store.save_profile({"capital": "1000000", "cash": "1000000"})
        revision = store.account_revision()
        with pytest.raises(ValueError):
            store.confirm_account(_confirmation_payload(store, **changes))
        assert store.account_revision() == revision
        assert store.db.execute("SELECT count(*) FROM account_confirmations").fetchone()[0] == 0
    finally:
        store.close()


def test_account_confirmation_is_revision_bound_and_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "datetime", _FixedPlannerDatetime)
    store = PlanStore(tmp_path)
    other = PlanStore(tmp_path)
    try:
        store.save_profile({"capital": "1000000", "cash": "1000000"})
        stale_body = _confirmation_payload(store)
        other.save_profile({"capital": "900000", "cash": "900000"})
        with pytest.raises(ValueError, match="资料已改变"):
            store.confirm_account(stale_body)
        confirmed = store.confirm_account(_confirmation_payload(store))
        assert confirmed["account_revision"] == store.account_revision()
        assert confirmed["total_assets"] == "900000.00"
        assert confirmed["positions_count"] == 0 and confirmed["positions_complete"] is True
        assert store.state()["account_check"]["status"] == "confirmed"
        original = store.db.execute("SELECT body FROM account_confirmations").fetchone()[0]
        store.save_position({"ts_code": "600000.SH", "quantity": 100, "cost_price": "10", "buy_date": "2026-09-09"})
        assert store.state()["account_check"]["status"] == "stale"
        assert store.db.execute("SELECT body FROM account_confirmations").fetchone()[0] == original
    finally:
        store.close()
        other.close()


@pytest.mark.parametrize("cash,frozen,other_assets,confirm,allow", [
    ("1000000", "0", "0", False, False),
    ("1000000", "0", "0", True, True),
    ("800000", "0", "0", True, False),
    ("800000", "200000", "0", True, False),
    ("800000", "0", "200000", True, False),
])
def test_complete_account_controls_actual_review_quantity(tmp_path, monkeypatch, cash, frozen, other_assets, confirm, allow):
    monkeypatch.setattr(planner, "datetime", _FixedPlannerDatetime)
    root = tmp_path / "root"
    source, selection = _write_planner_selection(root)
    store = PlanStore(root / "runtime/planner")
    try:
        store.save_profile({"capital": "1000000", "cash": cash})
        if confirm:
            store.confirm_account(_confirmation_payload(store, frozen_cash=frozen, other_assets=other_assets, other_assets_note="其他资产测试"))
    finally:
        store.close()
    monkeypatch.setattr(planner, "latest_screen", lambda ignored: (source, selection))
    monkeypatch.setattr(context_feed, "fetch_context", _fake_context)
    monkeypatch.setattr(planner, "CurrentClient", lambda client_root: _PlannerClient(client_root))
    planner.review(root, tmp_path / "result")
    result = json.loads((tmp_path / "result/planner-result.json").read_text(encoding="utf-8"))
    assert result["account_check"]["new_positions_allowed"] is allow
    assert (result["entries"][0]["quantity"] > 0) is allow
    assert result["entries"][0]["entry_high"] is not None
    if confirm and cash == "800000" and frozen == other_assets == "0":
        assert result["account_check"]["reconciliation_delta"] == "200000.00"


def test_missing_mark_never_uses_cost_for_reconciliation_or_portfolio_risk(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "datetime", _FixedPlannerDatetime)
    root = tmp_path / "root"
    source, selection = _write_planner_selection(root)
    _save_planner_position(root)
    store = PlanStore(root / "runtime/planner")
    try:
        store.confirm_account(_confirmation_payload(store))
    finally:
        store.close()

    class Client(_PlannerClient):
        def bars(self, code, adjust="", end=None):
            if code == "sh600000":
                raise RuntimeError("fixture missing holding price")
            return super().bars(code, adjust, end)

    monkeypatch.setattr(planner, "latest_screen", lambda ignored: (source, selection))
    monkeypatch.setattr(context_feed, "fetch_context", _fake_context)
    monkeypatch.setattr(planner, "CurrentClient", Client)
    planner.review(root, tmp_path / "result")
    result = json.loads((tmp_path / "result/planner-result.json").read_text(encoding="utf-8"))
    assert result["portfolio"] == {"valuation_known": False, "exposure": None, "open_risk": None}
    assert result["account_check"]["holdings_value"] is None
    assert result["account_check"]["reconciliation_delta"] is None
    assert result["entries"][0]["quantity"] == 0


@pytest.mark.parametrize("delta,allowed", [("1.00", True), ("-1.00", True), ("1.01", False), ("-1.01", False)])
def test_account_reconciliation_fixed_money_tolerance(tmp_path, monkeypatch, delta, allowed):
    monkeypatch.setattr(planner, "datetime", _FixedPlannerDatetime)
    store = PlanStore(tmp_path)
    try:
        store.save_profile({"capital": str(Decimal("100000") + Decimal(delta)), "cash": "99000"})
        store.save_position({"ts_code": "600000.SH", "quantity": 100, "cost_price": "9", "buy_date": "2026-09-09"})
        store.confirm_account(_confirmation_payload(store))
        result = planner.reconcile_account(
            store.account_statement(),
            [{"ts_code": "600000.SH", "quantity": 100, "close": "10", "action": "持有"}],
            "2026-09-10", _FixedPlannerDatetime.now(planner.SHANGHAI),
        )
        assert result["new_positions_allowed"] is allowed
        assert result["holdings_value"] == "1000.00"
        assert result["reconciliation_delta"] == delta
    finally:
        store.close()


def test_account_confirmation_morning_validity_and_new_close_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "datetime", _FixedPlannerDatetime)
    store = PlanStore(tmp_path)
    try:
        store.save_profile({"capital": "1000000", "cash": "1000000"})
        store.confirm_account(_confirmation_payload(store))
        morning = datetime.fromisoformat("2026-09-11T09:00:00+08:00")
        close = datetime.fromisoformat("2026-09-11T16:00:00+08:00")
        assert store.account_statement(morning)["status"] == "confirmed"
        assert store.account_statement(close)["status"] == "stale"
    finally:
        store.close()


def test_review_includes_all_holdings_and_stabilises_same_input_version(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    output = tmp_path / "output"
    source, selection = _write_planner_selection(root)
    _save_planner_position(root)
    monkeypatch.setattr(planner, "datetime", _FixedPlannerDatetime)
    monkeypatch.setattr(planner, "latest_screen", lambda ignored: (source, selection))
    monkeypatch.setattr(context_feed, "fetch_context", _fake_context)
    monkeypatch.setattr(planner, "CurrentClient", lambda client_root: _PlannerClient(client_root))

    first = planner.review(root, output)
    second = planner.review(root, output)

    assert first["version_id"] == second["version_id"]
    body = (output / "planner-result.json").read_text(encoding="utf-8")
    assert {row["ts_code"] for row in json.loads(body)["holdings"]} == {"600000.SH"}
    assert {row["ts_code"] for row in json.loads(body)["entries"]} == {"000001.SZ"}


@pytest.mark.parametrize("variant", ["last-date", "factor"])
def test_review_rejects_changed_qfq_revision_without_sell_signal(variant, tmp_path, monkeypatch):
    root = tmp_path / "root"
    output = tmp_path / "output"
    source, selection = _write_planner_selection(root)
    _save_planner_position(root)
    monkeypatch.setattr(planner, "datetime", _FixedPlannerDatetime)
    monkeypatch.setattr(planner, "latest_screen", lambda ignored: (source, selection))
    monkeypatch.setattr(context_feed, "fetch_context", _fake_context)

    monkeypatch.setattr(planner, "CurrentClient", lambda client_root: _PlannerClient(client_root))
    baseline = planner.review(root, output)
    monkeypatch.setattr(
        planner, "CurrentClient", lambda client_root: _PlannerClient(client_root, qfq_variant=variant)
    )
    changed = planner.review(root, output)

    assert changed["version_id"] != baseline["version_id"]
    body = json.loads((output / "planner-result.json").read_text(encoding="utf-8"))
    holding = body["holdings"][0]
    assert holding["action"] == "数据待核验"
    assert holding["sell_quantity"] == 0


def test_review_missing_qfq_cannot_publish_optimistic_exit(tmp_path, monkeypatch):
    root = tmp_path / "root"
    output = tmp_path / "output"
    source, selection = _write_planner_selection(root)
    _save_planner_position(root)
    monkeypatch.setattr(planner, "datetime", _FixedPlannerDatetime)
    monkeypatch.setattr(planner, "latest_screen", lambda ignored: (source, selection))
    monkeypatch.setattr(context_feed, "fetch_context", _fake_context)
    monkeypatch.setattr(
        planner, "CurrentClient", lambda client_root: _PlannerClient(client_root, qfq_variant="missing")
    )

    result = planner.review(root, output)
    body = json.loads((output / "planner-result.json").read_text(encoding="utf-8"))
    holding = body["holdings"][0]

    assert result["status"] == "partial"
    assert holding["action"] == "数据待核验"
    assert holding["sell_quantity"] == 0
