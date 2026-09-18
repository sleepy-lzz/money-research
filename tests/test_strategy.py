from types import SimpleNamespace

import numpy as np
import pandas as pd

from ashare_agent.strategy import is_rebalance_day, market_regime, rank_candidates, select_targets


def _snapshot(n: int = 150):
    calendar = pd.bdate_range("2024-01-01", periods=n + 1).strftime("%Y-%m-%d").tolist()
    rows = []
    securities = []
    for code, direction in [
        ("AAA.SZ", 1.0),
        ("BBB.SZ", -1.0),
        ("CCC.SZ", 1.0),
        ("DDD.SZ", 1.0),
        ("EEE.SZ", 1.0),
    ]:
        securities.append(
            {
                "ts_code": code,
                "list_date": calendar[0],
                "delist_date": None,
                "exchange": "SZSE",
                "board": "MAIN",
                "name": code,
            }
        )
        for i, day in enumerate(calendar[:n]):
            close = 10.0 + direction * i * 0.05
            row = {
                "ts_code": code,
                "trade_date": day,
                "open": close,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 2_000_000,
                "amount": 200_000_000,
                "adj_factor": 1.0,
                "up_limit": close * 1.1,
                "down_limit": close * 0.9,
                "suspended": False,
                "is_st": False,
                "state_known": True,
                "industry": "Tech" if code in {"AAA.SZ", "BBB.SZ"} else code,
                "industry_known": True,
            }
            if code == "CCC.SZ":
                row["is_st"] = True
            if code == "DDD.SZ" and i == n - 1:
                row["suspended"] = True
            if code == "EEE.SZ" and i == n - 1:
                row["state_known"] = False
            rows.append(row)
    bench = pd.DataFrame(
        {
            "trade_date": calendar[:n],
            "ts_code": "000300.SH",
            "close": np.linspace(100, 140, n),
        }
    )
    return SimpleNamespace(
        market=pd.DataFrame(rows),
        securities=pd.DataFrame(securities),
        calendar=calendar,
        benchmarks=bench,
    )


def test_rank_is_point_in_time_and_exposes_guards():
    snapshot = _snapshot()
    as_of = snapshot.calendar[149]
    # A future observation must not change the result.
    future = snapshot.market.iloc[0].copy()
    future["trade_date"] = snapshot.calendar[150]
    future["close"] = 10_000
    snapshot.market = pd.concat([snapshot.market, pd.DataFrame([future])], ignore_index=True)
    result = rank_candidates(
        snapshot,
        as_of,
        {"min_listing_days": 1, "liquidity_min": 100_000_000, "candidate_count": 20},
    )
    by_code = result.set_index("ts_code")
    assert by_code.loc["AAA.SZ", "eligible"]
    assert by_code.loc["AAA.SZ", "rank"] == 1
    assert "trend_below_ma120" in by_code.loc["BBB.SZ", "reason"]
    assert "st" in by_code.loc["CCC.SZ", "reason"]
    assert "suspended" in by_code.loc["DDD.SZ", "reason"]
    assert "status_unknown" in by_code.loc["EEE.SZ", "reason"]


def test_rebalance_uses_next_known_session_and_targets_keep_suspension():
    calendar = ["2024-01-04", "2024-01-05", "2024-01-08"]
    assert is_rebalance_day(calendar, "2024-01-05")
    assert not is_rebalance_day(calendar, "2024-01-08")

    ranking = pd.DataFrame(
        [
            {"ts_code": "HOLD.SZ", "rank": pd.NA, "eligible": False, "reason": "suspended", "industry": "A"},
            {"ts_code": "NEW.SZ", "rank": 1, "eligible": True, "reason": "", "industry": "B"},
        ]
    )
    targets = select_targets(ranking, ["HOLD.SZ"], True, {"target_weight": 0.08})
    assert targets == {"HOLD.SZ": 0.08, "NEW.SZ": 0.08}


def test_market_regime_is_rule_score_not_probability():
    snapshot = _snapshot(220)
    regime = market_regime(snapshot, snapshot.calendar[219])
    assert regime["regime"] == "bull"
    assert regime["score"] == 2
    assert regime["score"] != 1.0
