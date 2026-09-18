"""SQLite paper ledger: integer money, lot-level availability, atomic idempotent events."""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path

from .models import Intent, cents, stable_id


class Ledger:
    def __init__(self, path: Path | str, initial_cash: float, config_hash: str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=2, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(
          order_id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL, intent TEXT NOT NULL,
          ts_code TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0),
          decision_date TEXT NOT NULL, execute_date TEXT NOT NULL, status TEXT NOT NULL,
          filled INTEGER NOT NULL DEFAULT 0 CHECK(filled>=0 AND filled<=quantity),
          reserved_cents INTEGER NOT NULL DEFAULT 0 CHECK(reserved_cents>=0),
          notional_cents INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS lots(
          lot_id TEXT PRIMARY KEY, ts_code TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>=0),
          cost_cents INTEGER NOT NULL CHECK(cost_cents>=0), acquired TEXT NOT NULL,
          available_on TEXT NOT NULL, industry TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fills(
          fill_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(order_id),
          ts_code TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
          price REAL NOT NULL, fee REAL NOT NULL, trade_date TEXT NOT NULL, realized_pnl REAL);
        CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,
          event_key TEXT UNIQUE NOT NULL, trade_date TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entitlements(
          action_id TEXT PRIMARY KEY, ts_code TEXT NOT NULL, quantity INTEGER NOT NULL,
          action TEXT NOT NULL, cash_cents INTEGER NOT NULL, bonus_quantity INTEGER NOT NULL,
          ex_applied INTEGER NOT NULL DEFAULT 0, paid INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS equity(
          trade_date TEXT PRIMARY KEY, equity REAL NOT NULL, cash REAL NOT NULL,
          receivable REAL NOT NULL, exposure REAL NOT NULL, regime TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
          trade_date TEXT PRIMARY KEY, data_hash TEXT NOT NULL, result TEXT NOT NULL);
        """)
        with self.atomic():
            if self.get_meta("config_hash") is None:
                self.set_meta("config_hash", config_hash)
                self.set_meta("cash_cents", str(cents(initial_cash)))
                self.set_meta("initial_cash", str(initial_cash))
            elif self.get_meta("config_hash") != config_hash:
                raise ValueError("Ledger config differs; use a separate account/database for this strategy")

    @contextmanager
    def atomic(self):
        """Nested calls share the outer session transaction; competing writers fail visibly."""
        outer = self.db.in_transaction
        if not outer:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            if not outer:
                self.db.execute("COMMIT")
        except BaseException:
            if not outer and self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()

    def rows(self, table: str) -> list[dict]:
        if table not in {"orders", "lots", "fills", "audit", "equity", "sessions", "entitlements"}:
            raise ValueError("unknown ledger table")
        return [dict(r) for r in self.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        self.db.execute(
            "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )

    @property
    def cash_cents(self) -> int:
        return int(self.get_meta("cash_cents") or 0)

    @property
    def reserved_cents(self) -> int:
        return self.db.execute("SELECT COALESCE(SUM(reserved_cents),0) FROM orders").fetchone()[0]

    @property
    def available_cash(self) -> float:
        return (self.cash_cents - self.reserved_cents) / 100

    def audit(self, key: str, date: str, kind: str, payload: dict):
        self.db.execute(
            "INSERT INTO audit(event_key,trade_date,kind,payload) VALUES(?,?,?,?)",
            (key, date, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )

    def positions(self, date: str) -> list[dict]:
        out: dict[str, dict] = {}
        for lot in self.rows("lots"):
            if lot["quantity"] == 0:
                continue
            pos = out.setdefault(
                lot["ts_code"],
                dict(
                    ts_code=lot["ts_code"], quantity=0, available_to_sell=0, cost=0, industry=lot["industry"]
                ),
            )
            pos["quantity"] += lot["quantity"]
            pos["cost"] += lot["cost_cents"] / 100
            if lot["available_on"] <= date and lot["acquired"] < date:
                pos["available_to_sell"] += lot["quantity"]
        for row in self.db.execute(
            "SELECT ts_code,SUM(quantity-filled) q FROM orders WHERE side='SELL' "
            "AND status IN ('ACCEPTED','PARTIAL') GROUP BY ts_code"
        ):
            if row["ts_code"] in out:
                out[row["ts_code"]]["available_to_sell"] -= row["q"]
        return sorted(out.values(), key=lambda p: p["ts_code"])

    def get_order(self, order_id: str) -> dict:
        row = self.db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            raise ValueError("unknown order")
        return dict(row)

    @staticmethod
    def intent_from_order(order: dict) -> Intent:
        payload = json.loads(order["intent"])
        return Intent(**{x.name: payload[x.name] for x in fields(Intent)})

    def submit(self, intent: Intent, reserve: float = 0) -> str:
        """Accept a unique frozen intent and reserve actual cash/shares, never projected sale proceeds."""
        body = json.dumps(intent.to_dict(), ensure_ascii=False, sort_keys=True)
        with self.atomic():
            existing = self.db.execute(
                "SELECT * FROM orders WHERE proposal_id=?", (intent.proposal_id,)
            ).fetchone()
            if existing:
                if existing["intent"] != body:
                    raise ValueError("idempotency collision: same proposal with changed content")
                return existing["order_id"]
            reservation = cents(reserve) if intent.side == "BUY" else 0
            if intent.side == "BUY":
                if intent.quantity % 100 or reservation < cents(intent.quantity * intent.limit_price):
                    raise ValueError("buy must use whole lots and reserve worst-price notional")
                if reservation > self.cash_cents - self.reserved_cents:
                    raise ValueError("insufficient unreserved cash")
            else:
                pos = next(
                    (p for p in self.positions(intent.execute_date) if p["ts_code"] == intent.ts_code), None
                )
                if not pos or intent.quantity > pos["available_to_sell"]:
                    raise ValueError("T+1 or insufficient unreserved shares")
                if intent.quantity % 100 and intent.quantity != pos["available_to_sell"]:
                    raise ValueError("odd lot sale must dispose all available shares")
            self.db.execute(
                "INSERT INTO orders(order_id,proposal_id,intent,ts_code,side,quantity,decision_date,"
                "execute_date,status,reserved_cents) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    intent.order_id,
                    intent.proposal_id,
                    body,
                    intent.ts_code,
                    intent.side,
                    intent.quantity,
                    intent.decision_date,
                    intent.execute_date,
                    "ACCEPTED",
                    reservation,
                ),
            )
            self.audit(
                stable_id("submit", intent.order_id), intent.decision_date, "order_accepted", intent.to_dict()
            )
        return intent.order_id

    def terminate(self, order_id: str, date: str, reason: str, status: str = "REJECTED"):
        if status not in {"REJECTED", "CANCELLED", "EXPIRED"}:
            raise ValueError("invalid terminal state")
        with self.atomic():
            row = self.get_order(order_id)
            if row["status"] not in {"ACCEPTED", "PARTIAL"}:
                return
            self.db.execute(
                "UPDATE orders SET status=?,reserved_cents=0,reason=? WHERE order_id=?",
                (status, reason, order_id),
            )
            self.audit(
                stable_id("terminal", order_id),
                date,
                "order_" + status.lower(),
                dict(order_id=order_id, reason=reason),
            )

    def apply_fill(
        self,
        fill_id: str,
        order_id: str,
        quantity: int,
        price: float,
        fee_cents: int,
        date: str,
        available_on: str,
    ):
        """Post one fill atomically. Duplicate identical callbacks are a no-op, conflicting callbacks fail."""
        with self.atomic():
            existing = self.db.execute("SELECT * FROM fills WHERE fill_id=?", (fill_id,)).fetchone()
            if existing:
                if (
                    existing["order_id"],
                    existing["quantity"],
                    existing["price"],
                    cents(existing["fee"]),
                    existing["trade_date"],
                ) != (order_id, quantity, price, fee_cents, date):
                    raise ValueError("conflicting duplicate fill")
                return
            order = self.get_order(order_id)
            intent = self.intent_from_order(order)
            if (
                type(quantity) is not int
                or quantity <= 0
                or not math.isfinite(price)
                or price <= 0
                or type(fee_cents) is not int
                or fee_cents < 0
                or date != intent.execute_date
            ):
                raise ValueError("invalid fill")
            if (
                order["status"] not in {"ACCEPTED", "PARTIAL"}
                or order["filled"] + quantity > order["quantity"]
            ):
                raise ValueError("fill exceeds active order")
            if (intent.side == "BUY" and price > intent.limit_price + 1e-9) or (
                intent.side == "SELL" and price < intent.limit_price - 1e-9
            ):
                raise ValueError("fill violates frozen limit")
            notional = cents(price * quantity)
            realized = None
            reservation = order["reserved_cents"]
            if intent.side == "BUY":
                if available_on <= date or quantity % 100:
                    raise ValueError("invalid buy availability/lot")
                cost = notional + fee_cents
                if cost > reservation or cost > self.cash_cents:
                    raise ValueError("fill exceeds cash reservation")
                self.set_meta("cash_cents", str(self.cash_cents - cost))
                reservation -= cost
                self.db.execute(
                    "INSERT INTO lots VALUES(?,?,?,?,?,?,?)",
                    (fill_id, intent.ts_code, quantity, cost, date, available_on, intent.industry),
                )
            else:
                left, basis = quantity, 0
                lots = self.db.execute(
                    "SELECT * FROM lots WHERE ts_code=? AND acquired<? AND available_on<=? "
                    "AND quantity>0 ORDER BY acquired,lot_id",
                    (intent.ts_code, date, date),
                ).fetchall()
                if sum(lot["quantity"] for lot in lots) < left:
                    raise ValueError("T+1: fill exceeds sellable lots")
                for lot in lots:
                    take = min(left, lot["quantity"])
                    allocated = (
                        lot["cost_cents"]
                        if take == lot["quantity"]
                        else lot["cost_cents"] * take // lot["quantity"]
                    )
                    self.db.execute(
                        "UPDATE lots SET quantity=quantity-?,cost_cents=cost_cents-? WHERE lot_id=?",
                        (take, allocated, lot["lot_id"]),
                    )
                    basis += allocated
                    left -= take
                    if not left:
                        break
                proceeds = notional - fee_cents
                if self.cash_cents + proceeds < 0:
                    raise ValueError("sale fee exceeds available cash")
                self.set_meta("cash_cents", str(self.cash_cents + proceeds))
                realized = (proceeds - basis) / 100
            filled = order["filled"] + quantity
            status = "FILLED" if filled == order["quantity"] else "PARTIAL"
            if status == "FILLED":
                reservation = 0
            self.db.execute(
                "UPDATE orders SET filled=?,status=?,reserved_cents=?,notional_cents=notional_cents+? "
                "WHERE order_id=?",
                (filled, status, reservation, notional, order_id),
            )
            self.db.execute(
                "INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    fill_id,
                    order_id,
                    intent.ts_code,
                    intent.side,
                    quantity,
                    price,
                    fee_cents / 100,
                    date,
                    realized,
                ),
            )
            self.audit(
                fill_id,
                date,
                "fill",
                dict(order_id=order_id, quantity=quantity, price=price, fee=fee_cents / 100),
            )

    def record_entitlement(self, action: dict, date: str):
        if action["record_date"] != date:
            return
        with self.atomic():
            old = self.db.execute(
                "SELECT action FROM entitlements WHERE action_id=?", (action["action_id"],)
            ).fetchone()
            body = json.dumps(action, sort_keys=True)
            if old:
                if old[0] != body:
                    raise ValueError("corporate action revision conflicts with ledger")
                return
            qty = sum(p["quantity"] for p in self.positions(date) if p["ts_code"] == action["ts_code"])
            bonus = qty * float(action["bonus_ratio"])
            if abs(bonus - round(bonus)) > 1e-8:
                raise ValueError("fractional bonus allocation unsupported; explicit evidence required")
            self.db.execute(
                "INSERT INTO entitlements(action_id,ts_code,quantity,action,cash_cents,bonus_quantity) "
                "VALUES(?,?,?,?,?,?)",
                (
                    action["action_id"],
                    action["ts_code"],
                    qty,
                    body,
                    cents(qty * float(action["cash_per_share"])),
                    round(bonus),
                ),
            )

    def apply_actions(self, date: str):
        with self.atomic():
            for entitlement in self.rows("entitlements"):
                action = json.loads(entitlement["action"])
                key = entitlement["action_id"]
                if not entitlement["ex_applied"] and action["ex_date"] <= date:
                    if entitlement["bonus_quantity"]:
                        lots = self.db.execute(
                            "SELECT * FROM lots WHERE ts_code=? AND quantity>0", (action["ts_code"],)
                        ).fetchall()
                        industry = lots[0]["industry"] if lots else "UNKNOWN"
                        # Total book cost is conserved. Bonus lot has zero added acquisition cash.
                        self.db.execute(
                            "INSERT INTO lots VALUES(?,?,?,?,?,?,?)",
                            (
                                stable_id("bonus", key),
                                action["ts_code"],
                                entitlement["bonus_quantity"],
                                0,
                                action["record_date"],
                                action["share_list_date"],
                                industry,
                            ),
                        )
                    self.db.execute("UPDATE entitlements SET ex_applied=1 WHERE action_id=?", (key,))
                    self.audit(stable_id("ex", key), date, "corporate_action_ex", action)
                if not entitlement["paid"] and action["pay_date"] <= date:
                    self.set_meta("cash_cents", str(self.cash_cents + entitlement["cash_cents"]))
                    self.db.execute("UPDATE entitlements SET paid=1 WHERE action_id=?", (key,))
                    self.audit(
                        stable_id("pay", key),
                        date,
                        "dividend_paid",
                        dict(amount=entitlement["cash_cents"] / 100),
                    )

    @property
    def receivable(self) -> float:
        return self.db.execute(
            "SELECT COALESCE(SUM(cash_cents),0)/100.0 FROM entitlements WHERE ex_applied=1 AND paid=0"
        ).fetchone()[0]

    def mark(self, date: str, prices: dict[str, float], regime: str) -> dict:
        positions = self.positions(date)
        missing = [p["ts_code"] for p in positions if p["ts_code"] not in prices]
        if missing:
            raise ValueError(f"Cannot value held securities: {missing}")
        value = sum(cents(p["quantity"] * prices[p["ts_code"]]) for p in positions) / 100
        equity = self.cash_cents / 100 + self.receivable + value
        row = dict(
            trade_date=date,
            equity=equity,
            cash=self.cash_cents / 100,
            receivable=self.receivable,
            exposure=value / equity if equity > 0 else 0,
            regime=regime,
        )
        self.db.execute("INSERT INTO equity VALUES(?,?,?,?,?,?)", tuple(row.values()))
        return row

    def reconcile(self) -> dict:
        expected_cash = cents(self.get_meta("initial_cash") or "0")
        expected_shares: dict[str, int] = {}
        for fill in self.rows("fills"):
            sign = 1 if fill["side"] == "BUY" else -1
            expected_cash -= sign * cents(fill["price"] * fill["quantity"]) + cents(fill["fee"])
            expected_shares[fill["ts_code"]] = (
                expected_shares.get(fill["ts_code"], 0) + sign * fill["quantity"]
            )
        for entitlement in self.rows("entitlements"):
            if entitlement["paid"]:
                expected_cash += entitlement["cash_cents"]
            if entitlement["ex_applied"]:
                symbol = entitlement["ts_code"]
                expected_shares[symbol] = expected_shares.get(symbol, 0) + entitlement["bonus_quantity"]
        actual_shares: dict[str, int] = {}
        for lot in self.rows("lots"):
            actual_shares[lot["ts_code"]] = actual_shares.get(lot["ts_code"], 0) + lot["quantity"]
        checks = dict(
            cash_matches_events=self.cash_cents == expected_cash,
            shares_match_events=all(
                actual_shares.get(s, 0) == expected_shares.get(s, 0)
                for s in set(actual_shares) | set(expected_shares)
            ),
            nonnegative_cash=self.cash_cents >= 0,
            valid_reservations=0 <= self.reserved_cents <= self.cash_cents,
            nonnegative_lots=not self.db.execute(
                "SELECT 1 FROM lots WHERE quantity<0 OR cost_cents<0"
            ).fetchone(),
            fill_totals=not self.db.execute(
                "SELECT 1 FROM orders o WHERE filled != "
                "(SELECT COALESCE(SUM(quantity),0) FROM fills f WHERE f.order_id=o.order_id)"
            ).fetchone(),
        )
        if not all(checks.values()):
            raise ValueError(f"Ledger reconciliation failed: {checks}")
        return checks
