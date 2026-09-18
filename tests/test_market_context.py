import json
from datetime import datetime

import pytest

from ashare_agent.market_context import FED_URL, TOPICS, fetch_market_context

NOW = datetime.fromisoformat("2026-09-11T16:30:00+08:00")


class Client:
    def __init__(self, rows=None, rss=None, fetched="2026-09-11T16:20:00+08:00", available=None):
        self.rows = rows if rows is not None else [article()]
        self.rss = rss or b"<rss><channel /></rss>"
        self.fetched, self.available = fetched, available or fetched
        self.receipts, self.calls = [], []

    def get(self, url, params=None, refresh=False):
        self.calls.append((url, params, refresh))
        self.receipts.append(
            dict(url=url, params=params, fetched_at=self.fetched, available_at=self.available)
        )
        if url == FED_URL:
            return self.rss
        return json.dumps({"result": {"cmsArticleWebOld": self.rows}}).encode()


def article(**kwargs):
    return (
        dict(
            title="货币政策与中国经济：美联储、关税及原油观察",
            content="相关主题报道，并非事件已确认",
            url="https://example.invalid/news",
            showTime="2026-09-11 10:00:00",
        )
        | kwargs
    )


def test_bounded_topics_merge_hits_without_votes_or_trading_weight():
    client = Client([article(), article()])
    result = fetch_market_context(client, NOW)
    assert len(client.calls) == 6 and all(call[2] for call in client.calls)
    assert len(result["evidence"]) == 1
    row = result["evidence"][0]
    assert set(row["topics"]) == set(TOPICS)
    assert row["trading_eligible"] is False and row["direction"] == "未判定"
    assert row["available_at"] == client.available
    assert "sentiment" not in row and "ts_code" not in row
    assert all(
        item["accepted"] == 1 for key, item in result["coverage"].items() if key.startswith("eastmoney")
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"showTime": "2026-09-11"},
        {"showTime": "20260911"},
        {"showTime": "2026-W37-5"},
        {"showTime": "2026-09-11Z"},
        {"showTime": "2026-09-11+08:00"},
        {"showTime": "2026-09-11 18:00:00"},
        {"showTime": "2026-08-01 10:00:00"},
        {"url": "javascript:alert(1)"},
        {"url": "https://user:password@example.invalid/news"},
        {"title": "无关", "content": "无关材料"},
    ],
)
def test_unverifiable_future_old_unsafe_or_unrelated_material_not_accepted(changes):
    assert not fetch_market_context(Client([article(**changes)]), NOW)["evidence"]


@pytest.mark.parametrize(
    "fetched,available",
    [
        ("2026-09-11T17:00:00+08:00", "2026-09-11T17:00:00+08:00"),
        ("2026-09-11T16:00:00+08:00", "2026-09-11T17:00:00+08:00"),
        ("2026-09-11T16:00:00+08:00", "2026-09-11T15:00:00+08:00"),
        ("2026-09-11T16:00:00", "2026-09-11T16:00:00"),
    ],
)
def test_actual_receipt_times_cannot_be_backdated(fetched, available):
    result = fetch_market_context(Client(fetched=fetched, available=available), NOW)
    assert not result["evidence"]
    assert all(c["status"] in {"missing", "invalid_time"} for c in result["coverage"].values())


def test_official_rss_keeps_timezone_and_source_without_claiming_market_direction():
    rss = b"""<rss><channel><item><title>Federal Reserve issues statement</title>
    <description>FOMC statement</description><link>https://www.federalreserve.gov/newsevents/release.htm</link>
    <pubDate>Thu, 10 Sep 2026 14:00:00 -0400</pubDate></item></channel></rss>"""
    result = fetch_market_context(Client([], rss), NOW)
    (row,) = result["evidence"]
    assert row["published_at"] == "2026-09-11T02:00:00+08:00"
    assert row["quality"] == "official_feed_timestamp" and row["direction"] == "未判定"
    assert not fetch_market_context(Client([], rss.replace(b" -0400", b"")), NOW)["evidence"]
    assert not fetch_market_context(
        Client([], rss.replace(b"www.federalreserve.gov", b"example.invalid")), NOW
    )["evidence"]


def test_revision_retains_same_url_but_changed_content_as_new_evidence():
    old = fetch_market_context(Client(), NOW)["evidence"][0]
    new = fetch_market_context(Client([article(content="美联储澄清此前传闻")]), NOW)["evidence"][0]
    assert old["url"] == new["url"] and old["evidence_id"] != new["evidence_id"]
    assert old["content_hash"] != new["content_hash"]


def test_failure_isolated_and_empty_or_malformed_feed_not_complete():
    class Broken(Client):
        def get(self, url, params=None, refresh=False):
            if url == FED_URL:
                raise TimeoutError("network down")
            return super().get(url, params, refresh)

    result = fetch_market_context(Broken(), NOW)
    assert result["evidence"] and result["coverage"]["fed:global_rates"]["status"] == "missing"
    result = fetch_market_context(Client([], b"<!DOCTYPE rss><rss><channel /></rss>"), NOW)
    assert result["coverage"]["fed:global_rates"]["status"] == "missing"
    assert result["coverage"]["eastmoney:cn_policy"]["status"] == "no_recent_evidence"


def test_live_cutoff_sampled_after_fetch_and_receipt_identity_checked(monkeypatch):
    from ashare_agent import market_context

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(market_context, "datetime", Clock)
    assert fetch_market_context(Client())["evidence"]

    class WrongReceipt(Client):
        def get(self, url, params=None, refresh=False):
            raw = super().get(url, params, refresh)
            self.receipts[-1]["url"] = "https://example.invalid/unrelated"
            return raw

    assert not fetch_market_context(WrongReceipt(), NOW)["evidence"]
