"""Deterministic end-of-day risk state and pre-funded portfolio order construction."""

from __future__ import annotations

import math
from datetime import date as Date

from .broker import PaperBroker
from .config import Settings
from .models import Intent, price_tick, stable_id


def risk_state(history: list[dict], initial_cash: float, config: Settings, previous: dict) -> dict:
    """Drawdown halt is latched; daily/weekly halts recover after a configured quiet cooldown."""
    if not history:
        return dict(halted=False, hard_halt=False, cooldown=0, reasons=[])
    today = history[-1]
    current = today["equity"]
    high = max([initial_cash] + [r["equity"] for r in history])
    prev = history[-2]["equity"] if len(history) > 1 else initial_cash
    week = Date.fromisoformat(today["trade_date"]).isocalendar()[:2]
    prior_week = [r for r in history if Date.fromisoformat(r["trade_date"]).isocalendar()[:2] != week]
    week_base = prior_week[-1]["equity"] if prior_week else initial_cash
    dd = current / high - 1
    daily = current / prev - 1 if prev > 0 else -1
    weekly = current / week_base - 1 if week_base > 0 else -1
    reasons = []
    hard = bool(previous.get("hard_halt")) or dd <= -config.risk.max_drawdown
    cooldown = max(0, int(previous.get("cooldown", 0)) - 1)
    if hard:
        reasons.append("max_drawdown_latched")
    if daily <= -config.risk.daily_loss:
        reasons.append("daily_loss")
        cooldown = config.risk.cooldown_sessions
    if weekly <= -config.risk.weekly_loss:
        reasons.append("weekly_loss")
        cooldown = config.risk.cooldown_sessions
    if cooldown and not reasons:
        reasons.append("cooldown")
    return dict(
        halted=hard or cooldown > 0,
        hard_halt=hard,
        cooldown=cooldown,
        reasons=reasons,
        drawdown=dd,
        daily_return=daily,
        weekly_return=weekly,
    )


def create_orders(
    broker: PaperBroker,
    ranking: list[dict],
    targets: dict[str, float],
    equity: float,
    date: str,
    next_date: str,
    config: Settings,
    snapshot_id: str,
    rebalance: bool,
    halted: bool,
    market: dict[str, dict],
) -> list[dict]:
    """Risk-check using current holdings plus reservations; sale proceeds are never pre-spent."""
    by_symbol = {r["ts_code"]: r for r in ranking}
    positions = {p["ts_code"]: p for p in broker.get_positions(next_date)}
    cfg = config.strategy
    skipped: list[dict] = []
    if halted:
        targets = {}
    current_values = {s: p["quantity"] * market[s]["close"] for s, p in positions.items()}
    total = sum(current_values.values())
    industry_values: dict[str, float] = {}
    for s, p in positions.items():
        industry = market[s].get("industry") or p["industry"]
        industry_values[industry] = industry_values.get(industry, 0) + current_values[s]
    # Sequential planned trims obey caps even when prices have drifted; if blocked the real exposure remains.
    planned_total = 0.0
    planned_industry: dict[str, float] = {}
    for symbol, p in sorted(positions.items()):
        row = market[symbol]
        price = float(row["close"])
        industry = row.get("industry") or p["industry"]
        target = targets.get(symbol, 0)
        max_value = min(
            equity * target,
            equity * cfg.max_gross - planned_total,
            equity * cfg.max_industry - planned_industry.get(industry, 0),
        )
        if not rebalance and target > 0:
            max_value = min(current_values[symbol], max_value)
        desired = max(0, math.floor(max_value / price / 100) * 100)
        keep = min(p["quantity"], desired)
        planned_total += keep * price
        planned_industry[industry] = planned_industry.get(industry, 0) + keep * price
        sell = min(max(0, p["quantity"] - desired), p["available_to_sell"])
        candidate = by_symbol.get(symbol, {})
        adv = float(candidate.get("adv20", 0) or 0)
        if not math.isfinite(adv):
            adv = 0
        capacity = math.floor(adv * config.execution.adv_participation)
        sell = min(sell, capacity)
        if sell != p["available_to_sell"]:
            sell = (sell // 100) * 100
        if sell <= 0:
            if desired < p["quantity"]:
                skipped.append(dict(ts_code=symbol, side="SELL", reason="locked_or_capacity"))
            continue
        # Predetermined low sell limit, never re-sized after tomorrow's opening price.
        limit = price_tick(price * 0.5)
        reason = "risk_exit" if halted else ("strategy_exit" if target == 0 else "rebalance_trim")
        intent = Intent(
            stable_id(config.fingerprint, date, symbol, "SELL"),
            symbol,
            "SELL",
            sell,
            date,
            next_date,
            limit,
            industry,
            reason,
            adv,
            snapshot_id,
        )
        broker.submit_order(intent)
    if not rebalance or halted:
        return skipped
    # Current holdings remain in the buy-risk budget even if sell orders were submitted above.
    count = len(positions)
    for candidate in sorted(
        ranking, key=lambda r: (r.get("rank") if r.get("rank") is not None else 1e9, r["ts_code"])
    ):
        symbol = candidate["ts_code"]
        target = targets.get(symbol, 0)
        if target <= 0 or not candidate.get("eligible", False):
            continue
        if symbol not in positions and count >= cfg.max_positions:
            skipped.append(dict(ts_code=symbol, side="BUY", reason="max_positions_pending_sells_counted"))
            continue
        price = float(candidate["close"])
        limit = price_tick(price * (1 + config.execution.entry_gap_limit))
        industry = candidate["industry"]
        existing = current_values.get(symbol, 0)
        desired = max(0, equity * target - existing)
        cash_headroom = broker.ledger.available_cash - equity * (1 - cfg.max_gross)
        budget = min(
            desired,
            equity * cfg.max_gross - total,
            equity * cfg.max_industry - industry_values.get(industry, 0),
            cash_headroom,
        )
        adv = float(candidate["adv20"])
        if not math.isfinite(adv) or adv <= 0 or budget <= 0:
            continue
        quantity = min(
            math.floor(budget / limit / 100) * 100,
            math.floor(adv * config.execution.adv_participation / 100) * 100,
        )
        while quantity > 0:
            notional = quantity * limit
            reserve = notional + broker.fees.calculate("BUY", next_date, notional) / 100
            if reserve <= cash_headroom + 1e-9 and reserve <= broker.ledger.available_cash + 1e-9:
                break
            quantity -= 100
        if quantity <= 0:
            skipped.append(dict(ts_code=symbol, side="BUY", reason="cash_lot_or_capacity"))
            continue
        intent = Intent(
            stable_id(config.fingerprint, date, symbol, "BUY"),
            symbol,
            "BUY",
            quantity,
            date,
            next_date,
            limit,
            industry,
            "weekly_target",
            adv,
            snapshot_id,
        )
        broker.submit_order(intent)
        total += quantity * limit
        industry_values[industry] = industry_values.get(industry, 0) + quantity * limit
        if symbol not in positions:
            count += 1
    return skipped
