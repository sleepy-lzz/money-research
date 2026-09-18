from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

import ashare_agent.current_data as current_data
from ashare_agent.current_data import SHANGHAI, CurrentClient, normalize_bars, possible_limit_state
from ashare_agent.current_screen import assess, screen, verify_current
from ashare_agent.session_calendar import sessions


def _tencent_row(day: str, price: float = 10.0, volume: float = 100.0, amount: float = 10.0):
    """Tencent daily row: volume is hands and amount is ten-thousand CNY."""
    return [
        day,
        str(price),
        str(price),
        str(price + 0.5),
        str(price - 0.5),
        str(volume),
        "0",
        "0",
        str(amount),
    ]


def test_normalize_bars_scales_shenzhen_volume_and_amount_once():
    frame = normalize_bars(
        [_tencent_row("2025-01-02", price=10, volume=100, amount=10)],
        "sz000001",
        "2025-01-02",
    )

    assert frame.loc["2025-01-02", "volume"] == pytest.approx(10_000)
    assert frame.loc["2025-01-02", "amount"] == pytest.approx(100_000)


@pytest.mark.parametrize(
    ("rows", "end", "message"),
    [
        ([_tencent_row("2025-01-02")[:8]], "2025-01-02", "成交额"),
        ([_tencent_row("2025-01-02"), _tencent_row("2025-01-02")], "2025-01-02", "重复"),
        ([_tencent_row("2025-01-03")], "2025-01-02", "之后"),
    ],
)
def test_normalize_bars_rejects_missing_amount_duplicate_or_future_rows(rows, end, message):
    with pytest.raises(ValueError, match=message):
        normalize_bars(rows, "sz000001", end)


def _history(days: list[str], offset: float = 0.0) -> pd.DataFrame:
    close = pd.Series(
        [10.0 + offset + (2.0 * index / (len(days) - 1)) for index in range(len(days))],
        index=days,
        dtype=float,
    )
    return pd.DataFrame(
        {
            "trade_date": days,
            "open": close,
            "close": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "volume": 20_000_000.0,
            "amount": close * 20_000_000.0,
        },
        index=days,
    )


def _assess_fixture():
    days = pd.date_range("2024-01-01", periods=251, freq="D").strftime("%Y-%m-%d").tolist()
    raw = _history(days)
    # Deliberately reverse the adjusted frame: assess must align by trade date.
    adjusted = _history(days).iloc[::-1]
    last = raw.iloc[-1]
    cutoff = datetime.fromisoformat(days[-1] + "T16:00:00+08:00")
    quote = {
        "name": "Demo",
        "date": days[-1],
        "observed_at": days[-1] + "T15:30:00+08:00",
        **{field: float(last[field]) for field in ("open", "close", "high", "low", "volume", "amount")},
    }
    return days, raw, adjusted, quote, cutoff


def test_assess_uses_date_alignment_for_raw_and_qfq_series():
    days, raw, adjusted, quote, cutoff = _assess_fixture()

    row, reason = assess("sh600000", quote, raw, adjusted, days, "Demo", 100_000_000, cutoff)

    assert reason is None
    assert row["observed_sessions"] == 251
    assert row["momentum60"] > 0
    assert row["momentum120"] > 0


def test_assess_rejects_a_missing_benchmark_session_instead_of_intersecting_it_away():
    days, raw, adjusted, quote, cutoff = _assess_fixture()
    raw = raw.drop(index=days[120])

    with pytest.raises(ValueError, match="缺失"):
        assess("sh600000", quote, raw, adjusted, days, "Demo", 100_000_000, cutoff)


def test_possible_limit_state_is_only_a_conservative_warning():
    assert possible_limit_state(9.0, 10.0, "普通公司") == "possible_lower_limit"
    assert possible_limit_state(11.0, 10.0, "普通公司") == "possible_upper_limit"
    assert possible_limit_state(9.0, 10.0, "ST示例") == "unknown"
    assert possible_limit_state(9.0, None, "普通公司") == "unknown"


@pytest.mark.parametrize(
    ("quote_change", "expected"),
    [
        ({"date": "2024-09-06"}, "日期"),
        ({"observed_at": "2024-09-07T16:01:00+08:00"}, "日期/时间"),
        ({"name": "Other"}, "名称"),
    ],
)
def test_assess_rejects_cross_day_late_or_mismatched_quote(quote_change, expected):
    days, raw, adjusted, quote, cutoff = _assess_fixture()
    quote.update(quote_change)

    with pytest.raises(ValueError, match=expected):
        assess("sh600000", quote, raw, adjusted, days, "Demo", 100_000_000, cutoff)


