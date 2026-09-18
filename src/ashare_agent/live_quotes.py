"""Two-source intraday quotes for the small, explicitly supplied stock set.

This module deliberately keeps source receipts next to the parsed quote.  A
quote is useful to a monitor only when both sources supplied a same-day value
and the response itself was acquired recently.  It does not infer trading
status or execution availability.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .current_data import (
    SHANGHAI,
    CurrentClient,
    mainboard_code,
    possible_limit_state,
    public_code,
    risk_name,
)

SINA_QUOTE_URL = "https://hq.sinajs.cn/list="
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
MAX_CODES = 50
MAX_AGE_SECONDS = Decimal("90")
PRICE_TICK = Decimal("0.01")
PRICE_TOLERANCE = Decimal("0.002")

_SINA_ROW = re.compile(r'var\s+hq_str_([a-z]{2}\d{6})="([^"\r\n]*)";?')
_TENCENT_ROW = re.compile(r'v_([a-z]{2}\d{6})="([^"\r\n]*)";?')


def _as_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI)
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def _text(raw: bytes | str, encoding: str = "utf-8") -> str:
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        return raw.decode(encoding, errors="replace")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _number_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip().replace("/", "-")
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.strptime(text, "%Y%m%d%H%M%S") if re.fullmatch(r"\d{14}", text) else datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _timestamp_from_sina(parts: list[str]) -> datetime | None:
    if len(parts) <= 31:
        return None
    date = parts[30].strip()
    time = parts[31].strip()
    return _parse_timestamp(f"{date}T{time}") if date and time else None


def _source_receipt(receipts: list[dict[str, Any]], before: int) -> dict[str, Any] | None:
    """Return the receipt appended by the most recent source request."""

    if len(receipts) > before:
        return receipts[-1]
    # Lightweight offline clients used by callers may expose the request
    # receipt without appending it from ``get``.  Reusing that receipt still
    # preserves the freshness gate; an old receipt will be marked stale.
    return receipts[-1] if receipts else None


def _source_available_at(receipt: dict[str, Any] | None) -> datetime | None:
    if not isinstance(receipt, dict):
        return None
    value = receipt.get("available_at")
    if value is None:
        return None
    return _parse_timestamp(value)


def _empty_source(source: str, code: str, reason: str = "missing_quote") -> dict[str, Any]:
    return {
        "source": source,
        "code": code,
        "name": "",
        "price": None,
        "previous_close": None,
        "open": None,
        "bid": None,
        "ask": None,
        "bid_size": None,
        "ask_size": None,
        "volume": None,
        "observed_at": None,
        "available_at": None,
        "valid": False,
        "reasons": [reason],
    }


def _parse_sina(raw: bytes | str, codes: Iterable[str], receipt: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Parse ``hq.sinajs.cn`` rows keyed by internal ``sh600000`` symbols."""

    wanted = set(codes)
    result: dict[str, dict[str, Any]] = {}
    available = _source_available_at(receipt)
    for symbol, body in _SINA_ROW.findall(_text(raw, "gb18030")):
        if symbol not in wanted or symbol in result:
            continue
        parts = body.split(",")
        observed = _timestamp_from_sina(parts)
        row = _empty_source("sina", symbol)
        row.update(
            name=parts[0].strip() if parts else "",
            price=_decimal(parts[3]) if len(parts) > 3 else None,
            previous_close=_decimal(parts[2]) if len(parts) > 2 else None,
            open=_decimal(parts[1]) if len(parts) > 1 else None,
            bid=_decimal(parts[6]) if len(parts) > 6 else None,
            ask=_decimal(parts[7]) if len(parts) > 7 else None,
            bid_size=_decimal(parts[10]) if len(parts) > 10 else None,
            ask_size=_decimal(parts[20]) if len(parts) > 20 else None,
            volume=_decimal(parts[8]) if len(parts) > 8 else None,
            observed_at=observed,
            available_at=available,
        )
        result[symbol] = row
    return result


def _tencent_volume(parts: list[str]) -> Decimal | None:
    # Current Tencent quote rows expose cumulative volume at 36.  Some
    # verified short responses expose the same cumulative value at 6.  Index 8
    # is bid volume, so it must never be used as traded volume.
    if len(parts) > 36:
        return _decimal(parts[36])
    return _decimal(parts[6]) if len(parts) > 6 else None


