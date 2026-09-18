"""Paper adapter only. No live API import or executable live code exists."""

from __future__ import annotations

from typing import Protocol

from .config import ExecutionConfig, StrategyConfig
from .execution import FeeModel, simulate_open
from .ledger import Ledger
from .models import Intent, stable_id


class Broker(Protocol):
    def get_cash(self) -> dict: ...
    def get_positions(self, date: str) -> list[dict]: ...
    def get_orders(self) -> list[dict]: ...
    def submit_order(self, intent: Intent) -> str: ...
    def cancel_order(self, order_id: str, date: str) -> None: ...


class PaperBroker:
    def __init__(
        self,
        ledger: Ledger,
        config: ExecutionConfig,
        caps: StrategyConfig | None = None,
        *,
        require_opening_evidence: bool = False,
    ):
        self.ledger = ledger
        self.config = config
        self.fees = FeeModel(config)
        self.caps = caps
        self.require_opening_evidence = require_opening_evidence

    def get_cash(self) -> dict:
        return dict(
            total=self.ledger.cash_cents / 100,
            available=self.ledger.available_cash,
            frozen=self.ledger.reserved_cents / 100,
            receivable=self.ledger.receivable,
        )

    def get_positions(self, date: str) -> list[dict]:
        return self.ledger.positions(date)

    def get_orders(self) -> list[dict]:
        return self.ledger.rows("orders")

    def submit_order(self, intent: Intent) -> str:
        reserve = 0.0
        if intent.side == "BUY":
            notional = intent.quantity * intent.limit_price
            reserve = notional + self.fees.calculate("BUY", intent.execute_date, notional) / 100
        return self.ledger.submit(intent, reserve)

    def cancel_order(self, order_id: str, date: str) -> None:
        self.ledger.terminate(order_id, date, "cancel_requested", "CANCELLED")

    def settle_day(
        self,
        date: str,
        market: dict[str, dict],
        securities: dict[str, dict],
        next_session: str,
        corporate_action_symbols: set[str] | None = None,
    ):
        corporate_action_symbols = corporate_action_symbols or set()
        # Stable sort retains the priority in which the strategy froze same-side orders.
        orders = sorted(self.get_orders(), key=lambda x: x["side"] != "SELL")
        for order in orders:
            if order["status"] not in {"ACCEPTED", "PARTIAL"} or order["execute_date"] > date:
                continue
            if order["execute_date"] < date:
                self.ledger.terminate(order["order_id"], date, "missed_session", "EXPIRED")
                continue
            intent = self.ledger.intent_from_order(order)
            if intent.ts_code in corporate_action_symbols:
                self.ledger.terminate(
                    order["order_id"], date, "corporate_action_requires_new_intent", "CANCELLED"
                )
                continue
            decision = simulate_open(
                intent,
                market.get(intent.ts_code),
                securities.get(intent.ts_code, {}),
                self.config,
                require_opening_evidence=self.require_opening_evidence,
            )
            if not decision.allowed:
                self.ledger.terminate(order["order_id"], date, decision.reason)
                continue
            quantity = intent.quantity - order["filled"]
            fee = self.fees.calculate(
                intent.side, date, quantity * decision.price, order["notional_cents"] / 100
            )
            if intent.side == "BUY" and self.caps is not None:
                reason = self._opening_risk(intent, quantity, decision.price, fee, market, date)
                if reason:
                    self.ledger.terminate(order["order_id"], date, reason)
                    continue
            self.ledger.apply_fill(
                stable_id("fill", order["order_id"], order["filled"]),
                order["order_id"],
                quantity,
                decision.price,
                fee,
                date,
                next_session,
            )

    def _opening_risk(
        self, intent: Intent, quantity: int, price: float, fee: int, market: dict[str, dict], date: str
    ) -> str | None:
        """Predeclared conditional rejection; never change the frozen order size at open."""
        caps = self.caps
        assert caps is not None
        positions = self.ledger.positions(date)
        values: dict[str, float] = {}
        industries: dict[str, float] = {}
        for position in positions:
            symbol = position["ts_code"]
            row = market.get(symbol)
            if row is None or not row.get("state_known") or not row.get("industry_known"):
                return "opening_portfolio_data_unknown"
            if row.get("suspended"):
                value = row.get("valuation_price", row.get("close"))
            else:
                value = row.get("open")
            if value is None:
                return "opening_valuation_unknown"
            mark = float(value)
            values[symbol] = position["quantity"] * mark
            sector = row["industry"]
            industries[sector] = industries.get(sector, 0) + values[symbol]
        gross = sum(values.values())
        cash = self.ledger.cash_cents / 100
        notional = quantity * price
        slip_cost = quantity * (price - float(market[intent.ts_code]["open"]))
        projected_equity = cash + self.ledger.receivable + gross - fee / 100 - slip_cost
        sector = market[intent.ts_code].get("industry")
        if not market[intent.ts_code].get("industry_known") or sector != intent.industry:
            return "opening_industry_changed_or_unknown"
        if intent.ts_code not in values and len(values) >= caps.max_positions:
            return "opening_max_positions"
        if values.get(intent.ts_code, 0) + notional > projected_equity * caps.target_weight + 0.01:
            return "opening_single_position_cap"
        if gross + notional > projected_equity * caps.max_gross + 0.01:
            return "opening_gross_cap"
        if industries.get(sector, 0) + notional > projected_equity * caps.max_industry + 0.01:
            return "opening_industry_cap"
        if cash - notional - fee / 100 < projected_equity * (1 - caps.max_gross) - 0.01:
            return "opening_cash_floor"
        return None