def test_assess_excludes_risk_warning_name():
    days, raw, adjusted, quote, cutoff = _assess_fixture()
    quote["name"] = "ST Demo"

    row, reason = assess("sh600000", quote, raw, adjusted, days, "ST Demo", 100_000_000, cutoff)

    assert row is None
    assert "风险" in reason


class _FakeCurrentClient:
    def __init__(self, raw: pd.DataFrame, adjusted: pd.DataFrame, code: str = "sz000001"):
        self.raw = raw
        self.adjusted = adjusted
        self.code = code
        self.receipts = [{"source": "offline-fixture", "status": "complete"}]
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def bars(self, code, adjust="", end=None):
        self.calls.append((code, adjust))
        if code == "sh000300":
            return self.raw, ""
        if code != self.code:
            raise AssertionError(f"unexpected symbol: {code}")
        return (self.adjusted if adjust == "qfq" else self.raw), "Demo"

    def quotes(self, codes):
        last = self.raw.iloc[-1]
        day = self.raw.index[-1]
        return {
            code: {
                "code": code,
                "name": "Demo",
                "date": day,
                "observed_at": day + "T15:30:00+08:00",
                **{
                    field: float(last[field])
                    for field in ("open", "close", "high", "low", "volume", "amount")
                },
            }
            for code in codes
        }

    def universe(self, progress=lambda _: None):
        return [{"code": self.code, "name": "Demo"}]

    def close(self):
        self.closed = True


def test_screen_saves_offline_report_without_creating_a_ledger(tmp_path: Path):
    days = sessions("2025-01-01", "2026-09-11")[-260:]
    raw = _history(days, offset=5.0)
    client = _FakeCurrentClient(raw, raw.copy())
    cutoff = datetime.fromisoformat(days[-1] + "T16:30:00+08:00")

    result = screen(
        tmp_path / "success",
        symbols="000001.SZ",
        client=client,
        cutoff=cutoff,
        min_amount=100_000_000,
    )

    output = tmp_path / "success"
    assert result["status"] == "complete"
    assert result["valid_count"] == 1
    assert result["signal_policy"] == "next_session_only"
    assert result["signal_valid_for"] == "2026-09-14"
    assert result["candidates"][0]["risk_level"] in {"ordinary", "elevated"}
    assert "momentum20" in result["candidates"][0]
    assert (output / "result.json").is_file()
    assert (output / "report.html").is_file()
    assert (output / "candidates.csv").is_file()
    assert not list(tmp_path.rglob("*ledger*"))
    assert not list(tmp_path.rglob("account.sqlite"))


class _PartialCurrentClient(_FakeCurrentClient):
    def bars(self, code, adjust="", end=None):
        if code != "sh000300":
            raise RuntimeError("offline fixture history failure")
        return super().bars(code, adjust, end)


def test_screen_marks_data_failure_partial_and_still_writes_report(tmp_path: Path):
    days = sessions("2025-01-01", "2026-09-11")[-260:]
    raw = _history(days, offset=5.0)
    client = _PartialCurrentClient(raw, raw.copy())
    cutoff = datetime.fromisoformat(days[-1] + "T16:30:00+08:00")

    result = screen(
        tmp_path / "partial",
        symbols="000001.SZ",
        client=client,
        cutoff=cutoff,
        min_amount=100_000_000,
    )

    assert result["status"] == "partial"
    assert result["attempted_count"] == 1
    assert result["valid_count"] == 0
    assert "offline fixture history failure" in result["exclusions"][0]["reason"]
    assert (tmp_path / "partial" / "result.json").is_file()
    assert (tmp_path / "partial" / "report.html").is_file()


class _LowAmountStaleQuoteClient(_FakeCurrentClient):
    def quotes(self, codes):
        quotes = super().quotes(codes)
        stale_day = self.raw.index[-2]
        for quote in quotes.values():
            quote["date"] = stale_day
            quote["observed_at"] = stale_day + "T15:30:00+08:00"
        return quotes


def test_screen_marks_low_amount_with_stale_quote_as_partial_data_failure(tmp_path: Path):
    days = sessions("2025-01-01", "2026-09-11")[-260:]
    raw = _history(days, offset=5.0)
    raw["amount"] = 50_000_000.0
    client = _LowAmountStaleQuoteClient(raw, raw.copy())
    cutoff = datetime.fromisoformat(days[-1] + "T16:30:00+08:00")

    result = screen(
        tmp_path / "stale-low-amount",
        symbols="000001.SZ",
        client=client,
        cutoff=cutoff,
        min_amount=100_000_000,
    )

    assert result["status"] == "partial"
    assert result["attempted_count"] == 1
    assert result["valid_count"] == 0
    assert "日期/时间" in result["exclusions"][0]["reason"]
    assert "成交额低于门槛" not in result["exclusions"][0]["reason"]


