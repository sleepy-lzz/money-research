"""Validated, versioned simulation settings; live execution cannot be configured."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class StrategyConfig(StrictModel):
    min_listing_days: int = Field(default=250, ge=121)
    liquidity_min: float = Field(default=100_000_000, ge=0)
    candidate_count: int = Field(default=20, ge=1)
    max_positions: int = Field(default=10, ge=1)
    entry_rank: int = Field(default=10, ge=1)
    keep_rank: int = Field(default=20, ge=1)
    target_weight: float = Field(default=0.08, gt=0, le=1)
    max_gross: float = Field(default=0.8, gt=0, le=1)
    max_industry: float = Field(default=0.25, gt=0, le=1)
    momentum_windows: list[int] = [60, 120]
    trend_window: Literal[120] = 120

    @model_validator(mode="after")
    def check_baseline(self):
        if self.momentum_windows != [60, 120]:
            raise ValueError("v1 implements the frozen 60/120 baseline only")
        if not self.entry_rank <= self.keep_rank <= self.candidate_count:
            raise ValueError("entry_rank <= keep_rank <= candidate_count required")
        if self.target_weight > min(self.max_gross, self.max_industry):
            raise ValueError("target_weight exceeds portfolio constraints")
        return self


class FeeSchedule(StrictModel):
    effective_from: str
    stamp_sell: float = Field(ge=0, le=0.1)
    transfer_both: float = Field(ge=0, le=0.1)


class ExecutionConfig(StrictModel):
    mode: Literal["BACKTEST", "PAPER"] = "PAPER"
    slippage_bps: float = Field(default=10, ge=0, le=1000)
    commission_rate: float = Field(default=0.0003, ge=0, le=0.1)
    minimum_commission: float = Field(default=5, ge=0)
    entry_gap_limit: float = Field(default=0.03, ge=0, le=0.2)
    adv_participation: float = Field(default=0.001, gt=0, le=0.01)
    fees: list[FeeSchedule] = [
        FeeSchedule(effective_from="2015-08-01", stamp_sell=0.001, transfer_both=0.00002),
        FeeSchedule(effective_from="2022-04-29", stamp_sell=0.001, transfer_both=0.00001),
        FeeSchedule(effective_from="2023-08-28", stamp_sell=0.0005, transfer_both=0.00001),
    ]

    @model_validator(mode="after")
    def dated_fees(self):
        from datetime import date

        dates = [date.fromisoformat(x.effective_from) for x in self.fees]
        if not dates or dates != sorted(set(dates)):
            raise ValueError("fee dates must be unique and sorted")
        return self


class RiskConfig(StrictModel):
    initial_cash: float = Field(default=100_000, gt=0)
    max_drawdown: float = Field(default=0.15, gt=0, le=1)
    daily_loss: float = Field(default=0.03, gt=0, le=1)
    weekly_loss: float = Field(default=0.06, gt=0, le=1)
    cooldown_sessions: int = Field(default=5, ge=1)


class DataConfig(StrictModel):
    benchmark: str = "000300.SH"
    strict: Literal[True] = True


class ResearchConfig(StrictModel):
    enabled: bool = False
    max_candidates: int = Field(default=20, ge=0, le=100)
    timeout_seconds: float = Field(default=20, gt=0, le=60)
    max_calls: int = Field(default=5, ge=0, le=20)


class Settings(StrictModel):
    strategy_version: str = "mainboard-mom60-120-v1"
    strategy: StrategyConfig = StrategyConfig()
    execution: ExecutionConfig = ExecutionConfig()
    risk: RiskConfig = RiskConfig()
    data: DataConfig = DataConfig()
    research: ResearchConfig = ResearchConfig()

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump()
        # Mode and research do not change mechanical decisions; replay parity is testable.
        payload["execution"].pop("mode")
        payload.pop("research")
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def load_settings(path: Path | None = None) -> Settings:
    return (
        Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        if path
        else Settings()
    )
