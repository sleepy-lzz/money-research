from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import ashare_agent.live_quotes as live_quotes
from ashare_agent.current_data import SHANGHAI

NOW = datetime(2026, 9, 11, 10, 0, 0, tzinfo=SHANGHAI)


def _sina(
    symbol: str = "sh600000",
    *,
    price: str = "10.00",
    bid: str = "9.99",
    ask: str = "10.01",
    when: str = "10:00:00",
) -> bytes:
    parts = [""] * 32
    parts[0] = "浦发银行"
    parts[1] = "10.00"
    parts[2] = "9.99"
    parts[3] = price
    parts[6] = bid
    parts[7] = ask
    parts[8] = "1000"
    parts[10] = "100"
    parts[20] = "100"
    parts[30] = "2026-09-11"
    parts[31] = when
    return f'var hq_str_{symbol}="{",".join(parts)}";'.encode("gb18030")


def _tencent(
    symbol: str = "sh600000",
    *,
    price: str = "10.00",
    bid: str = "9.99",
    ask: str = "10.01",
    when: str = "20260911100000",
) -> str:
    parts = [""] * 37
    parts[1] = "浦发银行"
    parts[3] = price
    parts[4] = "9.99"
    parts[5] = "10.00"
    parts[9] = bid
    parts[10] = "100"
    parts[19] = ask
    parts[20] = "100"
    parts[30] = when
    parts[36] = "1000"
    return f'v_{symbol}="{"~".join(parts)}";'


class _Client:
    raw_sina = _sina()
    raw_tencent = _tencent()
    available_at = NOW.isoformat()
    refreshes: list[bool] = []

    def __init__(self, root: Path):
        self.root = root
        self.receipts: list[dict[str, str]] = []

    def get(self, url: str, *, refresh: bool = False):
        self.refreshes.append(refresh)
        assert refresh is True
        self.receipts.append({"url": url, "available_at": self.available_at, "status": "http_received"})
        return self.raw_sina if "sina" in url else self.raw_tencent

    def close(self):
        pass


def _run(monkeypatch: pytest.MonkeyPatch, client_type: type[_Client] = _Client, **attrs):
    client_type.raw_sina = _sina()
    client_type.raw_tencent = _tencent()
    for key, value in attrs.items():
        setattr(client_type, key, value)
    client_type.refreshes = []
    monkeypatch.setattr(live_quotes, "CurrentClient", client_type)
    return live_quotes.collect_quotes(Path("project"), ["600000.SH"], now=NOW)


def test_collect_quotes_refreshes_both_sources_and_returns_decimal_safe_fields(monkeypatch: pytest.MonkeyPatch):
    result = _run(monkeypatch)

    quote = result["quotes"]["600000.SH"]
    assert _Client.refreshes == [True, True]
    assert quote["status"] == "ok"
    assert quote["price"] == "10.00"
    assert quote["tradeability_status"] == "not_proven"
    assert quote["liquidity_observed"] is True
    assert quote["ask"] == "10.01"
    assert quote["source_observed_at"]["sina"] == NOW.isoformat()
    assert len(result["receipts"]) == 2
    assert all("nan" not in str(quote).lower() for _ in [0])


def test_collect_quotes_marks_old_observed_or_available_time_stale(monkeypatch: pytest.MonkeyPatch):
    old = (NOW - timedelta(seconds=91)).strftime("%H:%M:%S")
    result = _run(monkeypatch, raw_sina=_sina(when=old), raw_tencent=_tencent(when=(NOW - timedelta(seconds=1)).strftime("%Y/%m/%d %H:%M:%S")))

    quote = result["quotes"]["600000.SH"]
    assert quote["status"] == "stale"
    assert any("stale" in reason for reason in quote["reasons"])


def test_collect_quotes_marks_source_price_conflict(monkeypatch: pytest.MonkeyPatch):
    result = _run(monkeypatch, raw_tencent=_tencent(price="10.10", ask="10.11"))

    quote = result["quotes"]["600000.SH"]
    assert quote["status"] == "conflict"
    assert "price_conflict" in quote["reasons"]


def test_collect_quotes_marks_possible_lower_limit_without_claiming_tradeability(monkeypatch: pytest.MonkeyPatch):
    result = _run(monkeypatch, raw_sina=_sina(price="9.00", bid="8.99", ask="9.00"),
                  raw_tencent=_tencent(price="9.00", bid="8.99", ask="9.00"))

    quote = result["quotes"]["600000.SH"]
    assert quote["limit_state"] == "possible_lower_limit"
    assert quote["tradeability_status"] == "not_proven"


def test_collect_quotes_decodes_tencent_gb18030_bytes(monkeypatch: pytest.MonkeyPatch):
    result = _run(monkeypatch, raw_tencent=_tencent().encode("gb18030"))

    quote = result["quotes"]["600000.SH"]
    assert quote["status"] == "ok"
    assert quote["name"] == "浦发银行"


def test_missing_tencent_cumulative_volume_does_not_prove_liquidity(monkeypatch: pytest.MonkeyPatch):
    raw = _tencent().replace("~1000\";", "~\";")
    result = _run(monkeypatch, raw_tencent=raw)

    quote = result["quotes"]["600000.SH"]
    assert quote["status"] == "ok"
    assert quote["liquidity_observed"] is False
    assert "liquidity_insufficient" in quote["reasons"]


def test_crossed_book_is_rejected_and_locked_book_has_no_liquidity(monkeypatch: pytest.MonkeyPatch):
    crossed = _run(monkeypatch, raw_sina=_sina(bid="10.20"), raw_tencent=_tencent(bid="10.20"))
    assert crossed["quotes"]["600000.SH"]["status"] == "missing"
    assert any("crossed_book" in reason for reason in crossed["quotes"]["600000.SH"]["reasons"])

    locked = _run(monkeypatch, raw_sina=_sina(bid="10.00", ask="10.00"), raw_tencent=_tencent(bid="10.00", ask="10.00"))
    assert locked["quotes"]["600000.SH"]["status"] == "ok"
    assert locked["quotes"]["600000.SH"]["liquidity_observed"] is False


@pytest.mark.parametrize(
    ("sina", "tencent", "reason"),
    [
        (_sina(price="NaN"), _tencent(), "price_invalid"),
        (_sina(ask=""), _tencent(), "ask_invalid"),
    ],
)
def test_collect_quotes_rejects_nan_or_missing_required_fields(
    monkeypatch: pytest.MonkeyPatch, sina: bytes, tencent: str, reason: str
):
    result = _run(monkeypatch, raw_sina=sina, raw_tencent=tencent)

    quote = result["quotes"]["600000.SH"]
    assert quote["status"] == "missing"
    assert any(reason in item for item in quote["reasons"])
