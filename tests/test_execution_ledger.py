from dataclasses import replace

import pytest

from ashare_agent.broker import PaperBroker
from ashare_agent.config import ExecutionConfig, Settings
from ashare_agent.execution import FeeModel, simulate_open
from ashare_agent.ledger import Ledger
from ashare_agent.models import Intent, cents


def intent(side="BUY", qty=100, key="p1", decision="2024-01-02", execute="2024-01-03", limit=11):
    return Intent(key, "600000.SH", side, qty, decision, execute, limit, "BANK", "test", 1_000_000, "fixture")


def market(**changes):
    data = dict(
        trade_date="2024-01-03",
        open=10,
        high=11,
        low=9,
        close=10,
        up_limit=11,
        down_limit=9,
        suspended=False,
        is_st=False,
        state_known=True,
    )
    data.update(changes)
    return data


SEC = dict(exchange="SSE", board="MAIN")


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"suspended": True}, "suspended"),
        ({"state_known": False}, "unknown_trading_state"),
        ({"open": 11}, "limit_up_buy_blocked"),
        ({"up_limit": float("nan")}, "unknown_price_limits"),
        ({"is_st": True}, "risk_warning"),
    ],
)
def test_open_guards(changes, reason):
    assert simulate_open(intent(), market(**changes), SEC, ExecutionConfig()).reason == reason


def test_future_intraday_high_low_never_changes_open_fill():
    a = simulate_open(intent(), market(), SEC, ExecutionConfig())
    b = simulate_open(intent(), market(high=100, low=0.1, close=30), SEC, ExecutionConfig())
    assert a == b and a.allowed and a.price == 10.01
    assert (
        simulate_open(replace(intent(), adv20=99), market(), SEC, ExecutionConfig()).reason
        == "historical_capacity_limit"
    )


def test_gap_down_stop_does_not_fill_at_stop_and_limit_down_blocks():
    order = intent("SELL", limit=5)
    result = simulate_open(order, market(open=9.5), SEC, ExecutionConfig())
    assert result.allowed and result.price == 9.49
    assert simulate_open(order, market(open=9), SEC, ExecutionConfig()).reason == "limit_down_sell_blocked"
    assert not simulate_open(replace(intent(), limit_price=10), market(), SEC, ExecutionConfig()).allowed


def test_fee_dates_and_minimum_per_order():
    f = FeeModel(ExecutionConfig())
    assert f.calculate("BUY", "2024-01-03", 1000) == 501  # 5 commission + .01 transfer
    assert f.calculate("SELL", "2024-01-03", 1000) == 551
    assert f.calculate("SELL", "2023-08-25", 1000) == 601
    assert f.calculate("SELL", "2022-04-28", 1000) == 602
    assert f.calculate("BUY", "2024-01-03", 1000, 1000) == 1
    with pytest.raises(ValueError):
        f.calculate("BUY", "2010-01-01", 1000)
    assert cents(1.005) == 101


def test_hand_ledger_cash_fee_fifo_and_duplicate(tmp_path):
    settings = Settings()
    path = tmp_path / "paper.db"
    ledger = Ledger(path, 10000, settings.fingerprint)
    broker = PaperBroker(ledger, settings.execution)
    buy = intent()
    broker.submit_order(buy)
    assert ledger.available_cash == 8894.99  # 100*11 + 5.01 reserved
    assert broker.submit_order(buy) == buy.order_id
    ledger.apply_fill("f1", buy.order_id, 100, 10, 501, "2024-01-03", "2024-01-04")
    assert ledger.cash_cents == 899499
    assert ledger.positions("2024-01-03")[0]["available_to_sell"] == 0
    assert ledger.positions("2024-01-04")[0]["available_to_sell"] == 100
    ledger.apply_fill("f1", buy.order_id, 100, 10, 501, "2024-01-03", "2024-01-04")
    assert len(ledger.rows("fills")) == 1
    with pytest.raises(ValueError, match="conflicting duplicate"):
        ledger.apply_fill("f1", buy.order_id, 100, 10.1, 501, "2024-01-03", "2024-01-04")
    with pytest.raises(ValueError, match=r"T\+1"):
        broker.submit_order(intent("SELL", key="same-day", decision="2024-01-02", limit=5))
    sell = intent("SELL", key="sell", decision="2024-01-03", execute="2024-01-04", limit=5)
    broker.submit_order(sell)
    ledger.apply_fill("f2", sell.order_id, 100, 11, 556, "2024-01-04", "2024-01-05")
    assert ledger.cash_cents == 1008943
    assert ledger.rows("fills")[-1]["realized_pnl"] == 89.43
    assert not ledger.positions("2024-01-04")
    assert all(ledger.reconcile().values())
    ledger.close()
    reopened = Ledger(path, 10000, settings.fingerprint)
    assert reopened.cash_cents == 1008943
    assert all(reopened.reconcile().values())
    reopened.close()


