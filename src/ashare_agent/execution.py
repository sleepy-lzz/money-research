"""Conservative opening-price proxy. This is not exchange queue reconstruction."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from .config import ExecutionConfig
from .models import Intent, cents, price_tick
from .universe import supported_security


class FeeModel:
    def __init__(self, config: ExecutionConfig):
        self.config = config

    def calculate(self, side: str, date: str, notional: float, previous_notional: float = 0) -> int:
        """Fen for this fill; minimum commission charged once per order, not per fill."""
        if side not in {"BUY", "SELL"} or notional <= 0 or previous_notional < 0:
            raise ValueError("invalid fee input")
        applicable = [x for x in self.config.fees if x.effective_from <= date]
        if not applicable:
            raise ValueError(f"No verified fee schedule for {date}")
        rate = applicable[-1]

        def commission(n: float) -> int:
            return (
                max(
                    cents(self.config.minimum_commission),
                    cents(Decimal(str(n)) * Decimal(str(self.config.commission_rate))),
                )
                if n > 0
                else 0
            )

        delta = commission(notional + previous_notional) - commission(previous_notional)
        delta += cents(Decimal(str(notional)) * Decimal(str(rate.transfer_both)))
        if side == "SELL":
            delta += cents(Decimal(str(notional)) * Decimal(str(rate.stamp_sell)))
        return delta


@dataclass(frozen=True)
class FillDecision:
    allowed: bool
    reason: str
    price: float = 0


def simulate_open(
    intent: Intent,
    row: dict | None,
    security: dict,
    config: ExecutionConfig,
    *,
    require_opening_evidence: bool = False,
) -> FillDecision:
    """Evaluate an already frozen order using only open and session trading-state evidence."""

    def deny(reason: str) -> FillDecision:
        return FillDecision(False, reason)

    if not supported_security(security):
        return deny("unsupported_market")
    if row is None or row.get("trade_date") != intent.execute_date:
        return deny("missing_execution_data")
    if not bool(row.get("state_known", False)):
        return deny("unknown_trading_state")
    if bool(row.get("suspended", False)):
        return deny("suspended")
    if row.get("tradeable") is False or row.get("session_state_known") is False:
        return deny("session_not_tradeable")
    if require_opening_evidence:
        from .hardening import timestamp

        try:
            observed = timestamp(row.get("opening_observed_at"))
            opening_time = timestamp(intent.execute_date + "T09:30:00+08:00")
            volume = float(row.get("opening_volume"))
            if observed > opening_time or observed < timestamp(intent.execute_date + "T09:25:00+08:00"):
                return deny("opening_evidence_time_invalid")
            if (
                not math.isfinite(volume)
                or volume <= 0
                or intent.quantity > math.floor(volume * config.adv_participation)
            ):
                return deny("opening_capacity_unproven")
        except (TypeError, ValueError):
            return deny("opening_evidence_missing")
    if intent.side == "BUY" and bool(row.get("is_st", False)):
        return deny("risk_warning")
    values = [row.get(x) for x in ("open", "up_limit", "down_limit")]
    if any(v is None or not math.isfinite(float(v)) or float(v) <= 0 for v in values):
        return deny("unknown_price_limits")
    opening, upper, lower = map(float, values)
    if not lower <= opening <= upper:
        return deny("invalid_open")
    if intent.side == "BUY" and opening >= upper - 0.000001:
        return deny("limit_up_buy_blocked")
    if intent.side == "SELL" and opening <= lower + 0.000001:
        return deny("limit_down_sell_blocked")
    if intent.side == "BUY" and intent.quantity % 100:
        return deny("invalid_buy_lot")
    if intent.quantity > math.floor(intent.adv20 * config.adv_participation):
        return deny("historical_capacity_limit")
    direction = 1 if intent.side == "BUY" else -1
    price = price_tick(opening * (1 + direction * config.slippage_bps / 10000))
    if not lower <= price <= upper:
        return deny("slippage_outside_limits")
    if intent.side == "BUY" and price > intent.limit_price + 1e-9:
        return deny("entry_gap_or_limit")
    if intent.side == "SELL" and price < intent.limit_price - 1e-9:
        return deny("sell_limit")
    return FillDecision(True, "opening_price_proxy", price)
