import json
from datetime import datetime

import pytest

from ashare_agent import intraday
from ashare_agent.planner import Profile, account_input_hash, current_rule_hash

AT = datetime.fromisoformat("2026-09-11T10:00:00+08:00")


def profile():
    return Profile(capital="100000", cash="100000")


def entry(code="000001.SZ", **changes):
    return dict(ts_code=code, name="测试主板", entry_low="9.90", entry_high="10.10", stop_price="9.50",
                take_profit_price="11.30", amount20="100000000", reference_close="10.00", quantity=500,
                intraday_contract=1, decision_eligible=True, **changes)


def plan(p=None, holdings=None, entries=None):
    return dict(version_id="fixture-plan", created_at="2026-09-10T16:30:00+08:00", as_of="2026-09-10",
                source_hash=current_rule_hash(), profile=(p or profile()).model_dump(mode="json"),
                holdings=holdings or [], entries=entries if entries is not None else [entry()], status="complete")


def confirmation(p, positions, revision=1):
    return dict(id="fixture-confirmation", created_at="2026-09-11T09:00:00+08:00", revision=revision,
                input_hash=account_input_hash(p, positions), frozen_cash="0.00", other_assets="0.00",
                sellable_quantities={r["ts_code"]: r["quantity"] if r["buy_date"] < "2026-09-11" else 0 for r in positions})


def quote(price="10.00", ask="10.01", **changes):
    return {**dict(status="ok", price=price, ask=ask, bid="9.99", previous_close="10.00", name="测试主板",
                liquidity_observed=True, observed_at=AT.isoformat(), available_at=AT.isoformat(),
                source_observed_at={"sina": AT.isoformat(), "tencent": AT.isoformat()}), **changes}


def test_intraday_sizes_at_upper_band_and_never_above_frozen_quantity():
    p = profile()
    result = intraday.evaluate(p, [], 1, plan(p), confirmation(p, []), {"000001.SZ": quote()}, AT)
    assert 0 < result["entries"][0]["quantity"] <= 500
    assert result["entries"][0]["trigger"] == "入场条件触发"
    assert result["estimated_equity"] == "100000.00"


def test_possible_limit_state_stops_new_entry_and_sell_fill_claim():
    p = profile()
    q = quote("9.00", "9.00", limit_state="possible_lower_limit")
    result = intraday.evaluate(p, [], 1, plan(p), confirmation(p, []), {"000001.SZ": q}, AT)
    assert result["entries"][0]["quantity"] == 0
    assert any("涨跌停" in reason for reason in result["entries"][0]["reasons"])

    holding = dict(ts_code="600000.SH", quantity=100, buy_date="2026-09-10", cost_price="10.00",
                   stop_price="9.50", take_profit_price="11.30", max_hold_sessions=20)
    holding_plan = plan(p, holdings=[{**holding, "close": "10.00", "action": "继续观察"}], entries=[])
    result = intraday.evaluate(p, [holding], 1, holding_plan, confirmation(p, [holding]),
                               {"600000.SH": q}, AT)
    assert result["holdings"][0]["quantity"] == 0
    assert any("跌停" in reason for reason in result["holdings"][0]["reasons"])


@pytest.mark.parametrize("fault", ["future", "stale", "auction", "ask", "basis", "zero", "changed_account", "old_plan", "code", "risk_config"])
def test_invalid_facts_never_produce_positive_buy_quantity(fault):
    p, q = profile(), quote()
    frozen = plan(p)
    ack = confirmation(p, [])
    revision = 1
    if fault in {"future", "stale", "auction"}:
        q["source_observed_at"]["tencent"] = {"future": "2026-09-11T10:01:00+08:00", "stale": "2026-09-11T09:58:00+08:00", "auction": "2026-09-11T09:29:59+08:00"}[fault]
    elif fault == "ask":
        q["ask"] = "10.11"
    elif fault == "basis":
        q["previous_close"] = "9.00"
    elif fault == "zero":
        frozen["entries"][0]["quantity"] = 0
    elif fault == "changed_account":
        revision = 2
    elif fault == "old_plan":
        frozen["as_of"] = "2026-09-09"
    elif fault == "code":
        frozen["source_hash"] = "old"
    else:
        p.risk_pct /= 2
    result = intraday.evaluate(p, [], revision, frozen, ack, {"000001.SZ": q}, AT)
    assert result["entries"][0]["quantity"] == 0


def test_t_plus_one_stop_alert_visible_but_not_sellable_and_prevents_new_buy():
    p = profile()
    holding = dict(ts_code="600000.SH", quantity=100, buy_date="2026-09-11", cost_price="10.00",
                   stop_price="9.50", take_profit_price="11.30", max_hold_sessions=20)
    result = intraday.evaluate(p, [holding], 1, plan(p), confirmation(p, [holding]),
                              {"600000.SH": quote("9.40", "9.41"), "000001.SZ": quote()}, AT)
    assert result["holdings"][0]["trigger"] == "止损触发"
    assert result["holdings"][0]["quantity"] == 0
    assert result["entries"][0]["quantity"] == 0


def test_sellable_quantity_honours_user_available_lots_not_total_position():
    p = profile()
    holding = dict(ts_code="600000.SH", quantity=300, buy_date="2026-09-10", cost_price="10.00",
                   stop_price="9.50", take_profit_price="11.30", max_hold_sessions=20)
    ack = confirmation(p, [holding])
    ack["sellable_quantities"]["600000.SH"] = 100
    result = intraday.evaluate(p, [holding], 1, plan(p, [{**holding, "close": "10.00", "action": "继续观察"}]), ack,
                              {"600000.SH": quote("11.40", "11.41"), "000001.SZ": quote()}, AT)
    assert result["holdings"][0]["quantity"] == 100
    assert result["holdings"][0]["trigger"] == "止盈触发"