def test_old_position_sellable_but_new_lot_locked_and_partial_fills():
    ledger = Ledger(":memory:", 10000, "test")
    broker = PaperBroker(ledger, ExecutionConfig())
    old = intent(qty=200)
    broker.submit_order(old)
    ledger.apply_fill("old1", old.order_id, 100, 10, 501, "2024-01-03", "2024-01-04")
    assert ledger.get_order(old.order_id)["status"] == "PARTIAL"
    ledger.apply_fill("old2", old.order_id, 100, 10, 1, "2024-01-03", "2024-01-04")
    newer = intent(key="new", decision="2024-01-03", execute="2024-01-04")
    broker.submit_order(newer)
    ledger.apply_fill("new", newer.order_id, 100, 10, 501, "2024-01-04", "2024-01-05")
    p = ledger.positions("2024-01-04")[0]
    assert (p["quantity"], p["available_to_sell"]) == (300, 200)
    sell = intent("SELL", qty=200, key="exit", decision="2024-01-03", execute="2024-01-04", limit=5)
    broker.submit_order(sell)
    with pytest.raises(ValueError):
        broker.submit_order(replace(sell, proposal_id="another"))
    ledger.apply_fill("exit", sell.order_id, 200, 10, 510, "2024-01-04", "2024-01-05")
    assert ledger.positions("2024-01-04")[0]["available_to_sell"] == 0


def test_atomic_rollback_and_conflicting_proposal():
    ledger = Ledger(":memory:", 10000, "test")
    with pytest.raises(RuntimeError):
        with ledger.atomic():
            ledger.submit(intent(), 1105.01)
            raise RuntimeError("power-loss-like exception before commit")
    assert not ledger.rows("orders") and ledger.available_cash == 10000
    ledger.submit(intent(), 1105.01)
    with pytest.raises(ValueError, match="collision"):
        ledger.submit(replace(intent(), quantity=200), 2205.02)
    ledger.terminate(intent().order_id, "2024-01-03", "cancel", "CANCELLED")
    assert ledger.available_cash == 10000


def test_dividend_receivable_bonus_availability_and_equity_continuity():
    ledger = Ledger(":memory:", 10000, "test")
    broker = PaperBroker(ledger, ExecutionConfig())
    buy = intent()
    broker.submit_order(buy)
    ledger.apply_fill("buy", buy.order_id, 100, 10, 501, "2024-01-03", "2024-01-04")
    before = ledger.mark("2024-01-04", {"600000.SH": 10}, "unknown")["equity"]
    action = dict(
        action_id="div",
        ts_code="600000.SH",
        record_date="2024-01-04",
        ex_date="2024-01-05",
        pay_date="2024-01-08",
        share_list_date="2024-01-09",
        cash_per_share=1,
        bonus_ratio=1,
        known_at="2024-01-02",
    )
    ledger.record_entitlement(action, "2024-01-04")
    ledger.apply_actions("2024-01-05")
    assert ledger.receivable == 100
    p = ledger.positions("2024-01-05")[0]
    assert (p["quantity"], p["available_to_sell"]) == (200, 100)
    after = ledger.mark("2024-01-05", {"600000.SH": 4.5}, "unknown")["equity"]
    assert before == after == 9994.99
    ledger.apply_actions("2024-01-05")
    assert ledger.positions("2024-01-05")[0]["quantity"] == 200
    ledger.apply_actions("2024-01-08")
    assert ledger.receivable == 0 and ledger.cash_cents == 909499
    assert ledger.positions("2024-01-09")[0]["available_to_sell"] == 200


def test_config_rejects_live_and_unknown_parameters():
    with pytest.raises(ValueError):
        Settings.model_validate({"execution": {"mode": "LIVE_AUTO"}})
    with pytest.raises(ValueError):
        Settings.model_validate({"execution": {"allow_live_trading": True}})
