"""Value objects at the strategy/execution boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal


def cents(value: float | str | Decimal) -> int:
    """Convert CNY to integer fen with financial half-up rounding."""
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def price_tick(value: float) -> float:
    return cents(value) / 100


def stable_id(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Intent:
    proposal_id: str
    ts_code: str
    side: Literal["BUY", "SELL"]
    quantity: int
    decision_date: str
    execute_date: str
    limit_price: float
    industry: str
    reason: str
    adv20: float
    snapshot_id: str

    def __post_init__(self):
        import math
        from datetime import date

        date.fromisoformat(self.decision_date)
        date.fromisoformat(self.execute_date)
        if self.side not in {"BUY", "SELL"} or type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("invalid side/quantity")
        if self.execute_date <= self.decision_date:
            raise ValueError("execution must follow decision day")
        if not math.isfinite(self.limit_price) or self.limit_price <= 0:
            raise ValueError("invalid limit price")
        if not math.isfinite(self.adv20) or self.adv20 < 0:
            raise ValueError("invalid historical volume")

    @property
    def order_id(self) -> str:
        return stable_id("order", self.proposal_id)

    def to_dict(self) -> dict:
        return asdict(self)
