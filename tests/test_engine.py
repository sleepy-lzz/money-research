from copy import deepcopy

import pandas as pd
import pytest

from ashare_agent.config import Settings
from ashare_agent.data import make_demo_snapshot
from ashare_agent.engine import Engine


@pytest.fixture(scope="module")
def snapshot():
    data = make_demo_snapshot(285)
    # An explicit liquid test fixture; does not relax production strategy thresholds.
    data.market["volume"] *= 30
    data.market["amount"] *= 30
    return data


def test_backtest_matches_restartable_daily_paper_and_duplicate_is_noop(snapshot, tmp_path):
    dates = sorted(snapshot.market.trade_date.unique())[-15:]
    settings = Settings()
    backtest = Engine(snapshot, settings)
    backtest.run(dates[0], dates[-1])
    fills = backtest.ledger.rows("fills")
    assert fills, "Acceptance must exercise actual filled orders, not only a cash-only path"
    assert any(f["side"] == "BUY" for f in fills)
    path = tmp_path / "paper.sqlite"
    paper = Engine(snapshot, settings, path)
    for day in dates[:7]:
        paper.process_day(day)
    paper.close()
    paper = Engine(snapshot, settings, path)
    for day in dates[7:]:
        paper.process_day(day)
    before = (paper.ledger.rows("fills"), paper.ledger.rows("orders"), paper.ledger.rows("equity"))
    paper.process_day(dates[-1])
    assert before == (paper.ledger.rows("fills"), paper.ledger.rows("orders"), paper.ledger.rows("equity"))
    assert paper.ledger.rows("fills") == backtest.ledger.rows("fills")
    assert paper.ledger.rows("equity") == backtest.ledger.rows("equity")
    with pytest.raises(ValueError, match="old as-of"):
        paper.process_day(dates[0])
    with pytest.raises(ValueError, match="as-of date"):
        paper.payload({"as_of": dates[0]}, "PAPER_REPLAY", "old")
    orders = {o["order_id"]: o for o in paper.ledger.rows("orders")}
    for fill in fills:
        assert orders[fill["order_id"]]["decision_date"] < fill["trade_date"]
    paper.close()
    backtest.close()


def test_future_price_changes_do_not_alter_past_orders(snapshot):
    dates = sorted(snapshot.market.trade_date.unique())[-15:]
    changed = deepcopy(snapshot)
    mask = changed.market.trade_date > dates[5]
    for col in ("open", "high", "low", "close", "up_limit", "down_limit"):
        changed.market.loc[mask, col] *= 1.2
    first, second = Engine(snapshot, Settings()), Engine(changed, Settings())
    first.run(dates[0], dates[5])
    second.run(dates[0], dates[5])
    assert first.ledger.rows("orders") == second.ledger.rows("orders")
    assert first.ledger.rows("fills") == second.ledger.rows("fills")
    first.close()
    second.close()


def test_paper_rejects_skipped_sessions_and_revised_history(snapshot, tmp_path):
    dates = sorted(snapshot.market.trade_date.unique())[-15:]
    path = tmp_path / "paper.sqlite"
    engine = Engine(snapshot, Settings(), path)
    engine.process_day(dates[0])
    with pytest.raises(ValueError, match="one trading day"):
        engine.process_day(dates[2])
    engine.close()
    changed = deepcopy(snapshot)
    changed.market.loc[0, "amount"] *= 2
    with pytest.raises(ValueError, match="data changed"):
        Engine(changed, Settings(), path)


def test_missing_corporate_action_evidence_and_late_daily_data_block(snapshot):
    changed = deepcopy(snapshot)
    changed.metadata["actions_complete"] = False
    with pytest.raises(ValueError, match="research-only"):
        Engine(changed, Settings())
    changed = deepcopy(snapshot)
    day = sorted(changed.market.trade_date.unique())[-1]
    changed.market.loc[changed.market.trade_date == day, "available_at"] = "2030-01-01T18:00:00+08:00"
    engine = Engine(changed, Settings())
    with pytest.raises(ValueError, match="unavailable at decision"):
        engine.process_day(day)
    assert not engine.ledger.rows("sessions")
    engine.close()


def test_forward_never_accepts_historical_synthetic_as_real_evidence(snapshot):
    engine = Engine(snapshot, Settings())
    with pytest.raises(ValueError):
        engine.process_day(snapshot.market.trade_date.max(), forward=True)
    assert not engine.ledger.rows("orders")
    engine.close()


def test_period_metrics_match_cash_ledger(snapshot):
    engine = Engine(snapshot, Settings())
    dates = sorted(snapshot.market.trade_date.unique())[-10:]
    last = engine.run(dates[0], dates[-1])
    payload = engine.payload(last, "BACKTEST", "acceptance")
    assert payload["synthetic"] is True
    assert payload["metrics"]["total_return"] == pytest.approx(payload["equity"][-1]["equity"] / 100000 - 1)
    assert payload["metrics"]["costs"] == pytest.approx(sum(f["fee"] for f in payload["fills"]))
    assert pd.DataFrame(payload["equity"])["cash"].min() >= 0
    assert len(payload["provenance"]["source_hash"]) == 64
    assert len(payload["provenance"]["data_prefix_hash"]) == 64
    engine.close()
