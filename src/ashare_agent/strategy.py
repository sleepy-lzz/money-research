"""Deterministic daily strategy functions.

The strategy deliberately works against the small Snapshot interface in
``docs/CONTRACTS.md`` rather than importing the data or execution layers.
All calculations are point-in-time: market rows after ``as_of`` are ignored.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from .universe import supported_security

DEFAULT_STRATEGY_CONFIG: dict[str, Any] = {
    "min_listing_days": 250,
    "liquidity_min": 100_000_000.0,
    "candidate_count": 20,
    "max_positions": 10,
    "entry_rank": 10,
    "keep_rank": 20,
    "target_weight": 0.08,
    "max_gross": 0.80,
    "max_industry": 0.25,
    "momentum_windows": [60, 120],
    "trend_window": 120,
}

_OUTPUT_COLUMNS = [
    "ts_code",
    "rank",
    "score",
    "close",
    "ma120",
    "momentum60",
    "momentum120",
    "volatility20",
    "atr14",
    "adv20",
    "amount20",
    "industry",
    "eligible",
    "reason",
]


def _cfg(config: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(DEFAULT_STRATEGY_CONFIG)
    if config:
        result.update(config)
    return result


def _iso(value: Any) -> str:
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    value = str(value)
    # Dates in the contract are ISO dates. Keeping the first ten characters
    # also handles a timestamp column without making a future date available.
    parsed = date.fromisoformat(value[:10])
    return parsed.isoformat()


def _point_in_time_market(snapshot: Any, as_of: str, decision_cutoff: str | None = None) -> pd.DataFrame:
    market = getattr(snapshot, "market", None)
    if market is None:
        raise ValueError("snapshot.market is required")
    market = pd.DataFrame(market).copy()
    if "trade_date" not in market.columns or "ts_code" not in market.columns:
        raise ValueError("snapshot.market requires ts_code and trade_date")
    market["_date"] = market["trade_date"].map(_iso)
    # Do not use a later row to fill a missing or unknown as-of observation.
    market = market.loc[market["_date"] <= as_of].copy()
    if "available_at" in market.columns:
        # The contract exposes an optional availability timestamp. ``as_of``
        # is a daily decision boundary, so a row published on a later date is
        # future information even if its trade_date is old. Same-day
        # availability is accepted because strategy decisions are end of day.
        cutoff = pd.Timestamp(decision_cutoff or as_of + "T23:59:59+08:00")
        if cutoff.tzinfo is None:
            raise ValueError("decision cutoff requires timezone")
        available = pd.to_datetime(market["available_at"], utc=True, errors="coerce")
        market = market.loc[available.notna() & (available <= cutoff)].copy()
    market = market.sort_values(["ts_code", "_date"], kind="mergesort")
    return market


def _point_in_time_securities(snapshot: Any, as_of: str) -> pd.DataFrame:
    securities = getattr(snapshot, "securities", None)
    if securities is None:
        return pd.DataFrame()
    securities = pd.DataFrame(securities).copy()
    if "ts_code" not in securities.columns:
        return pd.DataFrame()
    if "list_date" in securities.columns:
        securities["_list_date"] = securities["list_date"].map(
            lambda x: _iso(x) if pd.notna(x) and str(x) else None
        )
    else:
        securities["_list_date"] = None
    if "delist_date" in securities.columns:
        securities["_delist_date"] = securities["delist_date"].map(
            lambda x: _iso(x) if pd.notna(x) and str(x) else None
        )
    else:
        securities["_delist_date"] = None
    return securities.drop_duplicates("ts_code", keep="last").set_index("ts_code", drop=False)


def _bool_known(row: pd.Series, column: str, default: bool = True) -> bool:
    if column not in row.index:
        return default
    value = row[column]
    if pd.isna(value):
        return False
    return bool(value)


def _is_suspended(row: pd.Series) -> bool:
    return bool(row.get("suspended", False)) if not pd.isna(row.get("suspended", False)) else False


def _finite(value: Any) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _listing_age(snapshot: Any, security: pd.Series | None, as_of: str) -> int | None:
    if security is None:
        return None
    list_date = security.get("_list_date")
    if not list_date:
        return None
    calendar = getattr(snapshot, "calendar", None)
    if calendar:
        dates = sorted({_iso(d) for d in calendar})
        return sum(list_date <= d <= as_of for d in dates)
    # Without the known trading calendar the conservative fallback is the
    # number of observed sessions, handled by the caller. Do not infer
    # weekends as trading sessions here.
    return None


def _latest_valid_adjusted(history: pd.DataFrame) -> pd.DataFrame:
    if history.empty:
        return history.copy()
    h = history.copy()
    close = _numeric(h.get("close", pd.Series(index=h.index, dtype=float)))
    factor = _numeric(h.get("adj_factor", pd.Series(index=h.index, dtype=float)))
    h["_close"] = close
    h["_factor"] = factor
    h["_adj"] = close * factor
    suspended = h.get("suspended", False)
    h["_suspended"] = suspended.fillna(False).astype(bool) if isinstance(suspended, pd.Series) else False
    return h.loc[~h["_suspended"] & np.isfinite(h["_adj"]) & (h["_adj"] > 0)].copy()


def _momentum(valid: pd.DataFrame, window: int) -> float:
    if len(valid) < window + 1:
        return float("nan")
    values = valid["_adj"].iloc[-(window + 1) :]
    return float(values.iloc[-1] / values.iloc[0] - 1.0)


def _atr14(history: pd.DataFrame) -> float:
    if history.empty:
        return float("nan")
    h = history.copy()
    for col in ("high", "low", "close"):
        h[f"_{col}"] = _numeric(h.get(col, pd.Series(index=h.index, dtype=float)))
    suspended = h.get("suspended", False)
    h["_suspended"] = suspended.fillna(False).astype(bool) if isinstance(suspended, pd.Series) else False
    h = h.loc[~h["_suspended"]].copy()
    h = h.loc[np.isfinite(h["_high"]) & np.isfinite(h["_low"]) & np.isfinite(h["_close"])]
    if len(h) < 14:
        return float("nan")
    previous = h["_close"].shift(1)
    true_range = pd.concat(
        [h["_high"] - h["_low"], (h["_high"] - previous).abs(), (h["_low"] - previous).abs()], axis=1
    ).max(axis=1)
    true_range = true_range.iloc[1:].dropna()
    if len(true_range) < 14:
        return float("nan")
    return float(true_range.iloc[-14:].mean())


def _liquidity(history: pd.DataFrame) -> tuple[float, float]:
    if history.empty:
        return float("nan"), float("nan")
    h = history.copy()
    suspended = h.get("suspended", False)
    suspended = (
        suspended.fillna(False).astype(bool)
        if isinstance(suspended, pd.Series)
        else pd.Series(False, index=h.index)
    )
    volume = _numeric(h.get("volume", pd.Series(index=h.index, dtype=float))).where(~suspended, 0).iloc[-20:]
    amount = _numeric(h.get("amount", pd.Series(index=h.index, dtype=float))).where(~suspended, 0).iloc[-20:]
    if len(volume) < 20 or len(amount) < 20 or not np.isfinite(volume).all() or not np.isfinite(amount).all():
        return float("nan"), float("nan")
    return float(volume.iloc[-20:].mean()), float(amount.iloc[-20:].mean())


def _volatility(valid: pd.DataFrame) -> float:
    if len(valid) < 21:
        return float("nan")
    returns = valid["_adj"].pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if len(returns) < 20:
        return float("nan")
    return float(returns.iloc[-20:].std(ddof=0))


def _industry_known(latest: pd.Series) -> bool:
    if "industry_known" not in latest.index:
        return True
    return _bool_known(latest, "industry_known", default=False)


def _security_is_supported(sec: pd.Series | None) -> tuple[bool, str]:
    return (True, "") if supported_security(sec) else (False, "unsupported_market")


def _active_on(sec: pd.Series | None, as_of: str) -> tuple[bool, str]:
    if sec is None:
        return True, ""
    list_date = sec.get("_list_date")
    delist_date = sec.get("_delist_date")
    if list_date and list_date > as_of:
        return False, "not_listed"
    # Contract: delist_date is exclusive.
    if delist_date and delist_date <= as_of:
        return False, "delisted"
    return True, ""


def _reasons(*parts: Iterable[str] | str) -> str:
    flat: list[str] = []
    for part in parts:
        if isinstance(part, str):
            values = [part] if part else []
        else:
            values = list(part)
        for value in values:
            if value and value not in flat:
                flat.append(value)
    return ";".join(flat)


def rank_candidates(
    snapshot: Any, as_of: str, config: dict[str, Any] | None = None, *, decision_cutoff: str | None = None
) -> pd.DataFrame:
    """Compute the frozen 60/120-day cross-sectional momentum baseline.

    The returned table contains the candidates that can be inspected for
    exits, including rows that fail a guard. Only eligible rows receive a
    positive integer rank; no missing factor is silently substituted.
    """

    as_of = _iso(as_of)
    cfg = _cfg(config)
    windows = list(cfg.get("momentum_windows", [60, 120]))
    if windows != [60, 120] or int(cfg.get("trend_window", 120)) != 120:
        # This worker implements the frozen baseline. Other windows should be
        # an explicit experiment rather than quietly changing its semantics.
        raise ValueError("baseline strategy requires momentum_windows=[60, 120] and trend_window=120")
    market = _point_in_time_market(snapshot, as_of, decision_cutoff)
    securities = _point_in_time_securities(snapshot, as_of)
    if market.empty:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    # Normalize and group once. The previous per-code boolean scan was
    # quadratic in the number of securities while producing the same sorted
    # per-symbol history for the calculations below.
    market["_code"] = market["ts_code"].astype(str)
    histories = {code: group for code, group in market.groupby("_code", sort=False)}
    codes = sorted(histories)
    rows: list[dict[str, Any]] = []
    for code in codes:
        history = histories[code].sort_values("_date", kind="mergesort")
        if history.empty:
            continue
        latest = history.iloc[-1]
        sec = securities.loc[code] if code in securities.index else None
        reasons: list[str] = []
        supported, reason = _security_is_supported(sec)
        if not supported:
            reasons.append(reason)
        active, reason = _active_on(sec, as_of)
        if not active:
            reasons.append(reason)

        age = _listing_age(snapshot, sec, as_of)
        if age is None:
            # With a complete security record but no calendar, observed
            # sessions are a conservative proxy only when enough history is
            # visibly present.
            age = len(history)
        if age < int(cfg.get("min_listing_days", 250)):
            reasons.append("insufficient_listing_age")

        state_known = _bool_known(latest, "state_known", default=False)
        if not state_known:
            reasons.append("status_unknown")
        else:
            is_st_value = latest.get("is_st", False)
            if pd.isna(is_st_value):
                reasons.append("status_unknown")
            elif bool(is_st_value):
                reasons.append("st")
        if _is_suspended(latest):
            reasons.append("suspended")
        if "tradeable" in latest and not bool(latest["tradeable"]):
            reasons.append("session_not_tradeable")
        if _iso(latest["_date"]) != as_of:
            reasons.append("data_not_current")
        if not _industry_known(latest):
            reasons.append("industry_unknown")

        valid = _latest_valid_adjusted(history)
        m60 = _momentum(valid, 60)
        m120 = _momentum(valid, 120)
        ma120 = float(valid["_adj"].iloc[-120:].mean()) if len(valid) >= 120 else float("nan")
        last_adj = float(valid["_adj"].iloc[-1]) if len(valid) else float("nan")
        if not _finite(m60) or not _finite(m120) or not _finite(ma120):
            reasons.append("insufficient_history")
        volatility = _volatility(valid)
        atr = _atr14(history)
        adv20, amount20 = _liquidity(history)
        if not _finite(adv20) or not _finite(amount20):
            reasons.append("insufficient_liquidity_history")
        elif amount20 < float(cfg.get("liquidity_min", 100_000_000)):
            reasons.append("liquidity_below_min")
        if _finite(last_adj) and _finite(ma120) and last_adj <= ma120:
            reasons.append("trend_below_ma120")
        # A current suspended row can still have a stale raw close. Prefer it
        # when valid; otherwise expose the latest actual close for inspection.
        raw_close = _numeric(pd.Series([latest.get("close", np.nan)])).iloc[0]
        if not _finite(raw_close):
            raw_close = float(valid["_close"].iloc[-1]) if len(valid) else float("nan")
        industry = latest.get("industry", sec.get("industry") if sec is not None else None)
        if pd.isna(industry):
            industry = ""
        rows.append(
            {
                "ts_code": code,
                "rank": pd.NA,
                "score": float("nan"),
                "close": float(raw_close) if _finite(raw_close) else float("nan"),
                "ma120": ma120,
                "momentum60": m60,
                "momentum120": m120,
                "volatility20": volatility,
                "atr14": atr,
                "adv20": adv20,
                "amount20": amount20,
                "industry": str(industry),
                "eligible": False,
                "reason": _reasons(reasons),
            }
        )

    result = pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)
    if result.empty:
        return result

    factor_valid = np.isfinite(result["momentum60"].astype(float)) & np.isfinite(
        result["momentum120"].astype(float)
    )
    # Hard-filtered securities cannot alter the cross-sectional percentile of
    # an otherwise eligible name. This matters when a rejected symbol has an
    # extreme momentum value (for example an ST stock or an unsupported board).
    score_universe = factor_valid & result["reason"].eq("")
    if score_universe.any():
        result.loc[score_universe, "score"] = (
            result.loc[score_universe, "momentum60"].rank(method="average", pct=True)
            + result.loc[score_universe, "momentum120"].rank(method="average", pct=True)
        ) / 2.0

    # Eligibility is a gate over every deterministic requirement. Scores may
    # remain visible for an ineligible row, but its rank is always null.
    result["eligible"] = result["reason"].eq("") & np.isfinite(result["score"].astype(float))
    eligible_idx = result.index[result["eligible"]]
    ordered = result.loc[eligible_idx].sort_values(
        ["score", "ts_code"], ascending=[False, True], kind="mergesort"
    )
    for rank, idx in enumerate(ordered.index, start=1):
        result.loc[idx, "rank"] = rank
    result["rank"] = result["rank"].astype("Int64")
    # Eligible first makes the table directly useful as a candidate report;
    # code is the deterministic final tie-break for failed rows.
    result["_rank_sort"] = result["rank"].fillna(10**9)
    result = result.sort_values(["_rank_sort", "ts_code"], kind="mergesort").drop(columns="_rank_sort")
    return result.reset_index(drop=True)[_OUTPUT_COLUMNS]


def market_regime(
    snapshot: Any, as_of: str, benchmark: str = "000300.SH", *, decision_cutoff: str | None = None
) -> dict[str, Any]:
    """Describe the broad market from a point-in-time MA200 and 20D slope."""

    as_of = _iso(as_of)
    data = getattr(snapshot, "benchmarks", None)
    base = {"regime": "unknown", "score": None, "signals": {"benchmark": benchmark}}
    if data is None:
        return base
    frame = pd.DataFrame(data).copy()
    required = {"trade_date", "ts_code", "close"}
    if not required.issubset(frame.columns):
        return base
    frame["_date"] = frame["trade_date"].map(_iso)
    frame = frame.loc[(frame["_date"] <= as_of) & (frame["ts_code"].astype(str) == benchmark)].sort_values(
        "_date", kind="mergesort"
    )
    if "available_at" in frame:
        available = pd.to_datetime(frame.available_at, utc=True, errors="coerce")
        frame = frame.loc[
            available.notna() & (available <= pd.Timestamp(decision_cutoff or as_of + "T23:59:59+08:00"))
        ]
    if frame.empty or frame["_date"].iloc[-1] != as_of or frame["_date"].duplicated().any():
        base["signals"]["reason"] = "benchmark missing decision session or has duplicate dates"
        return base
    expected = [_iso(day) for day in getattr(snapshot, "calendar", []) if _iso(day) <= as_of][-200:]
    if expected and set(expected) - set(frame["_date"]):
        base["signals"]["reason"] = "benchmark missing required calendar sessions"
        return base
    if not _numeric(frame.tail(200)["close"]).map(lambda value: np.isfinite(value) and value > 0).all():
        base["signals"]["reason"] = "benchmark has invalid window prices"
        return base
    values = _numeric(frame["close"]).replace([np.inf, -np.inf], np.nan).dropna()
    if len(values) < 200:
        base["signals"].update({"sessions": len(values), "required_sessions": 200})
        return base
    latest = float(values.iloc[-1])
    ma200 = float(values.iloc[-200:].mean())
    # A 20-session slope expressed as a return. It requires 21 points: the
    # endpoint plus the point 20 trading observations earlier.
    slope20 = float(values.iloc[-1] / values.iloc[-21] - 1.0)
    above = latest > ma200
    rising = slope20 > 0
    score = (1 if above else -1) + (1 if rising else -1)
    if above and rising:
        regime = "bull"
    elif (not above) and (not rising):
        regime = "bear"
    else:
        regime = "neutral"
    return {
        "regime": regime,
        "score": score,
        "signals": {
            "benchmark": benchmark,
            "close": latest,
            "ma200": ma200,
            "slope20": slope20,
            "above_ma200": above,
            "slope_positive": rising,
            "sessions": len(values),
        },
    }


def is_rebalance_day(calendar: list[str], as_of: str) -> bool:
    """Return true when the next known trading session starts another ISO week."""

    as_of = _iso(as_of)
    dates = sorted({_iso(d) for d in calendar})
    try:
        position = dates.index(as_of)
    except ValueError:
        return False
    if position + 1 >= len(dates):
        # Without a next session, the contract's look-ahead test cannot be
        # performed safely.
        return False
    current = date.fromisoformat(as_of).isocalendar()
    following = date.fromisoformat(dates[position + 1]).isocalendar()
    return (current.year, current.week) != (following.year, following.week)


def _row_for(ranking: pd.DataFrame, code: str) -> pd.Series | None:
    if ranking.empty or "ts_code" not in ranking.columns:
        return None
    rows = ranking.loc[ranking["ts_code"].astype(str) == str(code)]
    if rows.empty:
        return None
    return rows.iloc[0]


def _is_explicit_invalidation(row: pd.Series) -> bool:
    reason = str(row.get("reason", ""))
    if not reason:
        return False
    # Suspension and stale/missing data are operationally different from a
    # known invalidation. Existing suspended/missing holdings stay visible to
    # the root risk/execution layer and are not sold by this selector.
    if any(token in reason.split(";") for token in ("suspended", "data_not_current", "missing_data")):
        return False
    invalidating = {
        "st",
        "status_unknown",
        "not_listed",
        "delisted",
        "unsupported_exchange",
        "unsupported_board",
        "trend_below_ma120",
        "insufficient_history",
        "industry_unknown",
        "liquidity_below_min",
        "insufficient_liquidity_history",
    }
    return any(token in invalidating for token in reason.split(";"))


def select_targets(
    ranking: pd.DataFrame,
    held_symbols: list[str],
    rebalance: bool,
    config: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Select target weights; this function never produces order quantities."""

    cfg = _cfg(config)
    if ranking is None or ranking.empty:
        # Keep known holdings when the ranking feed is unavailable. This is a
        # conservative data-failure behavior; the root risk layer can report
        # or trim them with explicit evidence.
        return {str(symbol): float(cfg["target_weight"]) for symbol in held_symbols}
    weight = float(cfg.get("target_weight", 0.08))
    max_gross = float(cfg.get("max_gross", 0.80))
    max_positions = max(0, int(cfg.get("max_positions", 10)))
    keep_rank = int(cfg.get("keep_rank", 20))
    entry_rank = int(cfg.get("entry_rank", 10))
    max_industry = float(cfg.get("max_industry", 0.25))
    if weight <= 0 or max_gross <= 0 or max_positions == 0:
        return {}

    eligible_flag = (
        ranking["eligible"].astype(bool)
        if "eligible" in ranking.columns
        else pd.Series(False, index=ranking.index)
    )
    eligible = ranking.loc[eligible_flag].copy()
    eligible["_rank"] = pd.to_numeric(eligible.get("rank"), errors="coerce")
    eligible = eligible.loc[np.isfinite(eligible["_rank"])]
    eligible = eligible.sort_values(["_rank", "ts_code"], kind="mergesort")
    held_unique: list[str] = []
    for symbol in held_symbols:
        code = str(symbol)
        if code not in held_unique:
            held_unique.append(code)

    # Retention is evaluated in held order for deterministic, explainable
    # behavior. Missing rows and suspension remain held for review; explicit
    # invalidations and trend exits are omitted.
    retained: list[str] = []
    for code in held_unique:
        row = _row_for(ranking, code)
        if row is None:
            retained.append(code)
            continue
        rank = pd.to_numeric(pd.Series([row.get("rank")]), errors="coerce").iloc[0]
        reason = str(row.get("reason", ""))
        tokens = set(reason.split(";"))
        if (
            "suspended" in tokens
            or "data_not_current" in tokens
            or (np.isfinite(rank) and rank <= keep_rank and bool(row.get("eligible", False)))
            or (not _is_explicit_invalidation(row) and pd.isna(rank))
        ):
            retained.append(code)

    ordered: list[str] = retained[:max_positions]
    if rebalance:
        for _, row in eligible.iterrows():
            code = str(row["ts_code"])
            if code in ordered:
                continue
            if float(row["_rank"]) > entry_rank:
                continue
            if len(ordered) >= max_positions:
                break
            ordered.append(code)

    result: dict[str, float] = {}
    industry_totals: dict[str, float] = {}
    gross = 0.0
    for code in ordered:
        if gross + weight > max_gross + 1e-12:
            break
        row = _row_for(ranking, code)
        industry = "__unknown__" if row is None else str(row.get("industry", "")) or "__unknown__"
        if industry_totals.get(industry, 0.0) + weight > max_industry + 1e-12:
            continue
        result[code] = weight
        gross += weight
        industry_totals[industry] = industry_totals.get(industry, 0.0) + weight
    return result


__all__ = [
    "DEFAULT_STRATEGY_CONFIG",
    "is_rebalance_day",
    "market_regime",
    "rank_candidates",
    "select_targets",
]
