"""Deterministic performance analytics for the backtest and paper reports.

The functions in this module deliberately use a small, explicit contract.  In
particular, the first equity row is measured against ``initial_cash`` (the
portfolio value immediately before the first reported session), while a
benchmark is allowed to use a prior session as its anchor when that row is
available.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from math import isfinite, sqrt
from typing import Any

import numpy as np
import pandas as pd

_ANNUAL_SESSIONS = 252
_EPS = 1e-12


def _date_text(value: Any) -> str:
    """Return an ISO date string while accepting pandas/Python date values."""

    if isinstance(value, str):
        # Keep the public contract to calendar dates even if a caller supplied
        # an ISO timestamp.
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    if isinstance(value, (date, datetime, pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    return str(value)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _number_or_none(value: Any) -> float | None:
    number = _finite_float(value)
    return number


def _safe_return(numerator: float, denominator: float) -> float | None:
    if not isfinite(numerator) or not isfinite(denominator) or denominator <= _EPS:
        return None
    return numerator / denominator - 1.0


def _annualized_return(start_value: float | None, end_value: float | None, sessions: int) -> float | None:
    if start_value is None or end_value is None or start_value <= 0 or end_value <= 0 or sessions <= 0:
        return None
    try:
        return float((end_value / start_value) ** (_ANNUAL_SESSIONS / sessions) - 1.0)
    except (OverflowError, ValueError, ZeroDivisionError):
        return None


def _normalise_equity(equity: pd.DataFrame) -> pd.DataFrame:
    if equity is None:
        return pd.DataFrame(columns=["trade_date", "equity", "_date", "_value"])
    frame = equity.copy()
    if "trade_date" not in frame.columns or "equity" not in frame.columns:
        if frame.empty:
            return pd.DataFrame(columns=["trade_date", "equity", "_date", "_value"])
        raise ValueError("equity requires trade_date and equity columns")
    frame["_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.loc[frame["_date"].notna()].copy()
    frame["_value"] = pd.to_numeric(frame["equity"], errors="coerce")
    frame = frame.loc[frame["_value"].notna()].copy()
    # Stable sorting keeps the caller's order for duplicate dates; duplicate
    # dates are not useful for a daily curve, so retain the last observation.
    frame = frame.sort_values("_date", kind="mergesort").drop_duplicates("_date", keep="last")
    return frame.reset_index(drop=True)


def _normalise_fills(fills: pd.DataFrame | None) -> pd.DataFrame:
    if fills is None:
        return pd.DataFrame()
    frame = fills.copy()
    for column in ("quantity", "price", "fee", "realized_pnl"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def _benchmark_series(
    benchmark: pd.DataFrame | None,
    equity_dates: pd.Series,
) -> tuple[pd.DataFrame, str | None, float | None, bool]:
    """Prepare benchmark observations and return its previous-session anchor.

    The boolean indicates that the anchor is strictly before the first equity
    date.  An equal-date observation is still useful as a starting value, but
    it cannot produce a first-session benchmark return.
    """

    columns = ["_date", "_close"]
    if benchmark is None or len(benchmark) == 0 or len(equity_dates) == 0:
        return pd.DataFrame(columns=columns), None, None, False
    frame = benchmark.copy()
    if "trade_date" not in frame.columns or "close" not in frame.columns:
        return pd.DataFrame(columns=columns), None, None, False
    frame["_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["_close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.loc[frame["_date"].notna() & frame["_close"].notna()].copy()
    frame = frame.loc[np.isfinite(frame["_close"].to_numpy()) & (frame["_close"] > 0)]
    if frame.empty:
        return pd.DataFrame(columns=columns), None, None, False
    # If a snapshot contains several indices, use the first deterministic
    # symbol.  The contract exposes one benchmark argument and therefore one
    # return stream.
    if "ts_code" in frame.columns:
        codes = sorted(str(code) for code in frame["ts_code"].dropna().unique())
        if codes:
            frame = frame.loc[frame["ts_code"].astype(str) == codes[0]]
    frame = frame.sort_values("_date", kind="mergesort").drop_duplicates("_date", keep="last")
    frame = frame[["_date", "_close"]].reset_index(drop=True)
    first_date = equity_dates.iloc[0]
    prior = frame.loc[frame["_date"] < first_date]
    if not prior.empty:
        anchor = prior.iloc[-1]
        return frame, _date_text(anchor["_date"]), float(anchor["_close"]), True
    same_or_before = frame.loc[frame["_date"] <= first_date]
    if not same_or_before.empty:
        anchor = same_or_before.iloc[-1]
        return frame, _date_text(anchor["_date"]), float(anchor["_close"]), False
    return frame, None, None, False


def _benchmark_returns(
    benchmark: pd.DataFrame | None,
    equity_dates: pd.Series,
    equity_values: list[float],
) -> dict[str, Any]:
    frame, anchor_date, anchor_close, strict_prior = _benchmark_series(benchmark, equity_dates)
    empty: dict[str, Any] = {
        "benchmark_anchor_date": anchor_date,
        "benchmark_anchor_close": anchor_close,
        "benchmark_anchor_is_prior_session": strict_prior,
        "benchmark_return": None,
        "benchmark_cagr": None,
        "excess_return": None,
        "benchmark_daily_returns": [],
    }
    if frame.empty or anchor_close is None or len(equity_dates) == 0:
        return empty

    # As-of values are used so a calendar with a missing benchmark session does
    # not accidentally look like a portfolio loss.  The value must exist on or
    # before the equity date, never after it.
    bench_dates = frame["_date"].to_numpy(dtype="datetime64[ns]")
    bench_values = frame["_close"].to_numpy(dtype=float)
    values: list[float | None] = []
    for stamp in equity_dates:
        index = int(np.searchsorted(bench_dates, stamp.to_datetime64(), side="right") - 1)
        values.append(float(bench_values[index]) if index >= 0 else None)

    end_value = values[-1]
    total = _safe_return(end_value, anchor_close) if end_value is not None else None
    daily: list[float | None] = []
    previous = anchor_close
    for value in values:
        if value is None or previous is None:
            daily.append(None)
        else:
            daily.append(_safe_return(value, previous))
        if value is not None:
            previous = value
    # If the anchor equals the first equity date, the first daily return has no
    # prior session and is deliberately null.
    if not strict_prior and daily:
        daily[0] = None
    empty.update(
        {
            "benchmark_return": total,
            "benchmark_cagr": _annualized_return(anchor_close, end_value, len(equity_values)),
            "excess_return": (total - 0.0) if total is not None else None,
            "benchmark_daily_returns": daily,
        }
    )
    return empty


def _annual_return_details(frame: pd.DataFrame, initial_cash: float) -> dict[str, dict[str, Any]]:
    if frame.empty:
        return {}
    result: dict[str, dict[str, Any]] = {}
    previous = initial_cash if isfinite(initial_cash) else None
    years = sorted(int(year) for year in frame["_date"].dt.year.unique())
    for year in years:
        rows = frame.loc[frame["_date"].dt.year == year]
        first_date = rows.iloc[0]["_date"]
        last_date = rows.iloc[-1]["_date"]
        end = _finite_float(rows.iloc[-1]["_value"])
        # A year is complete only when its own observed boundaries cover the
        # natural-year boundaries.  This deliberately labels missing January
        # or December observations as partial rather than claiming a full-year
        # result from a sample-period return.
        partial = (first_date.month, first_date.day) != (1, 1) or (last_date.month, last_date.day) != (12, 31)
        result[str(year)] = {
            "return": _safe_return(end, previous) if end is not None and previous is not None else None,
            "sample_start": _date_text(first_date),
            "sample_end": _date_text(last_date),
            "sessions": int(len(rows)),
            "coverage": "partial_year" if partial else "full_year_coverage_unverified",
            "is_partial": partial,
            "full_year_verified": False,
        }
        if end is not None:
            previous = end
    return result


def _annual_returns(frame: pd.DataFrame, initial_cash: float) -> dict[str, float | None]:
    """Compatibility mapping; use period_coverage for year-boundary semantics."""

    return {year: details["return"] for year, details in _annual_return_details(frame, initial_cash).items()}


def _period_coverage(
    frame: pd.DataFrame,
    initial_cash: float | None,
) -> dict[str, Any]:
    if frame.empty:
        return {
            "sample_start": None,
            "sample_end": None,
            "sample_sessions": 0,
            "sample_coverage": "empty",
            "years": {},
        }
    details = _annual_return_details(frame, initial_cash if initial_cash is not None else float("nan"))
    partial = any(item["is_partial"] for item in details.values())
    return {
        "sample_start": _date_text(frame.iloc[0]["_date"]),
        "sample_end": _date_text(frame.iloc[-1]["_date"]),
        "sample_sessions": int(len(frame)),
        "sample_coverage": "partial_natural_years" if partial else "natural_year_coverage_unverified",
        "years": details,
    }


def _drawdown_stats(values: list[float], initial_cash: float) -> dict[str, Any]:
    if not values:
        return {
            "max_drawdown": None,
            "max_drawdown_abs": None,
            "max_drawdown_duration": None,
            "max_drawdown_duration_sessions": None,
            "drawdown_start": None,
            "drawdown_end": None,
        }
    path = np.asarray([initial_cash, *values], dtype=float)
    if not np.isfinite(path).all():
        return {
            "max_drawdown": None,
            "max_drawdown_abs": None,
            "max_drawdown_duration": None,
            "max_drawdown_duration_sessions": None,
            "drawdown_start": None,
            "drawdown_end": None,
        }
    peaks = np.maximum.accumulate(path)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = np.where(np.abs(peaks) > _EPS, path / peaks - 1.0, np.nan)
    reported = drawdowns[1:]
    if reported.size == 0 or not np.isfinite(reported).any():
        return {
            "max_drawdown": None,
            "max_drawdown_abs": None,
            "max_drawdown_duration": None,
            "max_drawdown_duration_sessions": None,
            "drawdown_start": None,
            "drawdown_end": None,
        }
    minimum = float(np.nanmin(reported))
    # A flat curve has no drawdown and therefore no drawdown-based ratios.
    if minimum >= -_EPS:
        duration = 0
    else:
        underwater = np.isfinite(drawdowns[1:]) & (drawdowns[1:] < -_EPS)
        current = 0
        duration = 0
        for is_underwater in underwater:
            current = current + 1 if is_underwater else 0
            duration = max(duration, current)
    return {
        "max_drawdown": minimum,
        "max_drawdown_abs": abs(minimum),
        "max_drawdown_duration": duration,
        "max_drawdown_duration_sessions": duration,
        "drawdown_start": None,
        "drawdown_end": None,
    }


def _regime_attribution(frame: pd.DataFrame, returns: list[float | None]) -> dict[str, dict[str, Any]]:
    if len(frame) < 2:
        return {}
    buckets: dict[str, list[float]] = defaultdict(list)
    for index in range(1, len(frame)):
        value = returns[index]
        if value is None:
            continue
        # The engine stores the prior-day regime on today's equity row, so the
        # current row is the regime that was in force for this realized return.
        raw_regime = frame.iloc[index].get("regime") if "regime" in frame.columns else None
        label = str(raw_regime) if raw_regime is not None and not pd.isna(raw_regime) else "unknown"
        buckets[label].append(value)
    result: dict[str, dict[str, Any]] = {}
    for regime, values in buckets.items():
        compounded = float(np.prod(1.0 + np.asarray(values, dtype=float)) - 1.0)
        result[regime] = {
            "sessions": len(values),
            "mean_daily_return": float(np.mean(values)),
            "cumulative_return": compounded,
        }
    return result


def compute_metrics(
    equity: pd.DataFrame,
    fills: pd.DataFrame,
    initial_cash: float,
    benchmark: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Compute auditable, daily performance metrics.

    Undefined statistics return ``None``.  This is intentional for one-session
    curves, zero-variance returns, absent close fills, and missing benchmark
    history; callers should render these as ``null``/``不可计算`` rather than
    inventing zeroes.
    """

    frame = _normalise_equity(equity)
    fills_frame = _normalise_fills(fills)
    starting_cash = _finite_float(initial_cash)
    values = [float(value) for value in frame["_value"].tolist()]
    dates = frame["_date"] if not frame.empty else pd.Series(dtype="datetime64[ns]")
    first_date = _date_text(dates.iloc[0]) if len(dates) else None
    last_date = _date_text(dates.iloc[-1]) if len(dates) else None
    end_value = values[-1] if values else None

    daily_returns: list[float | None] = []
    if values:
        daily_returns.append(_safe_return(values[0], starting_cash) if starting_cash is not None else None)
        for before, after in zip(values, values[1:]):
            daily_returns.append(_safe_return(after, before))
    finite_returns = [value for value in daily_returns if value is not None and isfinite(value)]
    daily_std = float(np.std(finite_returns, ddof=1)) if len(finite_returns) >= 2 else None
    sharpe = (
        float(np.mean(finite_returns) / daily_std * sqrt(_ANNUAL_SESSIONS))
        if daily_std is not None and daily_std > _EPS
        else None
    )
    downside = [min(value, 0.0) for value in finite_returns]
    downside_deviation = float(sqrt(np.mean(np.square(downside)))) if len(finite_returns) >= 2 else None
    sortino = (
        float(np.mean(finite_returns) / downside_deviation * sqrt(_ANNUAL_SESSIONS))
        if downside_deviation is not None and downside_deviation > _EPS
        else None
    )

    total_return = (
        _safe_return(end_value, starting_cash)
        if end_value is not None and starting_cash is not None
        else None
    )
    cagr = _annualized_return(starting_cash, end_value, len(values))
    drawdown = (
        _drawdown_stats(values, starting_cash) if starting_cash is not None else _drawdown_stats([], 0.0)
    )
    calmar = (
        cagr / abs(drawdown["max_drawdown"])
        if cagr is not None and drawdown["max_drawdown"] is not None and abs(drawdown["max_drawdown"]) > _EPS
        else None
    )

    if fills_frame.empty:
        costs = 0.0
        turnover_value = 0.0
        close_fills = pd.DataFrame()
    else:
        fee_values = pd.to_numeric(fills_frame.get("fee", pd.Series(dtype=float)), errors="coerce")
        costs = float(fee_values.fillna(0.0).sum())
        quantity = pd.to_numeric(fills_frame.get("quantity", pd.Series(dtype=float)), errors="coerce")
        price = pd.to_numeric(fills_frame.get("price", pd.Series(dtype=float)), errors="coerce")
        turnover_value = float((quantity.abs() * price).fillna(0.0).sum())
        realized = pd.to_numeric(fills_frame.get("realized_pnl", pd.Series(dtype=float)), errors="coerce")
        close_fills = fills_frame.loc[realized.notna()].copy()
        close_fills["_realized"] = realized.loc[close_fills.index]
    turnover = turnover_value / starting_cash if starting_cash is not None and starting_cash > _EPS else None
    close_pnl = close_fills["_realized"].astype(float).tolist() if not close_fills.empty else []
    wins = sum(1 for value in close_pnl if value > 0)
    losses = sum(1 for value in close_pnl if value < 0)
    profit = float(sum(value for value in close_pnl if value > 0))
    loss = float(sum(value for value in close_pnl if value < 0))
    closed_fill_win_rate = wins / len(close_pnl) if close_pnl else None
    closed_fill_profit_factor = profit / abs(loss) if loss < -_EPS else None

    exposures = pd.to_numeric(frame.get("exposure", pd.Series(dtype=float)), errors="coerce")
    finite_exposure = exposures.dropna().astype(float)
    average_exposure = float(finite_exposure.mean()) if not finite_exposure.empty else None
    max_exposure = float(finite_exposure.max()) if not finite_exposure.empty else None
    benchmark_metrics = _benchmark_returns(benchmark, dates, values)
    benchmark_return = benchmark_metrics["benchmark_return"]
    benchmark_metrics["excess_return"] = (
        total_return - benchmark_return if total_return is not None and benchmark_return is not None else None
    )
    coverage = _period_coverage(frame, starting_cash)
    annual_returns = {year: details["return"] for year, details in coverage["years"].items()}
    fill_statistics = {
        "scope": "closed_fill",
        "fill_count": int(len(fills_frame)),
        "closed_fill_count": len(close_pnl),
        "closed_fill_wins": wins,
        "closed_fill_losses": losses,
        "closed_fill_win_rate": closed_fill_win_rate,
        "closed_fill_profit_factor": closed_fill_profit_factor,
        "round_trip_episodes": "not_implemented",
    }

    metrics: dict[str, Any] = {
        "period": {"start": first_date, "end": last_date, "sessions": len(values)},
        "start_date": first_date,
        "end_date": last_date,
        "sessions": len(values),
        "total_return": total_return,
        "cagr": cagr,
        # Float mappings remain for compatibility.  period_coverage carries
        # the sample/full-year labels and boundaries.
        "annual_return": annual_returns,
        "annual_returns": annual_returns,
        "period_coverage": coverage,
        "cagr_basis": "annualized_from_sample_sessions_252",
        "cagr_is_annualized": True,
        "costs": costs,
        "total_costs": costs,
        "turnover_value": turnover_value,
        "turnover": turnover,
        "max_drawdown": drawdown["max_drawdown"],
        "max_drawdown_abs": drawdown["max_drawdown_abs"],
        "max_drawdown_duration": drawdown["max_drawdown_duration"],
        "max_drawdown_duration_sessions": drawdown["max_drawdown_duration_sessions"],
        "drawdown_start": drawdown["drawdown_start"],
        "drawdown_end": drawdown["drawdown_end"],
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "average_exposure": average_exposure,
        "max_exposure": max_exposure,
        "fill_count": int(len(fills_frame)),
        "closed_fill_count": len(close_pnl),
        "closed_fill_wins": wins,
        "closed_fill_losses": losses,
        "closed_fill_win_rate": closed_fill_win_rate,
        "closed_fill_profit_factor": closed_fill_profit_factor,
        # Deprecated compatibility aliases: these are closed-fill statistics,
        # never reconstructed round-trip trade/episode statistics.
        "close_fill_count": len(close_pnl),
        "trade_count": len(close_pnl),
        "winning_trades": wins if close_pnl else 0,
        "losing_trades": losses if close_pnl else 0,
        "win_rate": closed_fill_win_rate,
        "profit_factor": closed_fill_profit_factor,
        "deprecated_aliases": {
            "close_fill_count": "closed_fill_count",
            "trade_count": "closed_fill_count",
            "winning_trades": "closed_fill_wins",
            "losing_trades": "closed_fill_losses",
            "win_rate": "closed_fill_win_rate",
            "profit_factor": "closed_fill_profit_factor",
        },
        "fill_statistics": fill_statistics,
        "episode_statistics": {
            "status": "not_implemented",
            "definition": "No round-trip or position episode grouping is performed.",
        },
        "trade_pairing": "closed-fill scope only: non-null fills.realized_pnl are FIFO closed-lot net PnL including supplied buy/sell fees; no round-trip episode is inferred",
        "daily_return_count": len(finite_returns),
        "regime_attribution": _regime_attribution(frame, daily_returns),
        "daily_returns": daily_returns,
    }
    metrics.update(benchmark_metrics)
    return metrics