@pytest.mark.parametrize("mismatch", ["benchmark_missing", "both_missing", "extra_holiday"])
def test_screen_calendar_rejects_shared_gaps_and_extra_holidays(tmp_path, mismatch):
    days = sessions("2025-01-01", "2026-09-11")[-260:]
    bad_days = sorted([*days, "2026-01-01"]) if mismatch == "extra_holiday" else [
        day for day in days if day != "2026-09-08"
    ]
    raw = _history(bad_days if mismatch == "both_missing" else days, offset=5.0)
    benchmark = _history(bad_days, offset=5.0)

    class Client(_FakeCurrentClient):
        def bars(self, code, adjust="", end=None):
            if code == "sh000300":
                return benchmark, "Demo"
            return super().bars(code, adjust, end)

    result = screen(
        tmp_path / mismatch, symbols="000001.SZ", client=Client(raw, raw.copy()),
        cutoff=datetime.fromisoformat(days[-1] + "T16:30:00+08:00"),
    )
    assert result["status"] == "blocked"
    assert result["attempted_count"] == 0
    assert any("独立交易日历不一致" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "错误状态"),
        ([], "错误状态"),
        ({"code": 0, "data": []}, "证券"),
        ({"code": 0, "data": {"sh600000": []}}, "证券"),
    ],
)
def test_current_bars_rejects_null_or_list_malformed_response(payload, message, tmp_path: Path, monkeypatch):
    client = CurrentClient(tmp_path / "cache", interval=0)
    monkeypatch.setattr(client, "get", lambda *args, **kwargs: json.dumps(payload).encode("utf-8"))

    try:
        with pytest.raises(ValueError, match=message):
            client.bars("sh600000", end="2025-01-02")
    finally:
        client.close()


@pytest.mark.parametrize("qt", [[], None])
def test_current_bars_allows_empty_qt_name_but_stock_verification_fails_closed(qt, tmp_path: Path, monkeypatch):
    payload = {
        "code": 0,
        "data": {
            "sh600000": {
                "day": [_tencent_row("2025-01-02")],
                "qt": qt,
            }
        },
    }
    client = CurrentClient(tmp_path / "cache", interval=0)
    monkeypatch.setattr(client, "get", lambda *args, **kwargs: json.dumps(payload).encode("utf-8"))

    try:
        frame, name = client.bars("sh600000", end="2025-01-02")
    finally:
        client.close()

    assert name == ""
    last = frame.iloc[-1]
    quote = {
        "name": "Demo",
        "date": "2025-01-02",
        "observed_at": "2025-01-02T15:30:00+08:00",
        **{field: float(last[field]) for field in ("open", "close", "high", "low", "volume", "amount")},
    }
    with pytest.raises(ValueError, match="缺少可核对"):
        verify_current(
            quote,
            frame,
            "2025-01-02",
            name,
            datetime.fromisoformat("2025-01-02T16:00:00+08:00"),
        )


class _FrozenDateTime(datetime):
    current = datetime(2025, 1, 2, 15, 59, tzinfo=SHANGHAI)

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        return value if tz is not None else value.replace(tzinfo=None)


def _seed_cached_response(root: Path, url: str, params: dict, fetched_at: str, raw: bytes):
    identity = json.dumps({"url": url, "params": params}, sort_keys=True).encode()
    folder = root / hashlib.sha256(identity).hexdigest() / "fixture"
    folder.mkdir(parents=True)
    (folder / "response.bin").write_bytes(raw)
    (folder / "manifest.json").write_text(
        json.dumps({"fetched_at": fetched_at, "sha256": hashlib.sha256(raw).hexdigest()}),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("fetched_hour", "expect_cached"),
    [(15, False), (16, True)],
)
def test_current_cache_reuse_requires_same_day_after_16(tmp_path: Path, monkeypatch, fetched_hour, expect_cached):
    monkeypatch.setattr(current_data, "datetime", _FrozenDateTime)
    _FrozenDateTime.current = datetime(2025, 1, 2, 16, 30, tzinfo=SHANGHAI)
    root = tmp_path / f"cache-{fetched_hour}"
    client = CurrentClient(root, ttl=1800, interval=0)
    url, params, cached_raw = "https://fixture.invalid", {"kind": "bars"}, b"cached"
    _seed_cached_response(
        root,
        url,
        params,
        f"2025-01-02T{fetched_hour:02d}:15:00+08:00",
        cached_raw,
    )

    calls = []

    class Response:
        content = b"network"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(client.http, "get", lambda *args, **kwargs: calls.append(args) or Response())
    try:
        value = client.get(url, params)
    finally:
        client.close()

    assert value == (cached_raw if expect_cached else b"network")
    assert bool(calls) is (not expect_cached)