def setup_monitor(tmp_path, monkeypatch, quotes=None, entries=None):
    monkeypatch.setattr(intraday, "now", lambda: AT)
    book = intraday._book(tmp_path)
    book.save_profile(profile().model_dump(mode="json"))
    p = book.profile()
    revision = book.account_revision()
    book.publish(plan(p, entries=entries))
    book.close()
    def collector(root, codes):
        return {"quotes": quotes or {"000001.SZ": quote()}, "receipts": []}
    monitor = intraday.Monitor(tmp_path, collector)
    monitor.confirmation = confirmation(p, [], revision)
    return monitor


def test_tick_persists_deduplicated_alert_and_reuses_first_allocation(tmp_path, monkeypatch):
    quotes = {"000001.SZ": quote(), "600000.SH": quote("10.50", "10.51")}
    monitor = setup_monitor(tmp_path, monkeypatch, quotes, [entry(), entry("600000.SH")])
    monitor.tick()
    assert monitor.latest["entries"][0]["quantity"] > 0
    quotes["600000.SH"] = quote()
    monitor.tick()
    assert monitor.latest["entries"][1]["quantity"] == 0
    book = intraday._book(tmp_path)
    try:
        assert book.db.execute("SELECT count(*) FROM intraday_alerts").fetchone()[0] == 1
        assert book.db.execute("SELECT count(*) FROM intraday_ticks").fetchone()[0] == 2
    finally:
        book.close()


@pytest.mark.parametrize("change", ["account", "stop", "plan"])
def test_fetch_races_cannot_publish_old_account_or_stopped_results(tmp_path, monkeypatch, change):
    monitor = setup_monitor(tmp_path, monkeypatch)

    def collector(root, codes):
        if change == "stop":
            monitor.stop()
        else:
            book = intraday._book(root)
            if change == "account":
                book.save_profile({"capital": "100000", "cash": "0"})
            else:
                value = plan()
                value.update(version_id="new-plan", created_at="2026-09-11T09:00:00+08:00")
                book.publish(value)
            book.close()
        return {"quotes": {"000001.SZ": quote()}, "receipts": []}

    monitor.collector = collector
    if change == "stop":
        monitor.tick()
    else:
        with pytest.raises(ValueError, match="未发布"):
            monitor.tick()
    book = intraday._book(tmp_path)
    try:
        assert book.db.execute("SELECT count(*) FROM intraday_alerts").fetchone()[0] == 0
    finally:
        book.close()


@pytest.mark.parametrize("stamp,expected", [("09:29:59", False), ("09:30:00", True), ("11:30:00", False), ("12:00:00", False), ("13:00:00", True), ("14:57:00", False)])
def test_exchange_continuous_session_boundaries(stamp, expected):
    assert (intraday.market_phase(datetime.fromisoformat("2026-09-11T" + stamp + "+08:00")) == "连续竞价") is expected


def test_friday_plan_uses_next_exchange_session_not_calendar_day():
    from ashare_agent.planner import next_session_expired

    assert not next_session_expired("2026-09-11", datetime.fromisoformat("2026-09-14T09:30:00+08:00"))
    assert next_session_expired("2026-09-11", datetime.fromisoformat("2026-09-14T16:00:00+08:00"))


def test_read_state_invalidates_stopped_old_positive_quantities(tmp_path, monkeypatch):
    monitor = setup_monitor(tmp_path, monkeypatch)
    monitor.tick()
    assert monitor.latest["entries"][0]["quantity"] > 0
    state = monitor.state()
    assert state["running"] is False
    assert state["latest"]["entries"][0]["quantity"] == 0
    assert state["latest"]["status"] == "stale"
    json.dumps(state, allow_nan=False)


def test_less_cash_reduces_current_quantities_but_never_reuses_prior_cash():
    original = profile()
    current = Profile(capital="100000", cash="20000")
    result = intraday.evaluate(current, [], 2, plan(original), confirmation(current, [], 2), {"000001.SZ": quote()}, AT)
    assert result["entries"][0]["quantity"] == 100
    assert result["estimated_equity"] == "20000.00"


def test_unknown_holding_blocks_every_new_entry():
    p = profile()
    holding = dict(ts_code="600000.SH", quantity=100, buy_date="2026-09-10", cost_price="10.00",
                   stop_price="9.50", take_profit_price="11.30", max_hold_sessions=20)
    result = intraday.evaluate(p, [holding], 1, plan(p), confirmation(p, [holding]), {"000001.SZ": quote()}, AT)
    assert result["estimated_equity"] is None
    assert result["entries"][0]["quantity"] == 0


def test_quote_expiry_is_measured_from_source_time_not_tick_creation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    q = quote()
    q["source_observed_at"]["sina"] = "2026-09-11T09:58:40+08:00"
    monitor = setup_monitor(tmp_path, monkeypatch, {"000001.SZ": q})
    monitor.tick()
    assert monitor.latest["entries"][0]["quantity"] > 0
    monitor.thread = SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(intraday, "now", lambda: datetime.fromisoformat("2026-09-11T10:00:11+08:00"))
    assert monitor.state()["latest"]["entries"][0]["quantity"] == 0
