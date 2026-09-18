import pandas as pd
import pytest

from ashare_agent.analytics import compute_metrics, walk_forward_windows


def test_metrics_use_initial_cash_and_previous_benchmark_anchor() -> None:
    equity = pd.DataFrame(
        {
            "trade_date": ["2024-01-02", "2024-01-03"],
            "equity": [100.0, 110.0],
            "cash": [100.0, 110.0],
            "receivable": [0.0, 0.0],
            "exposure": [0.0, 0.0],
            "regime": ["risk_on", "risk_off"],
        }
    )
    benchmark = pd.DataFrame(
        {
            "trade_date": ["2024-01-01", "2024-01-02", "2024-01-03"],
            "ts_code": ["000300.SH"] * 3,
            "close": [100, 101, 102],
        }
    )
    metrics = compute_metrics(equity, pd.DataFrame(), 100.0, benchmark)
    assert metrics["total_return"] == pytest.approx(0.1)
    assert metrics["benchmark_anchor_date"] == "2024-01-01"
    assert metrics["benchmark_return"] == pytest.approx(0.02)
    assert metrics["excess_return"] == pytest.approx(0.08)
    assert metrics["win_rate"] is None
    assert metrics["profit_factor"] is None


def test_close_fill_pairing_and_costs() -> None:
    equity = pd.DataFrame(
        {"trade_date": ["2024-01-01", "2024-01-02", "2024-01-03"], "equity": [100, 99, 103]}
    )
    fills = pd.DataFrame(
        {
            "fill_id": [1, 2, 3],
            "order_id": [1, 2, 3],
            "ts_code": ["A", "A", "B"],
            "side": ["BUY", "SELL", "BUY"],
            "quantity": [10, 10, 5],
            "price": [10, 9, 20],
            "fee": [1.0, 1.0, 0.5],
            "trade_date": ["2024-01-01"] * 3,
            "realized_pnl": [None, -10.0, None],
        }
    )
    metrics = compute_metrics(equity, fills, 100.0)
    assert metrics["costs"] == 2.5
    assert metrics["turnover_value"] == 290.0
    assert metrics["closed_fill_count"] == 1
    assert metrics["closed_fill_wins"] == 0
    assert metrics["closed_fill_losses"] == 1
    assert metrics["closed_fill_win_rate"] == 0.0
    assert metrics["closed_fill_profit_factor"] == 0.0
    assert metrics["close_fill_count"] == 1
    assert metrics["win_rate"] == 0.0
    assert metrics["profit_factor"] == 0.0
    assert metrics["deprecated_aliases"]["win_rate"] == "closed_fill_win_rate"
    assert metrics["deprecated_aliases"]["profit_factor"] == "closed_fill_profit_factor"
    assert metrics["episode_statistics"]["status"] == "not_implemented"


def test_annual_returns_expose_sample_coverage_and_annualized_cagr() -> None:
    equity = pd.DataFrame(
        {
            "trade_date": ["2023-01-01", "2023-12-31", "2024-01-02", "2024-12-31"],
            "equity": [100.0, 110.0, 110.0, 121.0],
        }
    )
    metrics = compute_metrics(equity, pd.DataFrame(), 100.0)

    assert metrics["annual_return"]["2023"] == pytest.approx(0.1)
    assert metrics["annual_return"]["2024"] == pytest.approx(0.1)
    assert metrics["period_coverage"]["sample_start"] == "2023-01-01"
    assert metrics["period_coverage"]["sample_end"] == "2024-12-31"
    assert metrics["period_coverage"]["years"]["2023"]["coverage"] == "full_year_coverage_unverified"
    assert metrics["period_coverage"]["years"]["2023"]["full_year_verified"] is False
    assert metrics["period_coverage"]["years"]["2024"]["coverage"] == "partial_year"
    assert metrics["period_coverage"]["years"]["2024"]["is_partial"] is True
    assert metrics["cagr_is_annualized"] is True
    assert metrics["cagr_basis"] == "annualized_from_sample_sessions_252"


def test_regime_attribution_uses_row_period_label() -> None:
    equity = pd.DataFrame(
        {
            "trade_date": ["2024-01-01", "2024-01-02", "2024-01-03"],
            "equity": [100.0, 110.0, 99.0],
            "regime": ["unknown", "risk_on", "risk_off"],
        }
    )
    metrics = compute_metrics(equity, pd.DataFrame(), 100.0)
    assert metrics["regime_attribution"]["risk_on"]["cumulative_return"] == pytest.approx(0.1)
    assert metrics["regime_attribution"]["risk_off"]["cumulative_return"] == pytest.approx(-0.1)


def test_walk_forward_oos_blocks_do_not_overlap() -> None:
    calendar = pd.date_range("2024-01-01", periods=10, freq="D").strftime("%Y-%m-%d").tolist()
    windows = walk_forward_windows(calendar, calendar[0], calendar[-1], train_sessions=3, test_sessions=2)
    assert windows == [
        {
            "train_start": "2024-01-01",
            "train_end": "2024-01-03",
            "test_start": "2024-01-04",
            "test_end": "2024-01-05",
        },
        {
            "train_start": "2024-01-03",
            "train_end": "2024-01-05",
            "test_start": "2024-01-06",
            "test_end": "2024-01-07",
        },
        {
            "train_start": "2024-01-05",
            "train_end": "2024-01-07",
            "test_start": "2024-01-08",
            "test_end": "2024-01-09",
        },
    ]
