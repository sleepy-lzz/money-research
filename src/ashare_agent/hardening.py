"""Evidence-bound session inputs. Hashes prove identity, never source truthfulness."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pandas as pd

from .universe import supported_security


def frame_records(frame):
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def content_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    ).hexdigest()


def timestamp(value):
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("Evidence timestamps must be present and timezone-aware")
    return result


def ingest_receipt(payload: dict, published_at: str, source: str) -> dict:
    """Stamp actual local acquisition time; there is no backdating parameter.

    The payload must already contain a full source inventory, not a list selected
    by the strategy. Imported historic archives need independently audited receipts.
    """
    now = datetime.now(timezone.utc).isoformat()
    published = timestamp(published_at)
    if not source or published > timestamp(now):
        raise ValueError("Missing source or future publication")
    return dict(
        payload=payload,
        content_hash=content_hash(payload),
        published_at=published_at,
        fetched_at=now,
        available_at=now,
        source=source,
    )


def active_inventory(snapshot, day):
    result = []
    for row in frame_records(snapshot.securities.sort_values("ts_code")):
        if (
            supported_security(row)
            and row["list_date"] <= day
            and (not row.get("delist_date") or day < row["delist_date"])
        ):
            # Future delisting dates and current names are not decision inputs.
            result.append({k: row[k] for k in ("ts_code", "list_date", "exchange", "board")})
    return result


def session_payload(snapshot, day):
    """Every consumed field is bound, including benchmark and known corporate actions."""
    future = [d for d in snapshot.calendar if d > day]
    return dict(
        trade_date=day,
        next_session=future[0] if future else None,
        inventory=active_inventory(snapshot, day),
        market=frame_records(snapshot.market.loc[snapshot.market.trade_date == day].sort_values("ts_code")),
        benchmark=frame_records(
            snapshot.benchmarks.loc[snapshot.benchmarks.trade_date == day].sort_values("ts_code")
        ),
        actions=frame_records(
            snapshot.actions.loc[snapshot.actions.known_at <= day].sort_values("action_id")
        ),
    )


def check_receipt(receipt, expected, cutoff):
    if (
        not receipt.get("source")
        or receipt.get("content_hash") != content_hash(expected)
        or receipt.get("payload") != expected
    ):
        raise ValueError("Receipt payload/source/hash mismatch")
    published, fetched, available = (
        timestamp(receipt.get(k)) for k in ("published_at", "fetched_at", "available_at")
    )
    if published > available or fetched > available or available > timestamp(cutoff):
        raise ValueError("Receipt was not available at the decision cutoff")


def audit_real_snapshot(snapshot):
    """Current downloads alone are reconstruction data, not historical PIT proof."""
    if snapshot.metadata.get("synthetic"):
        return []
    issues = []
    raw = snapshot.metadata.get("raw_download", {})
    if raw.get("status") != "complete" or not raw.get("request_manifest"):
        issues.append("request-level raw coverage is not proven complete")
    requests = raw.get("request_manifest", [])
    completed = [
        r
        for r in requests
        if r.get("status") == "complete" and r.get("sha256") and r.get("revision") and r.get("fetched_at")
    ]
    required = {
        "stock_basic",
        "daily",
        "adj_factor",
        "stk_limit",
        "suspend_d",
        "dividend",
        "trade_cal",
        "index_daily",
    }
    if required - {r.get("endpoint") for r in completed}:
        issues.append("endpoint coverage missing from request manifest")
    if {"L", "D", "P"} - {
        r.get("params", {}).get("list_status") for r in completed if r.get("endpoint") == "stock_basic"
    }:
        issues.append("stock_basic L/D/P status coverage is incomplete")
    for endpoint in ("daily", "adj_factor", "stk_limit", "suspend_d", "dividend"):
        wanted = {str(d).replace("-", "") for d in snapshot.market.trade_date.unique()}
        actual = {r.get("logical_key") for r in completed if r.get("endpoint") == endpoint}
        if wanted - actual:
            issues.append(f"{endpoint}: session partitions missing from request manifest")
    receipts = snapshot.metadata.get("session_receipts", {})
    inventories = snapshot.metadata.get("inventory_receipts", {})
    for day in sorted(snapshot.market.trade_date.unique()):
        cutoff = day + "T23:59:59+08:00"
        try:
            # A separate complete-inventory source receipt prevents equating a
            # successfully downloaded present-day L/D/P table with historical coverage.
            expected_inventory = dict(
                trade_date=day, complete_inventory=active_inventory(snapshot, day), scope="SSE_SZSE_MAIN"
            )
            check_receipt(inventories.get(day, {}), expected_inventory, cutoff)
            check_receipt(receipts.get(day, {}), session_payload(snapshot, day), cutoff)
        except (ValueError, TypeError, KeyError):
            issues.append(
                f"{day}: independently archived inventory/session receipt missing, late or mismatched"
            )
            break
    return issues


def make_spine(daily, basic, calendar, start, end, full_day_evidence):
    """Create status rows before any joins; absence of quotes never implies a halt."""
    days = [d for d in calendar if start <= d <= end]
    rows = []
    for sec in frame_records(basic):
        listed = str(sec.get("list_date", "")).replace("-", "")
        delisted = sec.get("delist_date")
        for day in days:
            if (
                listed
                and listed <= day.replace("-", "")
                and (not delisted or day.replace("-", "") < str(delisted).replace("-", ""))
            ):
                rows.append(dict(ts_code=sec["ts_code"], trade_date=day))
    spine = pd.DataFrame(rows, columns=["ts_code", "trade_date"])
    daily = spine.merge(
        daily, on=["ts_code", "trade_date"], how="outer", validate="one_to_one", indicator=True
    )
    if daily["_merge"].eq("right_only").any():
        raise ValueError("daily quote outside the source inventory/listing interval")
    daily["quote_present"] = daily.pop("_merge").eq("both")
    evidence = full_day_evidence
    confirmed = set()
    if not evidence.empty:
        required = {"ts_code", "trade_date", "full_day"}
        if not required.issubset(evidence):
            raise ValueError("suspensions evidence missing required fields")
        if (
            evidence.duplicated(["ts_code", "trade_date"]).any()
            or not evidence.full_day.map(lambda v: isinstance(v, bool)).all()
        ):
            raise ValueError("Ambiguous full-day suspension evidence")
        confirmed = {(r.ts_code, str(r.trade_date)) for r in evidence.itertuples() if r.full_day}
    daily["full_day_confirmed"] = [
        (str(r.ts_code), str(r.trade_date)) in confirmed for r in daily.itertuples()
    ]
    if (daily.quote_present & daily.full_day_confirmed).any():
        raise ValueError("Full-day suspension conflicts with an actual daily quote")
    return daily


def add_valuations(market, actions):
    """Stale valuation is separate from OHLC and can never be sent to the matcher."""
    market = market.sort_values(["ts_code", "trade_date"]).copy()
    ex_days = {(r.ts_code, r.ex_date) for r in actions.itertuples()}
    prices, dates, kinds = [], [], []
    for code, history in market.groupby("ts_code", sort=False):
        previous = None
        last_date = None
        for row in history.itertuples():
            if pd.notna(row.close):
                previous, last_date, kind = float(row.close), row.trade_date, "actual_close"
            else:
                if (code, row.trade_date) in ex_days:
                    previous = None  # no unadjusted carry across ex-date
                kind = "stale_last_close" if previous is not None else "unknown"
            prices.append(previous)
            dates.append(last_date)
            kinds.append(kind)
    market["valuation_price"], market["valuation_date"], market["valuation_kind"] = prices, dates, kinds
    return market
