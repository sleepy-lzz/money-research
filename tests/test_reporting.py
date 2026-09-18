import json

from ashare_agent.reporting import write_report


def test_write_report_is_offline_and_escapes_html(tmp_path) -> None:
    paths = write_report(
        tmp_path,
        {
            "run_id": "<run>",
            "mode": "BACKTEST",
            "snapshot_id": "fixture",
            "synthetic": True,
            "as_of": "2024-01-02",
            "config": {},
            "metrics": {
                "total_return": 0.1,
                "cagr": 0.2,
                "sharpe": 0.7,
                "profit_factor": 0.4448,
                "closed_fill_win_rate": 0.5,
                "benchmark_anchor_date": "2024-01-01",
                "period_coverage": {
                    "sample_start": "2024-02-01",
                    "sample_end": "2024-12-30",
                    "sample_sessions": 2,
                    "sample_coverage": "partial_natural_years",
                    "years": {
                        "2024": {
                            "return": 0.1,
                            "sample_start": "2024-02-01",
                            "sample_end": "2024-12-30",
                            "sessions": 2,
                            "coverage": "partial_year",
                            "is_partial": True,
                        }
                    },
                },
            },
            "candidates": [{"symbol": "<script>alert(1)</script>"}],
            "research": [],
            "risk": {"halted": False},
            "regime": {"regime": "neutral"},
            "orders": [],
            "fills": [{"fill_id": "f1", "side": "BUY"}],
            "positions": [],
            "equity": [{"trade_date": "2024-01-02", "equity": 100}],
            "warnings": ["fixture"],
            "data_quality": {"status": "synthetic"},
        },
    )
    assert set(paths) == {"markdown", "html", "json"}
    html = open(paths["html"], encoding="utf-8").read()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "风险状态" in html and "当前市场状态" in html and "成交" in html
    assert "0.4448" in html and "44.48%" not in html
    assert "0.7000" in html and "70.00%" not in html
    assert "partial_year" in html and "样本期覆盖" in html
    assert "平仓成交胜率（closed-fill）" in html
    assert "固定规则滚动 OOS" in html and "no parameter fitting" in html
    assert "https://" not in html
    payload = json.loads(open(paths["json"], encoding="utf-8").read())
    assert payload["synthetic"] is True
