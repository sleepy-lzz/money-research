"""One event loop shared by historical replay and persistent daily paper processing."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .broker import PaperBroker
from .config import Settings
from .data import Snapshot, validate_snapshot
from .hardening import check_receipt, session_payload
from .ledger import Ledger
from .risk import create_orders, risk_state
from .strategy import is_rebalance_day, market_regime, rank_candidates, select_targets


def records(frame: pd.DataFrame) -> list[dict]:
    """JSON-safe numbers and nulls, no NaN leaking into reports or fingerprints."""
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def source_fingerprint() -> str:
    """Bind the executable trading/data rules, independent of report-only edits."""
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for name in (
        "config",
        "models",
        "data",
        "hardening",
        "universe",
        "raw_cache",
        "strategy",
        "risk",
        "execution",
        "ledger",
        "broker",
        "engine",
    ):
        digest.update(name.encode())
        if (root / f"{name}.py").exists():
            digest.update((root / f"{name}.py").read_bytes())
    return digest.hexdigest()


class _DailyMarket(Mapping):
    """Keep only three materialized day dictionaries instead of duplicating all historical rows."""

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.indices = {
            str(d): indices for d, indices in frame.groupby("trade_date", sort=True).indices.items()
        }
        self.cache: OrderedDict = OrderedDict()

    def __iter__(self):
        return iter(self.indices)

    def __len__(self):
        return len(self.indices)

    def __getitem__(self, date: str):
        if date not in self.cache:
            self.cache[date] = {r["ts_code"]: r for r in records(self.frame.iloc[self.indices[date]])}
            if len(self.cache) > 3:
                self.cache.popitem(last=False)
        return self.cache[date]


class Engine:
    def __init__(self, snapshot: Snapshot, settings: Settings, ledger_path: Path | str = ":memory:"):
        self.snapshot = snapshot
        self.settings = settings
        quality = validate_snapshot(snapshot)
        if quality["errors"] or not quality["tradable"]:
            raise ValueError(f"Snapshot is research-only, not simulation-ready: {quality}")
        self.quality = quality
        self.ledger = Ledger(ledger_path, settings.risk.initial_cash, settings.fingerprint)
        self.source_hash = source_fingerprint()
        previous_source = self.ledger.get_meta("source_hash")
        if previous_source is not None and previous_source != self.source_hash:
            self.ledger.close()
            raise ValueError("Trading source code changed; use a separate versioned ledger")
        self.ledger.set_meta("source_hash", self.source_hash)
        self.broker = PaperBroker(
            self.ledger,
            settings.execution,
            settings.strategy,
            require_opening_evidence=not bool(snapshot.metadata.get("synthetic")),
        )
        self.calendar = sorted(snapshot.calendar)
        self.market_by_date = _DailyMarket(snapshot.market)
        self._prefix_hashes: dict[str, str] = {}
        self._benchmark_days = {
            str(d): records(g.sort_values("ts_code")) for d, g in snapshot.benchmarks.groupby("trade_date")
        }
        self.securities = {r["ts_code"]: r for r in records(snapshot.securities)}
        self.actions = records(snapshot.actions)
        self._check_actions()
        synthetic = str(bool(snapshot.metadata.get("synthetic")))
        stored_type = self.ledger.get_meta("synthetic")
        if stored_type is not None and stored_type != synthetic:
            self.ledger.close()
            raise ValueError("Cannot mix synthetic and real market data in one account")
        self.ledger.set_meta("synthetic", synthetic)
        for completed in self.ledger.rows("sessions"):
            if completed["data_hash"] != self.day_hash(completed["trade_date"]):
                self.ledger.close()
                raise ValueError("Processed market data changed; use a separate replay ledger")

    def _check_actions(self):
        for action in self.actions:
            required = [
                "action_id",
                "ts_code",
                "record_date",
                "ex_date",
                "pay_date",
                "share_list_date",
                "cash_per_share",
                "bonus_ratio",
                "known_at",
            ]
            if any(action.get(k) is None for k in required):
                raise ValueError("Incomplete corporate action evidence")
            if not (
                action["record_date"] < action["ex_date"] <= action["pay_date"]
                and action["ex_date"] <= action["share_list_date"]
                and str(action["known_at"])[:10] <= action["record_date"]
            ):
                raise ValueError("Unsupported corporate action dates or late evidence")
            if action["cash_per_share"] < 0 or action["bonus_ratio"] < 0:
                raise ValueError("Unsupported negative corporate action")

    def day_hash(self, date: str) -> str:
        # A chained hash includes all past warmup inputs without re-serializing the full
        # historical panel on every session. Future prices/IPO dates never enter the prefix.
        if date in self._prefix_hashes:
            return self._prefix_hashes[date]
        previous_date = max(self._prefix_hashes, default="")
        digest = self._prefix_hashes.get(previous_date, "ashare-prefix-v2")
        for current in self.calendar:
            if current <= previous_date or current > date:
                continue
            securities = []
            for security in sorted(self.securities.values(), key=lambda r: r["ts_code"]):
                if security["list_date"] <= current:
                    item = {k: security[k] for k in ("ts_code", "list_date", "exchange", "board")}
                    item["delist_date"] = (
                        security.get("delist_date")
                        if security.get("delist_date") and security["delist_date"] <= current
                        else None
                    )
                    securities.append(item)
            day_rows = self.market_by_date.get(current, {})
            payload = dict(
                previous=digest,
                date=current,
                next_session=self.next_session(current),
                market=[day_rows[s] for s in sorted(day_rows)],
                benchmark=self._benchmark_days.get(current, []),
                securities=securities,
                actions=[a for a in self.actions if previous_date < str(a["known_at"])[:10] <= current],
                receipt=self.snapshot.metadata.get("session_receipts", {}).get(current),
                inventory_receipt=self.snapshot.metadata.get("inventory_receipts", {}).get(current),
            )
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            self._prefix_hashes[current] = digest
            previous_date = current
        return self._prefix_hashes[date]

    def next_session(self, date: str) -> str:
        following = [d for d in self.calendar if d > date]
        if not following:
            raise ValueError("Calendar must include the next session to freeze tomorrow's intent")
        return following[0]

    def close(self):
        self.ledger.close()

    def process_day(self, date: str, *, forward: bool = False) -> dict:
        if date not in self.market_by_date or date not in self.calendar:
            raise ValueError("Requested date has no complete market session")
        next_date = self.next_session(date)
        if forward:
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            deadline = datetime.fromisoformat(next_date + "T09:15:00+08:00")
            cutoff = datetime.fromisoformat(date + "T15:00:00+08:00")
            if now < cutoff or now >= deadline:
                raise ValueError(
                    "Forward paper intent must be frozen after close and before next auction; use --replay for history"
                )
            if bool(self.snapshot.metadata.get("synthetic")):
                raise ValueError("Synthetic data cannot be used for forward market evidence")
        decision_cutoff = (
            pd.Timestamp.now(tz="Asia/Shanghai") if forward else pd.Timestamp(date + "T23:59:59+08:00")
        )
        if not self.snapshot.metadata.get("synthetic"):
            check_receipt(
                self.snapshot.metadata.get("session_receipts", {}).get(date, {}),
                session_payload(self.snapshot, date),
                decision_cutoff.isoformat(),
            )
        for row in self.market_by_date[date].values():
            if forward and row.get("available_at") is None:
                raise ValueError("Forward data requires dated availability evidence")
            if row.get("available_at") is not None:
                published = pd.Timestamp(row["available_at"])
                if published.tzinfo is None or published > decision_cutoff:
                    raise ValueError("Session contains data unavailable at decision cutoff")
        data_hash = self.day_hash(date)
        with self.ledger.atomic():
            done = self.ledger.db.execute("SELECT * FROM sessions WHERE trade_date=?", (date,)).fetchone()
            if done:
                if done["data_hash"] != data_hash:
                    raise ValueError("Repeated session has changed input")
                latest = self.ledger.db.execute("SELECT MAX(trade_date) FROM sessions").fetchone()[0]
                if date != latest:
                    raise ValueError(
                        "Cannot generate an old as-of result from a later account; use saved report"
                    )
                return json.loads(done["result"])
            completed = self.ledger.rows("sessions")
            if completed and date != self.next_session(completed[-1]["trade_date"]):
                raise ValueError("Paper sessions must advance one trading day at a time; no skips/backdating")
            market = self.market_by_date[date]
            held = self.ledger.positions(date)
            missing = [p["ts_code"] for p in held if p["ts_code"] not in market]
            if missing:
                raise ValueError(f"Missing held-stock prices / delisting resolution required: {missing}")
            self.ledger.apply_actions(date)
            ex_symbols = {a["ts_code"] for a in self.actions if a["ex_date"] == date}
            self.broker.settle_day(date, market, self.securities, next_date, ex_symbols)
            regime = market_regime(
                self.snapshot, date, self.settings.data.benchmark, decision_cutoff=decision_cutoff.isoformat()
            )
            previous_regime = self.ledger.get_meta("next_regime") or "unknown"
            prices = {
                s: float(r["close"] if r.get("close") is not None else r["valuation_price"])
                for s, r in market.items()
                if r.get("close") is not None or r.get("valuation_price") is not None
            }
            marked = self.ledger.mark(date, prices, previous_regime)
            old_risk = json.loads(self.ledger.get_meta("risk_state") or "{}")
            risk = risk_state(
                self.ledger.rows("equity"), self.settings.risk.initial_cash, self.settings, old_risk
            )
            ranking = rank_candidates(
                self.snapshot,
                date,
                self.settings.strategy.model_dump(),
                decision_cutoff=decision_cutoff.isoformat(),
            )
            candidates = records(ranking)
            positions = self.ledger.positions(next_date)
            rebalance = is_rebalance_day(self.calendar, date)
            targets = select_targets(
                ranking, [p["ts_code"] for p in positions], rebalance, self.settings.strategy.model_dump()
            )
            skipped = create_orders(
                self.broker,
                candidates,
                targets,
                marked["equity"],
                date,
                next_date,
                self.settings,
                self.snapshot.snapshot_id,
                rebalance,
                risk["halted"],
                {s: dict(r, close=prices.get(s)) for s, r in market.items()},
            )
            for action in self.actions:
                self.ledger.record_entitlement(action, date)
            self.ledger.set_meta("risk_state", json.dumps(risk))
            self.ledger.set_meta("next_regime", regime["regime"])
            self.ledger.reconcile()
            result = dict(
                as_of=date,
                recorded_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                forward=forward,
                decision_cutoff=decision_cutoff.isoformat(),
                next_session=next_date,
                rebalance=rebalance,
                regime=regime,
                risk=risk,
                candidates=candidates,
                targets=targets,
                skipped=skipped,
                equity=marked,
                positions=self.ledger.positions(date),
            )
            self.ledger.db.execute(
                "INSERT INTO sessions VALUES(?,?,?)", (date, data_hash, json.dumps(result, sort_keys=True))
            )
            return result

    def run(self, start: str, end: str) -> dict:
        dates = [d for d in sorted(self.market_by_date) if start <= d <= end]
        if not dates or start > end:
            raise ValueError("No sessions in requested interval")
        result = {}
        for date in dates:
            done = self.ledger.db.execute(
                "SELECT result FROM sessions WHERE trade_date=?", (date,)
            ).fetchone()
            result = json.loads(done[0]) if done else self.process_day(date)
        return result

    def payload(self, last: dict, mode: str, run_id: str, research: list[dict] | None = None) -> dict:
        from .analytics import compute_metrics

        latest = self.ledger.db.execute("SELECT MAX(trade_date) FROM sessions").fetchone()[0]
        if last["as_of"] != latest:
            raise ValueError("Report as-of date differs from account state; use the saved report")
        equity = pd.DataFrame(self.ledger.rows("equity"))
        fills = pd.DataFrame(self.ledger.rows("fills"))
        benchmark = self.snapshot.benchmarks
        benchmark = benchmark[benchmark.ts_code == self.settings.data.benchmark].copy()
        warnings = list(self.quality["warnings"]) + [
            "真实数据缺少开盘成交量/时间证据则拒单；具备证据仍只是容量受限的近似成交，不重建队列。",
            "日线开盘近似撮合，不代表真实盘口、竞价容量或排队成交。",
            "所有阈值均为待检验的研究默认值，未证明存在超额收益。",
            "费用按配置日期表；佣金含券商经手/监管费用假设，实际账户需核对。",
            "现金红利按输入的税后金额记账；不自动推断持股期限差别税补扣。",
            "最大回撤触发后本账户停止开仓并尝试退出，不保证可卖出或损失封顶。",
        ]
        if mode == "PAPER_REPLAY":
            warnings.append("这是历史纸面重放，不是事前冻结的前瞻模拟记录。")
        if self.snapshot.metadata.get("benchmark_kind") != "total_return":
            warnings.append("基准不是经核验的全收益指数，分红口径差异不能解释为 Alpha。")
        return dict(
            run_id=run_id,
            mode=mode,
            snapshot_id=self.snapshot.snapshot_id,
            synthetic=bool(self.snapshot.metadata.get("synthetic")),
            as_of=last["as_of"],
            recorded_at=last["recorded_at"],
            provenance=dict(
                source_hash=self.source_hash,
                config_hash=self.settings.fingerprint,
                data_prefix_hash=self.day_hash(last["as_of"]),
            ),
            config=self.settings.model_dump(),
            metrics=compute_metrics(equity, fills, self.settings.risk.initial_cash, benchmark),
            candidates=last["candidates"],
            research=research or [],
            orders=self.ledger.rows("orders"),
            fills=self.ledger.rows("fills"),
            positions=self.ledger.positions(last["as_of"]),
            equity=records(equity),
            warnings=warnings,
            data_quality=self.quality,
            risk=last["risk"],
            regime=last["regime"],
            skipped=last["skipped"],
            audit=self.ledger.rows("audit"),
        )
