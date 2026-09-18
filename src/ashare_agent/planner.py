"""Persistent, conditional daily plans. User position snapshots are never simulated fills."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .current_data import (
    SHANGHAI,
    CurrentClient,
    mainboard_code,
    now_iso,
    possible_limit_state,
    public_code,
    risk_name,
)
from .current_screen import verify_current


def dec(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("金额必须为有效数值") from exc
    if not result.is_finite():
        raise ValueError("金额必须为有限数值")
    return result


def money(value):
    return str(dec(value).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def next_session_expired(as_of, cutoff):
    from .session_calendar import next_sessions

    following = next_sessions(as_of, 1)[0]
    current = cutoff.astimezone(SHANGHAI)
    return current.date().isoformat() > following or (
        current.date().isoformat() == following and current.hour >= 16
    )


_RULE_FILES = (
    "planner.py",
    "context_feed.py",
    "market_context.py",
    "financial_context.py",
    "current_data.py",
    "current_screen.py",
    "session_calendar.py",
    "intraday.py",
    "live_quotes.py",
)


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def current_rule_hash():
    """Return the identity of the rules used to make an executable plan."""
    from .session_calendar import calendar_manifest

    digest = hashlib.sha256()
    for name in _RULE_FILES:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(Path(__file__).with_name(name).read_bytes())
        digest.update(b"\0")
    digest.update(b"calendar_manifest\0")
    digest.update(_canonical_json(calendar_manifest()))
    return digest.hexdigest()


def _rule_staleness_reason(version, expected_hash):
    source_hash = version.get("source_hash")
    if not isinstance(source_hash, str) or not source_hash:
        return "计划缺少可验证的规则身份，请重新复核。"
    try:
        if len(source_hash) != 64:
            raise ValueError
        bytes.fromhex(source_hash)
    except ValueError:
        return "计划规则身份格式无效，当前规则版本可能已变化，请重新复核。"
    if source_hash != expected_hash:
        return "计划使用的规则版本已变化，请重新复核。"
    return None


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    capital: Decimal | None = Field(default=None, gt=0, le=1_000_000_000)
    cash: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    risk_pct: Decimal = Field(default=Decimal(".005"), gt=0, le=Decimal(".02"))
    max_position_pct: Decimal = Field(default=Decimal(".1"), gt=0, le=Decimal(".3"))
    max_gross_pct: Decimal = Field(default=Decimal(".6"), gt=0, le=1)
    portfolio_risk_pct: Decimal = Field(default=Decimal(".02"), gt=0, le=Decimal(".1"))
    stop_pct: Decimal = Field(default=Decimal(".06"), ge=Decimal(".01"), le=Decimal(".2"))
    reward_r: Decimal = Field(default=Decimal("2"), ge=1, le=5)
    max_hold_sessions: int = Field(default=20, ge=2, le=120)
    entry_band_pct: Decimal = Field(default=Decimal(".01"), gt=0, le=Decimal(".03"))
    weights: dict[str, Decimal] = {
        "mechanical": Decimal(".7"),
        "market": Decimal(".2"),
        "news": Decimal(".1"),
        "community": Decimal("0"),
    }

    @model_validator(mode="after")
    def bounds(self):
        if (self.capital is None) != (self.cash is None):
            raise ValueError("资金与可用现金必须同时填写")
        if self.capital is not None and self.cash > self.capital:
            raise ValueError("可用现金不能超过账户总资产")
        if self.max_position_pct > self.max_gross_pct or self.risk_pct > self.portfolio_risk_pct:
            raise ValueError("单股仓位/风险不能超过组合上限")
        if set(self.weights) != {"mechanical", "market", "news", "community"}:
            raise ValueError("需要四类明确权重")
        if any(not v.is_finite() or v < 0 or v > 1 for v in self.weights.values()):
            raise ValueError("权重必须为有效非负比例")
        if sum(self.weights.values()) != 1 or self.weights["community"] > Decimal(".05"):
            raise ValueError("权重总和须为 100%，未经校准的社区权重最多 5%")
        return self


class AccountConfirmationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    as_of: datetime
    frozen_cash: Decimal = Field(ge=0, le=1_000_000_000)
    other_assets: Decimal = Field(ge=0, le=1_000_000_000)
    other_assets_note: str = Field(default="", max_length=500)
    positions_complete: StrictBool
    expected_account_revision: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def validate_statement(self):
        if not self.positions_complete:
            raise ValueError("请先完整录入所有持仓；没有持仓时也需明确声明")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("账户快照时间必须带时区")
        if self.other_assets > 0 and not self.other_assets_note.strip():
            raise ValueError("其他资产非零时请填写构成说明")
        return self


def account_input_hash(profile, positions):
    return hashlib.sha256(_canonical_json({
        "profile": profile.model_dump(mode="json"), "positions": positions,
    })).hexdigest()


def reconcile_account(statement, holdings, as_of, cutoff):
    """A manual completeness statement plus independently verified closing marks."""
    reasons = list(statement["reasons"])
    confirmation = statement.get("confirmation")
    result = dict(
        status="blocked", new_positions_allowed=False, reasons=reasons,
        confirmation=confirmation, holdings_value=None, calculated_total=None,
        reconciliation_delta=None, tolerance="1.00",
    )
    if not confirmation:
        return result
    snapshot_at = datetime.fromisoformat(confirmation["as_of"]).astimezone(SHANGHAI)
    recorded_at = datetime.fromisoformat(confirmation["recorded_at"]).astimezone(SHANGHAI)
    if snapshot_at.date().isoformat() != as_of or snapshot_at.hour < 15 or recorded_at > cutoff:
        reasons.append("账户快照须对应本次行情日收盘，且在本次复核前已录入")
    frozen_positions = {p["ts_code"]: p["quantity"] for p in confirmation["positions"]}
    marks = {p["ts_code"]: p for p in holdings}
    if set(frozen_positions) != set(marks) or any(
        marks[c]["quantity"] != q for c, q in frozen_positions.items() if c in marks
    ):
        reasons.append("已确认持仓与本次估值持仓不一致")
        return result
    if any(p.get("close") is None or p.get("action") == "数据待核验" for p in holdings):
        reasons.append("持仓估值不完整，不能用成本价代替收盘市值核对账户")
        return result
    value = sum((dec(p["close"]) * p["quantity"] for p in holdings), Decimal(0))
    calculated = (
        dec(confirmation["available_cash"]) + dec(confirmation["frozen_cash"])
        + dec(confirmation["other_assets"]) + value
    )
    delta = dec(confirmation["total_assets"]) - calculated
    result.update(holdings_value=money(value), calculated_total=money(calculated), reconciliation_delta=money(delta))
    if abs(delta) > Decimal("1.00"):
        reasons.append("账户总资产与现金、冻结资金、其他资产及持仓市值的差额超过 1 元，请核对是否漏录或时点不一致")
    if dec(confirmation["frozen_cash"]) > 0 or dec(confirmation["other_assets"]) > 0:
        reasons.append("冻结资金或其他资产的风险尚未建模；金额可核对，暂不分配新开仓数量")
    if not reasons:
        result.update(status="ready", new_positions_allowed=True)
    return result


class PlanStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "plans.sqlite", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS positions(code TEXT PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, kind TEXT, body TEXT);
          CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY, created_at TEXT, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS account_confirmations(id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, body TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def profile(self):
        row = self.db.execute("SELECT body FROM settings WHERE id=1").fetchone()
        return Profile.model_validate_json(row[0]) if row else Profile()

    def save_profile(self, body):
        profile = Profile.model_validate(body)
        encoded = profile.model_dump_json()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(1,?)", (encoded,))
            self.db.execute(
                "INSERT INTO audit(recorded_at,kind,body) VALUES(?,?,?)", (now_iso(), "profile", encoded)
            )
        return profile.model_dump(mode="json")

    def positions(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT body FROM positions ORDER BY code")]

    def confirm_account(self, body):
        value = AccountConfirmationInput.model_validate(body)
        now = datetime.now(SHANGHAI)
        snapshot_at = value.as_of.astimezone(SHANGHAI)
        if snapshot_at > now:
            raise ValueError("账户快照时间不能晚于实际录入时间")
        if snapshot_at.hour < 15:
            raise ValueError("请使用交易日收盘后的账户快照")
        from .session_calendar import sessions

        day = snapshot_at.date().isoformat()
        if sessions(day, day) != [day]:
            raise ValueError("账户快照须为已知交易日收盘")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.account_revision() != value.expected_account_revision:
                raise ValueError("账户资料已改变，请刷新后核对最新资金与持仓")
            profile, positions = self.profile(), self.positions()
            if profile.capital is None or profile.cash is None:
                raise ValueError("请先保存账户总资产和可用现金")
            statement = dict(
                confirmation_id=uuid4().hex, source="USER_CONFIRMED_ACCOUNT_SNAPSHOT",
                as_of=snapshot_at.isoformat(), recorded_at=now.isoformat(),
                total_assets=money(profile.capital), available_cash=money(profile.cash),
                frozen_cash=money(value.frozen_cash), other_assets=money(value.other_assets),
                other_assets_note=value.other_assets_note.strip(), positions_complete=True,
                positions_count=len(positions), positions=positions,
                account_input_hash=account_input_hash(profile, positions),
            )
            cursor = self.db.execute(
                "INSERT INTO audit(recorded_at,kind,body) VALUES(?,?,?)",
                (now.isoformat(), "account_confirmation", json.dumps(statement, ensure_ascii=False)),
            )
            statement["account_revision"] = cursor.lastrowid
            self.db.execute(
                "INSERT INTO account_confirmations VALUES(?,?,?)",
                (statement["confirmation_id"], now.isoformat(), json.dumps(statement, ensure_ascii=False)),
            )
        return statement

    def account_statement(self, cutoff=None):
        cutoff = cutoff or datetime.now(SHANGHAI)
        row = self.db.execute("SELECT body FROM account_confirmations ORDER BY rowid DESC LIMIT 1").fetchone()
        if not row:
            return dict(status="unconfirmed", confirmation=None, reasons=["账户资料尚未核对：请完整录入持仓并确认收盘快照"])
        value = json.loads(row[0])
        reasons = []
        if value["account_revision"] != self.account_revision() or value["account_input_hash"] != account_input_hash(self.profile(), self.positions()):
            reasons.append("资金、参数或持仓已修改，账户核对已失效")
        snapshot_at = datetime.fromisoformat(value["as_of"]).astimezone(SHANGHAI)
        recorded_at = datetime.fromisoformat(value["recorded_at"]).astimezone(SHANGHAI)
        try:
            expired = next_session_expired(snapshot_at.date().isoformat(), cutoff)
        except ValueError:
            expired = True
        if expired:
            reasons.append("账户快照不是本次交易日，请按收盘资料重新核对")
        if snapshot_at > cutoff or recorded_at > cutoff:
            reasons.append("账户快照或录入时间晚于本次复核")
        return dict(status="stale" if reasons else "confirmed", confirmation=value, reasons=reasons)

    def save_position(self, body):
        if set(body) != {"ts_code", "quantity", "cost_price", "buy_date"}:
            raise ValueError("持仓需要代码、数量、成本和买入日期")
        code = public_code(mainboard_code(body["ts_code"]))
        quantity = body["quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 0 <= quantity <= 100_000_000:
            raise ValueError("数量必须为非负整数股数")
        cost = dec(body["cost_price"])
        acquired = date.fromisoformat(body["buy_date"])
        if not 0 < cost < 100_000 or acquired > datetime.now(SHANGHAI).date():
            raise ValueError("成本或买入日期无效")
        profile = self.profile()
        entry = dict(
            ts_code=code,
            quantity=quantity,
            cost_price=money(cost),
            buy_date=acquired.isoformat(),
            recorded_at=now_iso(),
            source="USER_POSITION_SNAPSHOT",
            stop_price=money(cost * (1 - profile.stop_pct)),
            take_profit_price=money(cost * (1 + profile.stop_pct * profile.reward_r)),
            max_hold_sessions=profile.max_hold_sessions,
        )
        old = next((p for p in self.positions() if p["ts_code"] == code), None)
        if old and old["cost_price"] == entry["cost_price"] and old["buy_date"] == entry["buy_date"]:
            for key in ("stop_price", "take_profit_price", "max_hold_sessions"):
                entry[key] = old[key]  # A daily update never loosens an existing stop.
        with self.db:
            self.db.execute(
                "INSERT INTO audit(recorded_at,kind,body) VALUES(?,?,?)",
                (entry["recorded_at"], "position_snapshot", json.dumps(entry, ensure_ascii=False)),
            )
            if quantity:
                self.db.execute("INSERT OR REPLACE INTO positions VALUES(?,?)", (code, json.dumps(entry)))
            else:
                self.db.execute("DELETE FROM positions WHERE code=?", (code,))
        return entry

    def add_evidence(self, item):
        encoded = json.dumps(item, ensure_ascii=False)
        with self.db:
            old = self.db.execute("SELECT body FROM evidence WHERE id=?", (item["evidence_id"],)).fetchone()
            if old and old[0] != encoded:
                raise ValueError("不可变证据 ID 冲突")
            self.db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?)", (item["evidence_id"], encoded))

    def manual_evidence(self, body):
        from .research import capture_evidence

        if set(body) != {"ts_code", "kind", "url", "published_at", "content"} or body["kind"] not in {
            "news",
            "community",
        }:
            raise ValueError("证据字段或类型无效")
        if not 1 <= len(body["content"]) <= 6000 or len(body["url"]) > 2000:
            raise ValueError("证据内容或链接过长")
        code = public_code(mainboard_code(body["ts_code"]))
        item = capture_evidence(
            url=body["url"], published_at=body["published_at"], content=body["content"], symbol=code
        )
        item.update(
            ts_code=code,
            kind=body["kind"],
            title=body["content"][:80],
            quality="manual_unverified",
            sentiment=None,
            risk_flags=[],
        )
        self.add_evidence(item)
        return item

    def state(self):
        # The rendered inputs and revision must describe one SQLite read snapshot.
        if self.db.in_transaction:
            return self._state()
        self.db.execute("BEGIN")
        try:
            return self._state()
        finally:
            self.db.rollback()

    def _state(self):
        rows = self.db.execute(
            "SELECT body FROM reviews ORDER BY created_at DESC,id DESC LIMIT 30"
        ).fetchall()
        versions = [json.loads(r[0]) for r in rows]
        versions = [v for v in versions if self.visible(v)]
        latest = dict(versions[0]) if versions else None
        now = datetime.now(SHANGHAI)
        try:
            expired = next_session_expired(latest["as_of"], now) if latest else False
        except ValueError:
            expired = True
        stale_reasons = []
        if latest and (latest.get("account_revision") != self.account_revision() or expired):
            stale_reasons.append("计划已过期或账户参数/持仓已修改，旧计划失效，请重新复核。")
        if latest:
            try:
                expected_hash = current_rule_hash()
            except (ImportError, OSError, TypeError, ValueError) as exc:
                rule_reason = f"当前交易日历规则不可验证，计划已阻止，请修复后重新复核：{exc}"
            else:
                rule_reason = _rule_staleness_reason(latest, expected_hash)
            if rule_reason:
                stale_reasons.append(rule_reason)
        if latest and stale_reasons:
            latest = json.loads(json.dumps(latest))
            latest.update(status="blocked", stale=True)
            warnings = latest.get("warnings", [])
            latest["warnings"] = stale_reasons + (warnings if isinstance(warnings, list) else [])
            for entry in latest.get("entries", []):
                entry.update(quantity=0, status="已失效")
            for holding in latest.get("holdings", []):
                holding.update(sell_quantity=0, action="需重新复核")
        return dict(
            profile=self.profile().model_dump(mode="json"),
            positions=self.positions(),
            latest=latest,
            history=[{k: v[k] for k in ("version_id", "created_at", "as_of", "status")} for v in versions],
            account_revision=self.account_revision(),
            account_check=self.account_statement(now),
        )

    def publish(self, result, expected_account_revision=None):
        expected = (
            result.get("account_revision")
            if expected_account_revision is None
            else expected_account_revision
        )
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.account_revision()
            if expected is None:
                expected = current
            if result.get("account_revision") is not None and result["account_revision"] != expected:
                raise ValueError("计划账户修订号与复核开始时不一致，未发布，请重新复核")
            if current != expected:
                raise ValueError("账户参数或持仓在复核期间已修改，旧计划未发布，请重新复核")
            prior = self.db.execute("SELECT body FROM reviews WHERE id=?", (result["version_id"],)).fetchone()
            if prior and not self.visible(json.loads(prior[0])):
                self.db.execute("DELETE FROM reviews WHERE id=?", (result["version_id"],))
            self.db.execute(
                "INSERT OR IGNORE INTO reviews VALUES(?,?,?)",
                (
                    result["version_id"],
                    result["created_at"],
                    json.dumps(result, ensure_ascii=False, allow_nan=False),
                ),
            )

    def account_revision(self):
        return self.db.execute("SELECT coalesce(max(id),0) FROM audit").fetchone()[0]

    def visible(self, version):
        ident = version.get("web_job_id")
        if not ident:
            return True
        path = self.root.parent / "web-runs" / ident / "job.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("status") == "completed"
        except (OSError, ValueError):
            return False


def weighted_interval(values, weights):
    weights = {k: dec(v) for k, v in weights.items()}
    low = sum(dec(values[k]) * v for k, v in weights.items() if values.get(k) is not None)
    missing = sum(v for k, v in weights.items() if values.get(k) is None)
    return float(low), float(low + missing)


def fee(notional):
    return max(Decimal("5"), notional * Decimal(".0003")) + notional * Decimal(".00001")


def stress_risk(mark, stop, quantity, entry_fee=False):
    cost = dec(mark) * quantity
    proceeds = dec(stop) * Decimal(".99") * quantity
    return (
        max(Decimal(0), cost - proceeds)
        + fee(proceeds)
        + proceeds * Decimal(".0005")
        + (fee(cost) if entry_fee else 0)
    )


def risk_quantity(entry, stop, cash, allocation, risk_budget, adv):
    """Round to MAIN lots; stress loss includes commission, stamp and 1% adverse exit buffer."""
    entry, stop = dec(entry), dec(stop)
    stressed_exit = stop * Decimal(".99")
    distance = entry - stressed_exit
    if distance <= 0:
        raise ValueError("止损必须低于买入价")
    initial = min(
        dec(cash) / entry,
        dec(allocation) / entry,
        dec(risk_budget) / distance,
        dec(adv) * Decimal(".001") / entry,
    )
    quantity = max(0, int((initial / 100).to_integral_value(rounding=ROUND_DOWN)) * 100)
    while quantity > 0:
        cost = entry * quantity
        loss = stress_risk(entry, stop, quantity, True)
        if cost + fee(cost) <= cash and cost <= allocation and loss <= risk_budget:
            return quantity, cost + fee(cost), loss
        quantity -= 100
    return 0, Decimal(0), Decimal(0)


def holding_review(position, raw, quote, name, benchmark, cutoff):
    result = {
        **position,
        "close": None,
        "held_sessions": None,
        "action": "数据待核验",
        "reason": "",
        "sell_quantity": 0,
        "sell_condition": "",
        "exit_trigger": None,
        "trigger_reason": "",
        "risk_warning": "",
        "risk_flags": [],
    }
    try:
        as_of = benchmark.index[-1]
        verify_current(quote, raw, as_of, name, cutoff)
        last = dec(raw.close.iloc[-1])
        stop, target = dec(position["stop_price"]), dec(position["take_profit_price"])
        age = sum(day >= position["buy_date"] for day in benchmark.index)
        result.update(close=money(last), held_sessions=age)
        if as_of < position["buy_date"]:
            raise ValueError("行情早于持仓日期")
        if last <= stop:
            exit_trigger, trigger_reason = "止损触发", "收盘价触及止损；下一可交易时段人工处理，不能假设止损价成交"
        elif last >= target:
            exit_trigger, trigger_reason = "止盈触发", "收盘价达到预设收益目标；下一可交易时段复核退出"
        elif age >= position["max_hold_sessions"]:
            exit_trigger, trigger_reason = "到期复核", "已达最长持有交易日，按计划复核退出；延长必须另作明确记录"
        else:
            exit_trigger, trigger_reason = None, ""
        risk = risk_name(name)
        if risk:
            risk_warning = (
                "当前名称出现风险警示，需人工核验公告、风险状态和实际可卖数量；"
                "不得仅凭名称变更推断允许成交"
            )
            result.update(risk_warning=risk_warning, risk_flags=["名称风险警示"])
        action = exit_trigger or ("风险复核" if risk else "继续观察")
        reason_parts = [trigger_reason] if trigger_reason else [
            "未触及收盘退出条件；下次收盘复核，盘中风险仍需人工监看"
        ]
        if risk:
            reason_parts.append(risk_warning)
        if as_of == position["buy_date"]:
            reason_parts.append("买入当日受 T+1 限制，当前不能据此卖出")
        result.update(
            action=action,
            reason="；".join(reason_parts),
            exit_trigger=exit_trigger,
            trigger_reason=trigger_reason,
        )
        if exit_trigger and as_of != position["buy_date"] and not risk:
            result["sell_quantity"] = position["quantity"]
            result["sell_condition"] = "下一交易日人工复核后拟退出数量；以券商实际可卖数量为准"
        elif exit_trigger:
            constraints = []
            if risk:
                constraints.append("风险状态、公告和实际可卖数量需人工核验")
            if as_of == position["buy_date"]:
                constraints.append("买入当日受 T+1 限制")
            result["sell_condition"] = "；".join(constraints) + "；确认后以券商实际可卖数量为准"
        elif risk:
            result["sell_condition"] = "需人工核验风险状态和实际可卖数量；不得据此自动卖出"
        else:
            result["sell_condition"] = "未触发退出条件；继续观察并以券商实际可卖数量为准"
    except (ValueError, TypeError, KeyError) as exc:
        result["reason"] = str(exc)
    return result


def latest_screen(root):
    for path in sorted((Path(root) / "runtime/web-runs").glob("*/job.json"), reverse=True):
        job = json.loads(path.read_text(encoding="utf-8"))
        folder = path.parent / "screen" if job.get("kind") == "planner" else path.parent
        source = folder / "result.json"
        if job.get("kind") in {"screen", "planner"} and job.get("status") == "completed" and source.exists():
            result = json.loads(source.read_text(encoding="utf-8"))
            if result.get("status") in {"partial", "complete"} and not result.get("synthetic"):
                return folder, result
    raise ValueError("请先完成一次真实选股，再生成持续计划")


def review(root, output, progress=lambda _: None):
    """Acquire current evidence, then freeze a decision cutoff and publish a new version."""
    from .context_feed import _sentiment, fetch_context
    from .financial_context import fetch_financial_context
    from .market_context import fetch_market_context

    root, output = Path(root), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    book = PlanStore(root / "runtime/planner")
    client = CurrentClient(root / "runtime/current-cache")
    try:
        review_account_revision = book.account_revision()
        profile = book.profile()
        rule_hash = current_rule_hash()
        positions = book.positions()
        current_date = datetime.now(SHANGHAI).date().isoformat()
        try:
            source, selection = latest_screen(root)
        except ValueError:
            source, selection = output / "screen", None
        if selection is None or selection["as_of"] != current_date:
            if datetime.now(SHANGHAI).hour < 16:
                raise ValueError("盘中不生成收盘计划；请在 16:00 后复核")
            from .current_screen import screen

            progress("缺少今日选股结果，先更新数据源主板目录，首次需要数十分钟")
            source = output / "screen"
            selection = screen(source, progress=progress)
            if selection["status"] not in {"partial", "complete"}:
                raise ValueError("今日选股数据未就绪；保留旧计划但不能视为仍然有效")
        benchmark = pd.read_parquet(source / "benchmark.parquet").set_index("trade_date", drop=False)
        from .current_screen import validate_selection_inputs

        validate_selection_inputs(selection, benchmark, datetime.now(SHANGHAI))
        as_of = selection["as_of"]
        codes = sorted({r["ts_code"] for r in selection["candidates"]} | {p["ts_code"] for p in positions})
        if len(codes) > 50:
            raise ValueError("本版持续跟踪最多 50 只，请缩小候选或持仓范围")
        progress(f"复核 {len(codes)} 只候选及全部持仓")
        quotes = client.quotes([mainboard_code(c) for c in codes])
        histories, errors, adjusted_holdings = {}, {}, {}
        for code in codes:
            try:
                histories[code] = client.bars(mainboard_code(code), end=as_of)
                if any(p["ts_code"] == code for p in positions):
                    adjusted_holdings[code], _ = client.bars(mainboard_code(code), adjust="qfq", end=as_of)
            except (ValueError, RuntimeError, TypeError, OSError) as exc:
                errors[code] = str(exc)
        progress("读取新闻和社区证据，缺失项保留未知状态")
        context = dict(evidence=[], warnings=[], coverage={})
        for offset in range(0, len(codes), 30):
            part = fetch_context(codes[offset : offset + 30], client, datetime.now(SHANGHAI))
            context["evidence"].extend(part["evidence"])
            context["warnings"].extend(part["warnings"])
            context["coverage"].update(part["coverage"])
        for item in context["evidence"]:
            book.add_evidence(item)
        progress("读取国内外宏观事件样本，单独展示来源及正反影响情景")
        market_context = fetch_market_context(client)
        progress("读取候选及持仓财务摘要，保留缺失、报告期和真实获取时间")
        financial_context = fetch_financial_context(codes, client)
        cutoff = datetime.now(SHANGHAI)
        evidence = []
        for row in book.db.execute("SELECT body FROM evidence"):
            item = json.loads(row[0])
            stamps = [datetime.fromisoformat(item[k]) for k in ("published_at", "available_at", "fetched_at")]
            if (
                item["ts_code"] in codes
                and all(t.tzinfo and t <= cutoff for t in stamps)
                and (cutoff - stamps[0]).days <= 7
            ):
                # Recompute derived labels with this review's rule version; never alter
                # the archived evidence's timestamps, text or content fingerprint.
                if item.get("quality") in {"aggregator_timestamp", "community_timestamp"}:
                    score, flags = _sentiment(item["title"])
                    item.update(sentiment=score, risk_flags=flags if item["kind"] == "news" else [])
                evidence.append(item)
        unique = {}
        for item in sorted(evidence, key=lambda e: e["available_at"]):
            identity = hashlib.sha256(f"{item.get('title', '')}\n{item['content']}".encode()).hexdigest()
            unique.setdefault((item["ts_code"], identity, item["kind"]), item)
        evidence = list(unique.values())
        result = dict(
            version_id=uuid4().hex,
            created_at=cutoff.isoformat(),
            as_of=as_of,
            status="review",
            entries=[],
            holdings=[],
            evidence=evidence,
            market_context=market_context,
            financial_context=financial_context,
            changes=[],
            factors={},
            profile=profile.model_dump(mode="json"),
            source_screen=str(source.name),
            warnings=[
                "日频条件计划；止损价不是保证成交价。跳空、跌停、停牌和 T+1 可使实际亏损超预算。",
                "组合权重是未经 OOS 校准的研究预设；仅展示评分，不决定数量。",
                "现金与持仓来自手工快照，成交后需同步更新；不会自动记作已买入或已卖出。",
                "计划仅用于独立交易日历的下一交易日，最迟该日 16:00 失效；入场前复核最新价格、公告及可交易状态。",
                "成本采用简化佣金/印花税模型及额外退出缓冲；请按券商实际费率核对。",
            ]
            + context["warnings"],
        )
        fresh = as_of == cutoff.date().isoformat() and cutoff.hour >= 16
        result["coverage"] = context["coverage"]
        if not fresh:
            result["warnings"].append("行情不是今日完整收盘数据，暂停所有新开仓数量；先刷新真实选股")
        if selection["status"] == "partial":
            result["warnings"].append("来源选股有数据缺失，本次计划只覆盖已核验子集")
        market = float(benchmark.close.iloc[-1] > benchmark.close.iloc[-120:].mean())
        result["factors"] = dict(
            market=market,
            market_label="指数趋势允许" if market else "指数弱势：暂停新开仓",
            weights=profile.model_dump(mode="json")["weights"],
            weight_mode="研究展示，不用于仓位",
        )
        for p in positions:
            if p["ts_code"] in histories:
                raw, name = histories[p["ts_code"]]
                item = holding_review(
                    p, raw, quotes.get(mainboard_code(p["ts_code"])), name, benchmark, cutoff
                )
                try:
                    adjusted = adjusted_holdings[p["ts_code"]]
                    since = raw.index[raw.index >= p["buy_date"]]
                    if not len(since) or p["buy_date"] < raw.index[0] or since[0] not in adjusted.index:
                        raise ValueError("买入时复权基准缺失")
                    if adjusted.index[-1] != as_of or raw.index[-1] != as_of:
                        raise ValueError("复权与原始价格末日未对齐")
                    before = float(adjusted.loc[since[0], "close"] / raw.loc[since[0], "close"])
                    after = float(adjusted.loc[as_of, "close"] / raw.loc[as_of, "close"])
                    if abs(after / before - 1) > 0.002:
                        raise ValueError("持有期可能发生除权除息，须核对成本和止盈止损基准")
                except (ValueError, KeyError, RuntimeError, TypeError, OSError) as exc:
                    item.update(action="数据待核验", reason=str(exc), sell_quantity=0, sell_condition="")
                if not fresh:
                    item.update(
                        action="数据待核验",
                        reason="旧收盘数据只能查看原计划，先更新行情",
                        sell_quantity=0,
                        sell_condition="",
                    )
            else:
                item = {
                    **p,
                    "close": None,
                    "held_sessions": None,
                    "action": "数据待核验",
                    "reason": errors.get(p["ts_code"], "行情缺失"),
                    "sell_quantity": 0,
                    "sell_condition": "",
                    "exit_trigger": None,
                    "trigger_reason": "",
                    "risk_warning": "",
                    "risk_flags": [],
                }
            news_alerts = sorted(
                {
                    flag
                    for e in evidence
                    if e["ts_code"] == p["ts_code"]
                    and e["kind"] == "news"
                    and e.get("quality") == "aggregator_timestamp"
                    for flag in e.get("risk_flags", [])
                }
            )
            if news_alerts:
                item["reason"] += "；重要新闻词待核实：" + "、".join(news_alerts)
                if item["action"] == "继续观察":
                    item["action"] = "新闻待核实"
            item["news_alerts"] = news_alerts
            result["holdings"].append(item)
        capital = profile.capital or Decimal(0)
        cash = profile.cash or Decimal(0)
        valuation_known = all(p["close"] is not None and p["action"] != "数据待核验" for p in result["holdings"])
        exposure = sum((dec(p["close"]) * p["quantity"] for p in result["holdings"]), Decimal(0)) if valuation_known else None
        open_risk = sum((
            stress_risk(p["close"], p["stop_price"], p["quantity"])
            for p in result["holdings"]
        ), Decimal(0)) if valuation_known else None
        gross_left = max(Decimal(0), capital * profile.max_gross_pct - exposure) if exposure is not None else Decimal(0)
        risk_left = max(Decimal(0), capital * profile.portfolio_risk_pct - open_risk) if open_risk is not None else Decimal(0)
        result["portfolio"] = dict(
            valuation_known=valuation_known,
            exposure=None if exposure is None else money(exposure),
            open_risk=None if open_risk is None else money(open_risk),
        )
        result["account_check"] = reconcile_account(book.account_statement(cutoff), result["holdings"], as_of, cutoff)
        result["warnings"] += result["account_check"]["reasons"]
        portfolio_known = all(p["action"] == "继续观察" for p in result["holdings"])
        if profile.capital is None:
            result["warnings"].append("尚未设置资金和风险偏好：显示条件价格，股数为 0，不使用假定的个人资产")
        for candidate in selection["candidates"]:
            code = candidate["ts_code"]
            if any(p["ts_code"] == code for p in positions):
                continue
            close = dec(candidate["close"])
            low, high = (
                dec(money(close * (1 - profile.entry_band_pct))),
                dec(money(close * (1 + profile.entry_band_pct))),
            )
            stop = dec(money(high * (1 - profile.stop_pct)))
            target = dec(money(high + (high - stop) * profile.reward_r))
            reasons = []
            risk_flags = list(candidate.get("risk_flags") or [])
            limit_state = candidate.get("limit_state", "unknown")
            try:
                raw, name = histories[code]
                verify_current(quotes.get(mainboard_code(code)), raw, as_of, name, cutoff)
                previous_close = raw.close.iloc[-2] if len(raw) >= 2 else None
                limit_state = possible_limit_state(close, previous_close, name)
                if limit_state in {"possible_lower_limit", "possible_upper_limit"}:
                    reasons.append("当前收盘可能处于涨跌停附近，无法证明次日可成交；数量置零")
            except (KeyError, ValueError, TypeError):
                reasons.append("最新行情未通过双源核验")
            news = [
                e
                for e in evidence
                if e["ts_code"] == code
                and e.get("quality") in {"aggregator_timestamp", "community_timestamp"}
                and e.get("sentiment") is not None
            ]
            scores = {
                kind: [float(e["sentiment"]) for e in news if e["kind"] == kind]
                for kind in ("news", "community")
            }
            if context["coverage"].get(code, {}).get("community") != "available":
                scores["community"] = []
            values = {
                "mechanical": candidate["score"],
                "market": market,
                **{k: (sum(v) / len(v) + 1) / 2 if v else None for k, v in scores.items()},
            }
            lower, upper = weighted_interval(values, profile.weights)
            risks = [flag for e in news if e["kind"] == "news" for flag in e.get("risk_flags", [])]
            if risks:
                reasons.append("新闻风险词待人工核实：" + ",".join(sorted(set(risks))))
            decision_eligible = not reasons and fresh and bool(market)
            if not fresh or not market or not portfolio_known:
                reasons.append("日期、市场风险或持仓资料门槛未满足")
            if not profile.capital:
                reasons.append("请先设置实际资金与可用现金")
            if not result["account_check"]["new_positions_allowed"]:
                reasons.append("账户完整性核对未通过，保留条件价格；" + "；".join(result["account_check"]["reasons"]))
            quantity, spent, loss = (0, Decimal(0), Decimal(0))
            if not reasons:
                quantity, spent, loss = risk_quantity(
                    high,
                    stop,
                    cash,
                    min(capital * profile.max_position_pct, gross_left),
                    min(capital * profile.risk_pct, risk_left),
                    candidate["amount20"],
                )
            cash -= spent
            gross_left -= high * quantity
            risk_left -= loss
            if not quantity and not reasons:
                reasons.append("剩余现金、组合风险或整手约束不足")
            result["entries"].append(
                dict(
                    ts_code=code,
                    name=candidate["name"],
                    intraday_contract=1,
                    decision_eligible=decision_eligible,
                    reference_close=money(close),
                    amount20=str(candidate["amount20"]),
                    entry_low=money(low),
                    entry_high=money(high),
                    quantity=quantity,
                    stop_price=money(stop),
                    take_profit_price=money(target),
                    risk_amount=money(loss),
                    score_low=lower,
                    score_high=upper,
                    factor_values=values,
                    risk_flags=risk_flags,
                    risk_review_required=bool(risk_flags),
                    risk_level="elevated" if risk_flags else "ordinary",
                    limit_state=limit_state,
                    status="条件计划" if quantity else "观察",
                    reasons=reasons or ["仅在价格区间内并复核可交易时执行；高开越界不追价，低于区间重新评估"],
                    max_hold_sessions=profile.max_hold_sessions,
                )
            )
        previous = book.state()["latest"]
        if previous:
            old = {p["ts_code"]: p for p in previous["holdings"]}
            for p in result["holdings"]:
                before = old.get(p["ts_code"], {})
                if before.get("action") != p["action"]:
                    result["changes"].append(
                        f"{p['ts_code']}：{before.get('action', '首次跟踪')} → {p['action']}"
                    )
            old_entries = {p["ts_code"]: p for p in previous["entries"]}
            for entry in result["entries"]:
                before = old_entries.get(entry["ts_code"])
                keys = ("entry_low", "entry_high", "quantity", "stop_price", "take_profit_price", "reasons")
                if before is None:
                    result["changes"].append(f"{entry['ts_code']}：新进入观察候选")
                elif any(before.get(k) != entry.get(k) for k in keys):
                    result["changes"].append(
                        f"{entry['ts_code']}：入场区间 {entry['entry_low']}–{entry['entry_high']} 元，"
                        f"计划股数 {before.get('quantity', 0)} → {entry['quantity']}；价格或条件已更新"
                    )
            current_codes = {p["ts_code"] for p in result["entries"]}
            for code in old_entries.keys() - current_codes:
                result["changes"].append(f"{code}：已退出候选名单；已有持仓继续单独复核")
        incomplete = (
            not fresh
            or not result["account_check"]["new_positions_allowed"]
            or errors
            or selection["status"] == "partial"
            or any(p["action"] == "数据待核验" for p in result["holdings"])
        )
        result["status"] = "partial" if incomplete else "complete"
        result["input_hash"] = hashlib.sha256(
            json.dumps(
                dict(
                    profile=result["profile"],
                    positions=positions,
                    selection=selection,
                    rules=rule_hash,
                    account_revision=review_account_revision,
                    histories={
                        c: hashlib.sha256(pair[0].to_json().encode()).hexdigest()
                        for c, pair in histories.items()
                    },
                    adjusted={
                        c: hashlib.sha256(frame.to_json().encode()).hexdigest()
                        for c, frame in adjusted_holdings.items()
                    },
                    benchmark=hashlib.sha256(benchmark.to_json().encode()).hexdigest(),
                    quotes=quotes,
                    coverage=context["coverage"],
                    market_context={k: v for k, v in market_context.items() if k != "as_of"},
                    financial_context={k: v for k, v in financial_context.items() if k != "as_of"},
                    data_errors=errors,
                    as_of=as_of,
                    evidence=[e["evidence_id"] for e in evidence],
                ),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        result["version_id"] = result["input_hash"][:32]
        result["web_job_id"] = (
            output.name if output.parent.resolve() == (root / "runtime/web-runs").resolve() else None
        )
        result["source_hash"] = rule_hash
        result["account_revision"] = review_account_revision
        (output / "planner-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (output / "receipts.json").write_text(
            json.dumps(client.receipts, ensure_ascii=False), encoding="utf-8"
        )
        book.publish(result, expected_account_revision=review_account_revision)
        return dict(mode="PLANNER", status=result["status"], version_id=result["version_id"], as_of=as_of)
    finally:
        client.close()
        book.close()