def _parse_tencent(raw: bytes | str, codes: Iterable[str], receipt: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Parse ``qt.gtimg.cn/q=`` rows keyed by internal ``sh600000`` symbols."""

    wanted = set(codes)
    result: dict[str, dict[str, Any]] = {}
    available = _source_available_at(receipt)
    for symbol, body in _TENCENT_ROW.findall(_text(raw, "gb18030")):
        if symbol not in wanted or symbol in result:
            continue
        parts = body.split("~")
        observed = _parse_timestamp(parts[30]) if len(parts) > 30 else None
        row = _empty_source("tencent", symbol)
        row.update(
            name=parts[1].strip() if len(parts) > 1 else "",
            price=_decimal(parts[3]) if len(parts) > 3 else None,
            previous_close=_decimal(parts[4]) if len(parts) > 4 else None,
            open=_decimal(parts[5]) if len(parts) > 5 else None,
            bid=_decimal(parts[9]) if len(parts) > 9 else None,
            ask=_decimal(parts[19]) if len(parts) > 19 else None,
            bid_size=_decimal(parts[10]) if len(parts) > 10 else None,
            ask_size=_decimal(parts[20]) if len(parts) > 20 else None,
            volume=_tencent_volume(parts),
            observed_at=observed,
            available_at=available,
        )
        result[symbol] = row
    return result


def _check_source(row: dict[str, Any], now: datetime) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    required = ("name", "price", "previous_close", "open", "bid", "ask")
    for field in required:
        if row.get(field) is None:
            reasons.append(f"{row['source']}_{field}_invalid")
    if row.get("observed_at") is None:
        reasons.append(f"{row['source']}_observed_at_missing")
    if row.get("available_at") is None:
        reasons.append(f"{row['source']}_available_at_missing")
    if row.get("volume") is None:
        reasons.append(f"{row['source']}_volume_missing")

    price = row.get("price")
    previous_close = row.get("previous_close")
    opening = row.get("open")
    bid = row.get("bid")
    ask = row.get("ask")
    if price is not None and price <= 0:
        reasons.append(f"{row['source']}_price_nonpositive")
    if previous_close is not None and previous_close < 0:
        reasons.append(f"{row['source']}_previous_close_negative")
    if opening is not None and opening < 0:
        reasons.append(f"{row['source']}_open_negative")
    if bid is not None and bid < 0:
        reasons.append(f"{row['source']}_bid_negative")
    if ask is not None and ask < 0:
        reasons.append(f"{row['source']}_ask_negative")
    if bid is not None and ask is not None and bid > ask:
        reasons.append(f"{row['source']}_crossed_book")
    if row.get("volume") is not None and row["volume"] < 0:
        reasons.append(f"{row['source']}_volume_negative")

    for field in ("observed_at", "available_at"):
        timestamp = row.get(field)
        if timestamp is None:
            continue
        age = (now - timestamp).total_seconds()
        if timestamp.date() != now.date():
            reasons.append(f"{row['source']}_{field}_not_today")
        elif age < 0:
            reasons.append(f"{row['source']}_{field}_after_now")
        elif age > float(MAX_AGE_SECONDS):
            reasons.append(f"{row['source']}_{field}_stale")

    # A quote from the pre-open auction or the previous continuous session is
    # not a current continuous-session quote.  Outside continuous trading,
    # return a stale observation so the market-session owner can decide how to
    # display it without turning it into a live signal.
    observed = row.get("observed_at")
    current_time = now.timetz().replace(tzinfo=None)
    morning_start, morning_end = datetime.strptime("09:30", "%H:%M").time(), datetime.strptime("11:30", "%H:%M").time()
    afternoon_start, afternoon_end = datetime.strptime("13:00", "%H:%M").time(), datetime.strptime("15:00", "%H:%M").time()
    if not (morning_start <= current_time < morning_end or afternoon_start <= current_time <= afternoon_end):
        if observed is not None:
            reasons.append(f"{row['source']}_outside_continuous_session")
    elif observed is not None:
        segment_start = morning_start if current_time < morning_end else afternoon_start
        if observed.timetz().replace(tzinfo=None) < segment_start:
            reasons.append(f"{row['source']}_not_current_continuous_session")

    # Volume is intentionally a liquidity signal only.  Its absence or zero
    # value makes trading advice unproven but does not discard price evidence.
    return not reasons or all(
        reason.endswith("_volume_missing") for reason in reasons
    ), reasons


def _within_price_tolerance(left: Decimal, right: Decimal) -> bool:
    difference = abs(left - right)
    if difference <= PRICE_TICK:
        return True
    reference = max(abs(left), abs(right))
    return bool(reference and difference / reference <= PRICE_TOLERANCE)


def _serialize_source(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field in ("price", "previous_close", "open", "bid", "ask", "bid_size", "ask_size", "volume"):
        result[field] = _number_text(result.get(field))
    for field in ("observed_at", "available_at"):
        value = result.get(field)
        result[field] = value.isoformat() if isinstance(value, datetime) else value
    return result


def _merge_quote(symbol: str, sina: dict[str, Any], tencent: dict[str, Any], now: datetime) -> dict[str, Any]:
    sina_valid, sina_reasons = _check_source(sina, now)
    tencent_valid, tencent_reasons = _check_source(tencent, now)
    sina["valid"], sina["reasons"] = sina_valid, sina_reasons
    tencent["valid"], tencent["reasons"] = tencent_valid, tencent_reasons

    reasons = list(sina_reasons) + list(tencent_reasons)
    both_prices = sina.get("price") is not None and tencent.get("price") is not None
    conflict = both_prices and not _within_price_tolerance(sina["price"], tencent["price"])
    both_previous = sina.get("previous_close") is not None and tencent.get("previous_close") is not None
    previous_conflict = both_previous and abs(sina["previous_close"] - tencent["previous_close"]) > PRICE_TICK
    names = [str(row.get("name", "")).strip() for row in (sina, tencent)]
    name_conflict = bool(names[0] and names[1] and names[0] != names[1])
    risk_flags = [risk_name(name) for name in names if name]
    risk_conflict = len(risk_flags) == 2 and risk_flags[0] != risk_flags[1]
    if conflict:
        reasons.append("price_conflict")
    if previous_conflict:
        reasons.append("previous_close_conflict")
    if name_conflict:
        reasons.append("name_conflict")
    if risk_conflict:
        reasons.append("risk_name_conflict")
    if any(risk_flags):
        reasons.append("risk_name_detected")
    conflict = conflict or previous_conflict or name_conflict or risk_conflict or any(risk_flags)

    if conflict and sina_valid and tencent_valid:
        status = "conflict"
    elif not sina_valid or not tencent_valid:
        # A source with only missing volume remains a price-valid source, but
        # no source with a stale/malformed timestamp can make this fresh.
        hard_sina = [r for r in sina_reasons if not r.endswith("_volume_missing")]
        hard_tencent = [r for r in tencent_reasons if not r.endswith("_volume_missing")]
        status = "stale" if any(
            "stale" in r
            or "not_today" in r
            or "after_now" in r
            or "continuous_session" in r
            or "outside_continuous" in r
            for r in reasons
        ) else "missing"
        if hard_sina or hard_tencent:
            status = "stale" if any(
                "stale" in r
                or "not_today" in r
                or "after_now" in r
                or "continuous_session" in r
                or "outside_continuous" in r
                for r in reasons
            ) else "missing"
    else:
        status = "conflict" if conflict else "ok"

    sources = {"sina": _serialize_source(sina), "tencent": _serialize_source(tencent)}
    first = sina if sina.get("price") is not None else tencent
    risk_source = next((row for row in (sina, tencent) if row.get("name") and risk_name(row["name"])), None)
    if risk_source is not None:
        first = risk_source
    limit_states = {
        possible_limit_state(row.get("price"), row.get("previous_close"), row.get("name", ""))
        for row in (sina, tencent)
    }
    # A limit state is only a conservative warning.  If the sources disagree,
    # preserve uncertainty and let the execution guard fail closed.
    if len(limit_states) == 1:
        limit_state = limit_states.pop()
    elif limit_states & {"possible_lower_limit", "possible_upper_limit"}:
        limit_state = "unknown"
        reasons.append("limit_state_conflict")
    else:
        limit_state = "unknown"
    observed_values = [row["observed_at"] for row in (sina, tencent) if isinstance(row.get("observed_at"), datetime)]
    available_values = [row["available_at"] for row in (sina, tencent) if isinstance(row.get("available_at"), datetime)]
    positive_depth = all(
        row.get("bid") is not None
        and row.get("ask") is not None
        and row["bid"] > 0
        and row["ask"] > 0
        for row in (sina, tencent)
    )
    aligned_depth = (
        positive_depth
        and all(row["bid"] < row["ask"] for row in (sina, tencent))
        and _within_price_tolerance(sina["bid"], tencent["bid"])
        and _within_price_tolerance(sina["ask"], tencent["ask"])
    )
    depth_sizes_observed = all(
        row.get("bid_size") is not None
        and row.get("ask_size") is not None
        and row["bid_size"] > 0
        and row["ask_size"] > 0
        for row in (sina, tencent)
    )
    volume_complete = all(
        row.get("volume") is not None and row["volume"] > 0 for row in (sina, tencent)
    )
    weak_liquidity = not volume_complete
    weak_liquidity = weak_liquidity or not aligned_depth or not depth_sizes_observed
    liquidity_observed = (
        status == "ok"
        and not weak_liquidity
        and sina_valid
        and tencent_valid
        and positive_depth
        and aligned_depth
    )
    if weak_liquidity:
        reasons.append("liquidity_insufficient")
    if not depth_sizes_observed:
        reasons.append("depth_size_insufficient")

    # Use a conservative displayed ask for a buy-side monitor: both sources
    # are retained below, while the aggregate ask is the larger source ask.
    asks = [row["ask"] for row in (sina, tencent) if row.get("ask") is not None]
    bids = [row["bid"] for row in (sina, tencent) if row.get("bid") is not None]

    return {
        "code": public_code(symbol),
        "name": first.get("name", ""),
        "price": _number_text(first.get("price")),
        "previous_close": _number_text(first.get("previous_close")),
        "open": _number_text(first.get("open")),
        "ask": _number_text(max(asks) if asks else None),
        "bid": _number_text(min(bids) if bids else first.get("bid")),
        "volume": _number_text(first.get("volume")),
        "observed_at": max(observed_values).isoformat() if observed_values else None,
        "available_at": max(available_values).isoformat() if available_values else None,
        "status": status,
        "reasons": reasons,
        "sources": sources,
        "source_observed_at": {
            source: sources[source].get("observed_at") for source in ("sina", "tencent")
        },
        "liquidity_status": "insufficient" if weak_liquidity else "observed",
        "liquidity_observed": liquidity_observed,
        "tradeability_status": "not_proven",
        "limit_state": limit_state,
    }


def collect_quotes(root: Path | str, codes: Iterable[str], now: datetime | None = None) -> dict[str, Any]:
    """Collect fresh Sina and Tencent quotes for at most 50 public symbols.

    ``root`` is the project root; requests use its existing runtime cache
    location, while ``refresh=True`` ensures an old response is never used as
    an intraday quote.  Returned numeric fields are strings so JSON consumers
    never receive a NaN or an implementation-specific float rounding.
    """

    if isinstance(codes, (str, bytes)):
        raise TypeError("codes must be an iterable of public stock codes")
    requested = list(codes)
    if len(requested) > MAX_CODES:
        raise ValueError("盘中报价一次最多 50 只主板股票")
    symbols: list[str] = []
    seen: set[str] = set()
    for value in requested:
        symbol = mainboard_code(value)
        if symbol not in seen:
            symbols.append(symbol)
            seen.add(symbol)
    if not symbols:
        return {"quotes": {}, "receipts": []}

    # An explicit cutoff is useful for deterministic fixtures.  In production
    # sample the cutoff after both HTTP calls have completed; sampling before
    # requests would make a newly acquired receipt appear to be from the
    # future and incorrectly reject every live quote.
    cutoff = _as_now(now) if now is not None else None
    client = CurrentClient(Path(root) / "runtime" / "current-cache")
    receipts = getattr(client, "receipts", None)
    if not isinstance(receipts, list):
        receipts = []
        client.receipts = receipts
    try:
        sina_receipt_start = len(receipts)
        try:
            sina_raw = client.get(SINA_QUOTE_URL + ",".join(symbols), refresh=True)
            sina_receipt = _source_receipt(receipts, sina_receipt_start)
            sina_rows = _parse_sina(sina_raw, symbols, sina_receipt)
        except Exception as exc:  # preserve per-source evidence for the monitor
            sina_rows = {}
            sina_receipt = None
            sina_error = f"sina_request_failed:{type(exc).__name__}"
        else:
            sina_error = None

        tencent_receipt_start = len(receipts)
        try:
            tencent_raw = client.get(TENCENT_QUOTE_URL + ",".join(symbols), refresh=True)
            tencent_receipt = _source_receipt(receipts, tencent_receipt_start)
            tencent_rows = _parse_tencent(tencent_raw, symbols, tencent_receipt)
        except Exception as exc:  # preserve per-source evidence for the monitor
            tencent_rows = {}
            tencent_receipt = None
            tencent_error = f"tencent_request_failed:{type(exc).__name__}"
        else:
            tencent_error = None

        cutoff = cutoff or _as_now(None)
        quotes: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            sina = sina_rows.get(symbol, _empty_source("sina", symbol))
            tencent = tencent_rows.get(symbol, _empty_source("tencent", symbol))
            if sina_error:
                sina["reasons"] = [sina_error]
            if tencent_error:
                tencent["reasons"] = [tencent_error]
            quote = _merge_quote(symbol, sina, tencent, cutoff)
            if sina_error:
                quote["reasons"].append(sina_error)
            if tencent_error:
                quote["reasons"].append(tencent_error)
            quotes[quote["code"]] = quote
        return {"quotes": quotes, "receipts": list(receipts)}
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


__all__ = ["collect_quotes"]
