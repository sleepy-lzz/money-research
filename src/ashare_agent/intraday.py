"""Prospective condition alerts, never orders or fills. Manual account data remains manual."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from .current_data import SHANGHAI, risk_name
from .daily_lab import exclusive_run
from .planner import (
    PlanStore,
    Profile,
    account_input_hash,
    current_rule_hash,
    dec,
    fee,
    money,
    risk_quantity,
    stress_risk,
)
from .session_calendar import next_sessions, sessions


def now():
    return datetime.now(SHANGHAI)


def market_phase(at):
    if sessions(at.date().isoformat(), at.date().isoformat()) != [at.date().isoformat()]:
        return "休市"
    clock = at.time().replace(tzinfo=None)
    if time(9, 30) <= clock < time(11, 30) or time(13) <= clock < time(14, 57):
        return "连续竞价"
    return "午间休市" if time(11, 30) <= clock < time(13) else "非监控时段（不含集合竞价）"


def source_hash():
    digest = hashlib.sha256()
    for name in ("intraday.py", "live_quotes.py", "planner.py", "session_calendar.py"):
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    expected_account_revision: int = Field(strict=True, ge=0)
    positions_complete: StrictBool
    corporate_actions_checked: StrictBool
    frozen_cash: Decimal = Field(ge=0, le=1_000_000_000)
    other_assets: Decimal = Field(ge=0, le=1_000_000_000)
    sellable_quantities: dict[str, StrictInt]

    @model_validator(mode="after")
    def explicit(self):
        if not self.positions_complete or not self.corporate_actions_checked:
            raise ValueError("须明确核对完整持仓、现金、成本及除权除息情况")
        return self


def _book(root):
    book = PlanStore(Path(root) / "runtime/planner")
    book.db.executescript("""
      CREATE TABLE IF NOT EXISTS intraday_confirmations(id TEXT PRIMARY KEY, body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS intraday_ticks(id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS intraday_alerts(id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, body TEXT NOT NULL);
    """)
    return book


def _inputs(book):
    profile = book.profile()
    positions = book.positions()
    plan = None
    for row in book.db.execute("SELECT body FROM reviews ORDER BY created_at DESC,id DESC LIMIT 30"):
        value = json.loads(row[0])
        if book.visible(value):
            plan = value
            break
    return profile, positions, book.account_revision(), plan


def _risk_profile(profile):
    return {k: v for k, v in profile.model_dump(mode="json").items() if k not in {"capital", "cash"}}


def _plan_reason(plan, profile, at):
    if not plan:
        return "尚无收盘计划，先完成收盘复核；盘中仍可监控已核对的持仓条件"
    if plan.get("source_hash") != current_rule_hash():
        return "收盘计划规则已变化，请在收盘后重新复核"
    if next_sessions(plan["as_of"], 1)[0] != at.date().isoformat():
        return "入场计划仅用于其下一个交易日，不能延用过期选股"
    if _risk_profile(Profile.model_validate(plan["profile"])) != _risk_profile(profile):
        return "风险参数已改变，冻结入场计划需重新复核"
    if datetime.fromisoformat(plan["created_at"]) > at:
        return "计划形成时间晚于本次监控"
    return None


def evaluate(profile, positions, revision, plan, confirmation, quotes, at):
    """Pure deterministic sizing. Missing facts produce unknown/zero, never an assumed fill."""
    result = dict(status="monitoring", checked_at=at.isoformat(), entries=[], holdings=[], reasons=[],
                  plan_version=plan.get("version_id") if plan else None, account_revision=revision)
    quotes = json.loads(json.dumps(quotes))
    for q in quotes.values():
        if q.get("status") != "ok":
            continue
        try:
            stamps = q["source_observed_at"]
            if set(stamps) != {"sina", "tencent"}:
                raise ValueError("双源时间缺失")
            for value in [*stamps.values(), q["available_at"]]:
                stamp = datetime.fromisoformat(value)
                if stamp.tzinfo is None or stamp.astimezone(SHANGHAI).date() != at.date() or not 0 <= (at - stamp).total_seconds() <= 90:
                    raise ValueError("报价获取或源时间已过期/位于未来")
            for value in stamps.values():
                stamp = datetime.fromisoformat(value).astimezone(SHANGHAI)
                if market_phase(stamp) != "连续竞价" or (stamp.hour < 12) != (at.hour < 12):
                    raise ValueError("报价不属于当前连续竞价时段")
        except (ValueError, KeyError, TypeError) as exc:
            q.update(status="stale", reasons=[str(exc)])
    account_ok = bool(confirmation and confirmation["revision"] == revision
                      and confirmation["input_hash"] == account_input_hash(profile, positions)
                      and datetime.fromisoformat(confirmation["created_at"]).date() == at.date()
                      and datetime.fromisoformat(confirmation["created_at"]) <= at)
    if not account_ok:
        result["reasons"].append("资金或持仓已变化、确认已过期；停止后核对实际账户并重新开始")
    plan_reason = _plan_reason(plan, profile, at)
    if plan_reason:
        result["reasons"].append(plan_reason)
    old_holdings = {p["ts_code"]: p for p in (plan or {}).get("holdings", [])}
    equity_holdings, open_risk = Decimal(0), Decimal(0)
    portfolio_known = True
    exit_present = False
    for p in positions:
        code = p["ts_code"]
        q = quotes.get(code, {})
        fresh = q.get("status") == "ok"
        row = dict(ts_code=code, name=q.get("name", code), price=q.get("price"), observed_at=q.get("observed_at"),
                   stop_price=p["stop_price"], take_profit_price=p["take_profit_price"], quantity=0,
                   available_quantity=0, status="观察", trigger=None, reasons=[])
        basis_ok = account_ok
        old = old_holdings.get(code)
        if p["buy_date"] < at.date().isoformat():
            if not old or any(old.get(key) != p[key] for key in ("cost_price", "buy_date", "stop_price", "take_profit_price")) or not old.get("close") or old.get("action") == "数据待核验":
                basis_ok = False
            elif fresh and abs(dec(q["previous_close"]) - dec(old["close"])) > Decimal(".011"):
                basis_ok = False
        if not basis_ok:
            row["reasons"].append("持仓成本或除权价格基准需重新核验；不沿用未知基准触发买卖")
        if not fresh:
            row["reasons"] += q.get("reasons", ["缺少新鲜双源报价"])
        if fresh and basis_ok:
            price = dec(q["price"])
            held_days = len(sessions(p["buy_date"], at.date().isoformat()))
            row["trigger"] = "止损触发" if price <= dec(p["stop_price"]) else (
                "止盈触发" if price >= dec(p["take_profit_price"]) else
                "到期复核" if held_days >= p["max_hold_sessions"] else None)
            sellable = confirmation["sellable_quantities"][code]
            if p["buy_date"] == at.date().isoformat():
                sellable = 0
                row["reasons"].append("买入当日受 T+1 限制，今日不能卖出")
            row["available_quantity"] = sellable
            if risk_name(q["name"]) or not q.get("liquidity_observed", False):
                sellable = 0
                row["reasons"].append("风险状态或买卖盘口不可核验，数量为0，需人工处理")
            limit_state = q.get("limit_state", "unknown")
            if limit_state == "possible_lower_limit":
                sellable = 0
                row["reasons"].append("当前可能接近跌停，无法证明卖出成交；数量为0")
            if row["trigger"]:
                row["quantity"] = sellable
                row["status"] = row["trigger"]
                exit_present = True
            equity_holdings += price * p["quantity"]
            open_risk += stress_risk(price, p["stop_price"], p["quantity"])
        else:
            portfolio_known = False
            row["status"] = "数据待核验"
        result["holdings"].append(row)
    asset_unknown = not account_ok or (confirmation and (dec(confirmation["frozen_cash"]) > 0 or dec(confirmation["other_assets"]) > 0))
    if asset_unknown:
        result["reasons"].append("未确认账户或存在未建模冻结资金/其他资产，新仓数量为0")
    cash = profile.cash or Decimal(0)
    equity = cash + equity_holdings if portfolio_known else None
    capital = min(profile.capital or Decimal(0), equity) if equity is not None else Decimal(0)
    gross_left = max(Decimal(0), capital * profile.max_gross_pct - equity_holdings)
    risk_left = max(Decimal(0), capital * profile.portfolio_risk_pct - open_risk)
    result["estimated_equity"] = money(equity) if equity is not None else None
    result["cash_basis"] = money(cash)
    result["reasons"].append("数量仅适用于本批次，不叠加旧提醒；实际成交后须同步资金/持仓。新闻沿用收盘证据，未作盘中新闻复核。")
    held_codes = {p["ts_code"] for p in positions}
    for entry in (plan or {}).get("entries", []):
        code = entry["ts_code"]
        if code in held_codes:
            continue
        q = quotes.get(code, {})
        row = dict(ts_code=code, name=entry["name"], price=q.get("price"), observed_at=q.get("observed_at"),
                   entry_low=entry["entry_low"], entry_high=entry["entry_high"], stop_price=entry["stop_price"],
                   take_profit_price=entry["take_profit_price"], quantity=0, status="等待入场条件", trigger=None, reasons=[])
        reasons = row["reasons"]
        if plan_reason or entry.get("intraday_contract") != 1 or entry.get("decision_eligible") is not True or entry.get("quantity", 0) <= 0:
            reasons.append(plan_reason or "收盘候选未通过市场/新闻/数据门槛，或缺少盘中契约字段")
        if asset_unknown or not portfolio_known or exit_present:
            reasons.append("账户、持仓估值或退出风险门槛未满足")
        if q.get("status") != "ok":
            reasons += q.get("reasons", ["缺少新鲜双源报价"])
        elif abs(dec(q["previous_close"]) - dec(entry.get("reference_close", 0))) > Decimal(".011"):
            reasons.append("昨收与冻结价格基准不符，可能除权或修订，停止沿用原计划")
        if q.get("status") == "ok":
            if risk_name(q["name"]) or not q.get("liquidity_observed", False):
                reasons.append("风险名称或买卖盘口未满足要求")
            if q.get("limit_state") in {"possible_lower_limit", "possible_upper_limit"}:
                reasons.append("当前可能处于涨跌停附近，成交无法证明；停止入场")
            price, ask = dec(q["price"]), dec(q.get("ask") or 0)
            low, high = dec(entry["entry_low"]), dec(entry["entry_high"])
            if not low <= price <= high or not low <= ask <= high:
                reasons.append("当前价或卖一价不在冻结入场区间，不追价")
        if not reasons:
            # Reserve this batch sequentially at the worst permitted entry price.
            size, spent, loss = risk_quantity(high, dec(entry["stop_price"]), cash,
                                              min(capital * profile.max_position_pct, gross_left),
                                              min(capital * profile.risk_pct, risk_left), dec(entry["amount20"]))
            size = min(size, entry["quantity"])
            spent = high * size + fee(high * size) if size else Decimal(0)
            loss = stress_risk(high, entry["stop_price"], size, True) if size else Decimal(0)
            row["quantity"] = size
            if size:
                row.update(status="入场条件触发", trigger="入场条件触发")
                cash -= spent
                gross_left -= high * size
                risk_left -= loss
            else:
                reasons.append("剩余资金、风险预算或整手约束不足")
        if reasons:
            row["status"] = "等待核验" if q.get("status") != "ok" else "未满足"
        result["entries"].append(row)
    if not account_ok:
        result["status"] = "account_changed"
    return result


class Monitor:
    def __init__(self, root, collector=None):
        from .live_quotes import collect_quotes

        self.root = Path(root)
        self.collector = collector or collect_quotes
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.confirmation = None
        self.latest = None
        self.last_error = None
        self.rules = source_hash()
        self.owner = None

    def start(self, body):
        value = Confirmation.model_validate(body)
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("监控已启动；更新账户后请先停止，再重新核对开始")
            if self.rules != source_hash():
                raise ValueError("监控代码已变化，请重新启动新版工作台")
            owner = exclusive_run(self.root / "runtime/intraday")
            owner.__enter__()
            book = _book(self.root)
            try:
                with book.db:
                    book.db.execute("BEGIN IMMEDIATE")
                    profile, positions, revision, _ = _inputs(book)
                    if value.expected_account_revision != revision:
                        raise ValueError("账户资料已变化，请刷新并重新核对")
                    if profile.cash is None or profile.capital is None:
                        raise ValueError("请先录入实际总资产和可用现金")
                    if set(value.sellable_quantities) != {p["ts_code"] for p in positions}:
                        raise ValueError("须明确每一笔持仓的实际可卖数量；无持仓时应为空表")
                    at = now()
                    for p in positions:
                        available = value.sellable_quantities[p["ts_code"]]
                        if not 0 <= available <= p["quantity"] or (p["buy_date"] == at.date().isoformat() and available):
                            raise ValueError("可卖数量超出持仓或违反买入当日 T+1")
                    confirmation = dict(id=uuid4().hex, created_at=at.isoformat(), revision=revision,
                                        input_hash=account_input_hash(profile, positions),
                                        frozen_cash=money(value.frozen_cash), other_assets=money(value.other_assets),
                                        sellable_quantities=value.sellable_quantities,
                                        positions_complete=True, corporate_actions_checked=True)
                    book.db.execute("INSERT INTO intraday_confirmations VALUES(?,?)", (confirmation["id"], json.dumps(confirmation)))
            except BaseException:
                owner.__exit__(None, None, None)
                raise
            finally:
                book.close()
            self.confirmation, self.owner = confirmation, owner
            self.stop_event.clear()
            self.last_error, self.latest = None, None
            self.thread = threading.Thread(target=self._run, daemon=True, name="intraday-condition-monitor")
            self.thread.start()
        return {"running": True}

    def stop(self):
        with self.lock:
            self.stop_event.set()
        return {"running": bool(self.thread and self.thread.is_alive()), "stopping": True}

    def close(self):
        self.stop()
        if self.thread:
            self.thread.join(timeout=1)

    def _run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.tick()
                    self.last_error = None
                except Exception as exc:
                    self.last_error = str(exc)
                    self.latest = None
                self.stop_event.wait(30)
        finally:
            if self.owner:
                self.owner.__exit__(None, None, None)
                self.owner = None

    def tick(self):
        at = now()
        if self.rules != source_hash():
            raise ValueError("监控代码版本已改变，停止使用旧进程结果")
        if market_phase(at) != "连续竞价":
            self.latest = dict(status="paused", checked_at=at.isoformat(), entries=[], holdings=[], reasons=[market_phase(at)])
            return
        book = _book(self.root)
        try:
            book.db.execute("BEGIN")
            profile, positions, revision, plan = _inputs(book)
            issued = [json.loads(r[0]) for r in book.db.execute("SELECT body FROM intraday_alerts")]
            book.db.rollback()
        finally:
            book.close()
        codes = sorted({p["ts_code"] for p in positions} | {p["ts_code"] for p in (plan or {}).get("entries", [])})
        if len(codes) > 50:
            raise ValueError("盘中监控最多50只，不能静默遗漏已有持仓")
        allocation = {a["ts_code"]: a["quantity"] for a in issued
                      if a.get("confirmation_id") == (self.confirmation or {}).get("id")
                      and a["kind"] == "入场条件触发" and a["quantity"] > 0}
        original_plan = plan
        if plan and allocation:
            plan = json.loads(json.dumps(plan))
            for entry in plan.get("entries", []):
                entry["quantity"] = min(entry.get("quantity", 0), allocation.get(entry["ts_code"], 0))
        collected = self.collector(self.root, codes)
        at = now()
        if self.stop_event.is_set() or market_phase(at) != "连续竞价":
            self.latest = None
            return
        result = evaluate(profile, positions, revision, plan, self.confirmation, collected["quotes"], at)
        if allocation:
            result["reasons"].append("本次确认已发入场提醒，后续不向其他候选重复分配资金；重新核对账户并开始才可重分配")
        result.update(quote_receipts=collected.get("receipts", []), source_hash=self.rules, id=uuid4().hex)
        deadlines = [at + timedelta(seconds=90)]
        for q in collected["quotes"].values():
            if q.get("status") == "ok":
                for value in q.get("source_observed_at", {}).values():
                    stamp = datetime.fromisoformat(value)
                    if stamp.tzinfo is not None:
                        deadlines.append(stamp + timedelta(seconds=90))
        result["valid_until"] = min(deadlines).isoformat()
        book = _book(self.root)
        try:
            with self.lock, book.db:
                if self.stop_event.is_set():
                    self.latest = None
                    return
                book.db.execute("BEGIN IMMEDIATE")
                if now() > min(deadlines) or market_phase(now()) != "连续竞价":
                    raise ValueError("提交前行情已过期或交易时段结束，本批未发布")
                current_profile, current_positions, current_revision, current_plan = _inputs(book)
                if current_revision != revision or account_input_hash(current_profile, current_positions) != account_input_hash(profile, positions) or (current_plan or {}).get("version_id") != (original_plan or {}).get("version_id") or self.rules != source_hash():
                    raise ValueError("报价期间账户或计划改变，本批结果未发布，请重新核对")
                book.db.execute("INSERT INTO intraday_ticks VALUES(?,?,?)", (result["id"], result["checked_at"], json.dumps(result)))
                for row in result["holdings"] + result["entries"]:
                    if not row.get("trigger"):
                        continue
                    key = f"{at.date()}:{revision}:{result['plan_version']}:{self.confirmation['id']}:{row['ts_code']}:{row['trigger']}:{row['quantity'] > 0}"
                    ident = hashlib.sha256(key.encode()).hexdigest()
                    alert = dict(id=ident, ts_code=row["ts_code"], kind=row["trigger"], message="；".join(row["reasons"]) or "条件已触发，请人工确认；不是成交通知",
                                 price=row["price"], quantity=row["quantity"], created_at=at.isoformat(), plan_version=result["plan_version"], tick_id=result["id"])
                    alert["confirmation_id"] = self.confirmation["id"]
                    book.db.execute("INSERT OR IGNORE INTO intraday_alerts VALUES(?,?,?)", (ident, at.isoformat(), json.dumps(alert)))
        finally:
            book.close()
        self.latest = result

    def state(self):
        book = _book(self.root)
        try:
            book.db.execute("BEGIN")
            profile, positions, revision, plan = _inputs(book)
            alerts = [json.loads(r[0]) for r in book.db.execute("SELECT body FROM intraday_alerts ORDER BY recorded_at DESC,id DESC LIMIT 100")]
            book.db.rollback()
        finally:
            book.close()
        at = now()
        latest = json.loads(json.dumps(self.latest)) if self.latest else None
        try:
            phase = market_phase(at)
            invalid = self.rules != source_hash()
        except (ValueError, OSError) as exc:
            phase, invalid = str(exc), True
        running = bool(self.thread and self.thread.is_alive())
        stopping = bool(running and self.stop_event.is_set())
        if latest and (not running or stopping or phase != "连续竞价" or invalid
                       or not 0 <= (at - datetime.fromisoformat(latest["checked_at"])).total_seconds() <= 90
                       or (latest.get("valid_until") and at > datetime.fromisoformat(latest["valid_until"]))
                       or latest.get("account_revision") != revision
                       or latest.get("plan_version") != (plan or {}).get("version_id")):
            latest.update(status="stale", reasons=["监控已停止、数据过期或账户/计划已变化，本批数量失效"])
            for row in latest.get("entries", []) + latest.get("holdings", []):
                row.update(quantity=0, status="已失效")
        return dict(running=running, stopping=stopping, interval_seconds=30, market_status=phase,
                    account=dict(profile=profile.model_dump(mode="json"), positions=positions, revision=revision),
                    confirmation=self.confirmation, latest=latest, alerts=alerts, last_error=self.last_error)