def walk_forward_windows(
    calendar: list[str],
    start: str,
    end: str,
    train_sessions: int = 252,
    test_sessions: int = 63,
) -> list[dict[str, str]]:
    """Return fixed-rule rolling train/test windows with complete OOS blocks.

    This helper only lays out the calendar.  It performs no parameter fitting
    and drops a trailing test fragment that is shorter than ``test_sessions``;
    callers should evaluate each returned OOS window independently.
    """

    if train_sessions <= 0 or test_sessions <= 0:
        raise ValueError("train_sessions and test_sessions must be positive")
    start_date = pd.Timestamp(start)
    end_date = pd.Timestamp(end)
    if start_date > end_date:
        raise ValueError("start must not be after end")
    dates = sorted(
        {
            pd.Timestamp(value).normalize()
            for value in calendar
            if pd.Timestamp(value).normalize() >= start_date.normalize()
            and pd.Timestamp(value).normalize() <= end_date.normalize()
        }
    )
    windows: list[dict[str, str]] = []
    test_start_index = train_sessions
    while test_start_index + test_sessions <= len(dates):
        train_start_index = test_start_index - train_sessions
        test_end_index = test_start_index + test_sessions - 1
        windows.append(
            {
                "train_start": dates[train_start_index].strftime("%Y-%m-%d"),
                "train_end": dates[test_start_index - 1].strftime("%Y-%m-%d"),
                "test_start": dates[test_start_index].strftime("%Y-%m-%d"),
                "test_end": dates[test_end_index].strftime("%Y-%m-%d"),
            }
        )
        # Advancing by the test block keeps OOS observations disjoint.  The
        # next training period may include prior OOS rows, as it would in a
        # fixed-rule rolling evaluation using all information then available.
        test_start_index += test_sessions
    return windows
