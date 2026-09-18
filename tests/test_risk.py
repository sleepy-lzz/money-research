from __future__ import annotations

from pathlib import Path

import pandas as pd

from ashare_agent.broker import PaperBroker
from ashare_agent.config import Settings
from ashare_agent.ledger import Ledger
from ashare_agent.models import Intent, stable_id
from ashare_agent.risk import create_orders, risk_state
from ashare_agent.strategy import select_targets


def _settings(**strategy):
    return Settings(
        strategy=strategy,
        risk={"initial_cash": 100_000, "daily_loss": 0.03, "weekly_loss": 0.06, "cooldown_sessions": 2},
    )


def _broker(settings: Settings, tmp_path: Path, cash: float | None = None):
    # A disk-backed temporary ledger makes the setup match the production
    # broker lifecycle while keeping each test isolated.
    ledger = Ledger(tmp_path / "ledger.sqlite", cash or settings.risk.initial_cash, settings.fingerprint)
    return ledger, PaperBroker(ledger, settings.execution)


def _seed_position(
    ledger: Ledger,
    broker: PaperBroker,
    settings: Settings,
    symbol: str,
    quantity: int,
    price: float,
    industry: str = "A",
):
    intent = Intent(
        stable_id("seed", symbol),
        symbol,
        "BUY",
        quantity,
        "2024-01-01",
        "2024-01-02",
        price,
        industry,
        "fixture",
        1_000_000,
        "fixture",
    )
    order_id = broker.submit_order(intent)
    fee = broker.fees.calculate("BUY", "2024-01-02", quantity * price, quantity * price)
    ledger.apply_fill(
        stable_id("seed-fill", symbol), order_id, quantity, price, fee, "2024-01-02", "2024-01-03"
    )


def _candidate(
    symbol: str,
    rank,
    *,
    industry: str = "A",
    close: float = 10.0,
    adv20: float = 10_000_000,
    eligible: bool = True,
):
    return {
        "ts_code": symbol,
        "rank": rank,
        "score": 1.0,
        "close": close,
        "adv20": adv20,
        "industry": industry,
        "eligible": eligible,
        "reason": "" if eligible else "st",
    }


def test_create_orders_does_not_oversize_target_or_adv20(tmp_path: Path):
    settings = _settings(target_weight=0.08, max_gross=0.8, max_industry=0.25)
    ledger, broker = _broker(settings, tmp_path)
    ranking = [_candidate("AAA.SZ", 1, close=10.0, adv20=50_000_000)]
    market = {}

    skipped = create_orders(
        broker,
        ranking,
        {"AAA.SZ": 0.08},
        100_000,
        "2024-01-03",
        "2024-01-04",
        settings,
        "fixture",
        True,
        False,
        market,
    )

    orders = [o for o in ledger.rows("orders") if o["decision_date"] == "2024-01-03"]
    # Frozen buy limit is 10.30. The target is 8,000 CNY, so only 700 shares
    # can be reserved; the large ADV cap cannot make this larger.
    assert orders[0]["quantity"] == 700
    assert orders[0]["quantity"] * 10.30 <= 8_000 + 1e-9
    assert skipped == []
    ledger.close()


def test_cash_gross_industry_caps_ignore_pending_sell_proceeds(tmp_path: Path):
    settings = _settings(target_weight=0.08, max_gross=0.8, max_industry=0.25)
    ledger, broker = _broker(settings, tmp_path)
    _seed_position(ledger, broker, settings, "HELD.SZ", 3_000, 10.0, "A")
    ranking = [
        _candidate("HELD.SZ", None, industry="A", adv20=10_000_000, eligible=False),
        _candidate("SAME.SZ", 1, industry="A"),
        _candidate("OTHER.SZ", 2, industry="B"),
    ]
    market = {"HELD.SZ": {"close": 10.0, "industry": "A"}}
    targets = {"SAME.SZ": 0.08, "OTHER.SZ": 0.08}

    skipped = create_orders(
        broker,
        ranking,
        targets,
        100_000,
        "2024-01-03",
        "2024-01-04",
        settings,
        "fixture",
        True,
        False,
        market,
    )

    orders = [o for o in ledger.rows("orders") if o["decision_date"] == "2024-01-03"]
    # A strategy exit is submitted first, but its proceeds are still pending;
    # this prevents the same-day buy budget from spending the sale.
    assert [o["side"] for o in orders] == ["SELL", "BUY"]
    assert orders[1]["quantity"] <= 700
    # SAME is blocked by the existing 30,000 CNY industry exposure. OTHER is
    # allowed, but remains below both the 8% target and the 80% gross cap.
    assert orders[1]["ts_code"] == "OTHER.SZ"
    assert not any(item.get("ts_code") == "SAME.SZ" and item.get("side") == "BUY" for item in skipped)
    assert ledger.available_cash < 100_000
    assert orders[1]["quantity"] * 10.30 <= 8_000 + 1e-9
    ledger.close()


def test_pending_sale_does_not_release_cash_or_gross_headroom(tmp_path: Path):
    settings = _settings(target_weight=0.08, max_gross=0.8, max_industry=0.25)
    ledger, broker = _broker(settings, tmp_path)
    # The account is 90% invested. Risk submits the full exit, but the sale is
    # still only an intent when the buy budget is evaluated.
    _seed_position(ledger, broker, settings, "HELD.SZ", 9_000, 10.0, "A")
    ranking = [
        _candidate("HELD.SZ", None, industry="A", adv20=10_000_000, eligible=False),
        _candidate("NEW.SZ", 1, industry="B"),
    ]
    market = {"HELD.SZ": {"close": 10.0, "industry": "A"}}

    create_orders(
        broker,
        ranking,
        {"NEW.SZ": 0.08},
        100_000,
        "2024-01-03",
        "2024-01-04",
        settings,
        "fixture",
        True,
        False,
        market,
    )
    orders = [o for o in ledger.rows("orders") if o["decision_date"] == "2024-01-03"]
    assert [o["side"] for o in orders] == ["SELL"]
    assert orders[0]["quantity"] == 9_000
    assert ledger.available_cash < 20_000
    ledger.close()


def test_rank_retention_and_daily_risk_halt():
    ranking = pd.DataFrame(
        [
            {"ts_code": "HELD15.SZ", "rank": 15, "eligible": True, "reason": "", "industry": "A"},
            {"ts_code": "HELD21.SZ", "rank": 21, "eligible": True, "reason": "", "industry": "B"},
            {"ts_code": "NEW1.SZ", "rank": 1, "eligible": True, "reason": "", "industry": "C"},
        ]
    )
    assert select_targets(ranking, ["HELD15.SZ", "HELD21.SZ"], True, {"target_weight": 0.08}) == {
        "HELD15.SZ": 0.08,
        "NEW1.SZ": 0.08,
    }
    assert select_targets(ranking, ["HELD15.SZ"], False, {"target_weight": 0.08}) == {
        "HELD15.SZ": 0.08,
    }

    settings = _settings()
    state = risk_state(
        [
            {"trade_date": "2024-01-01", "equity": 100_000},
            {"trade_date": "2024-01-02", "equity": 96_000},
        ],
        settings.risk.initial_cash,
        settings,
        {},
    )
    assert state["halted"] is True
    assert state["cooldown"] == settings.risk.cooldown_sessions
    assert "daily_loss" in state["reasons"]
