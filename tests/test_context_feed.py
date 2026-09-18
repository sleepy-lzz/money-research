from __future__ import annotations

import json
from datetime import datetime

import pytest

from ashare_agent.context_feed import EASTMONEY_SEARCH_URL, GUBA_URL, fetch_context
from ashare_agent.current_data import SHANGHAI


class FakeClient:
    def __init__(
        self,
        payloads,
        *,
        community_payloads=None,
        fetched_at="2026-09-10T12:00:00+08:00",
        failures=None,
    ):
        self.payloads = payloads
        self.community_payloads = community_payloads or {}
        self.fetched_at = fetched_at
        self.failures = set(failures or ())
        self.receipts = []
        self.calls = []

    def get(self, url, params=None, refresh=False):
        if url == GUBA_URL.format(code="000000"):
            raise AssertionError("invalid community fixture URL")
        if url.startswith("https://guba.eastmoney.com/list,"):
            code = url.split(",", 1)[1].split(".", 1)[0]
            payload = self.community_payloads.get(code, '{"re":[]}')
        else:
            code = json.loads(params["param"])["keyword"]
            payload = self.payloads.get(code, {"result": {"cmsArticleWebOld": []}})
        self.calls.append((url, params, refresh))
        if code in self.failures:
            raise TimeoutError("fixture timeout")
        self.receipts.append({"fetched_at": self.fetched_at, "available_at": self.fetched_at})
        return payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()


def article(code="600000", title="600000 上涨", *, published="2026-09-10 10:00:00", content=None, url=None):
    return {
        "title": title,
        "content": content or f"<p>{title} 的公告内容</p>",
        "url": url or f"https://example.invalid/{code}/{published}",
        "showTime": published,
        "stockCode": code,
    }


def response(*rows):
    return {"result": {"cmsArticleWebOld": list(rows)}}


def community_row(
    code="600000",
    post_id=1771542407,
    title="600000 上涨讨论",
    published="2026-09-10 10:00:00",
    summary="<p>摘要内容</p>",
):
    return {
        "post_id": post_id,
        "post_title": title,
        "stockbar_code": code,
        "post_publish_time": published,
        "post_last_time": "2026-09-10 23:59:59",
        "post_summary": summary,
        "user_nick_name": "must-not-be-stored",
        "ip": "192.0.2.1",
    }


def community_response(*rows, suffix="; window.evil = true;"):
    return "<script>var article_list=" + json.dumps({"re": list(rows)}) + suffix + "</script>"


def test_fetch_context_is_bounded_evidence_mapped_and_deduplicated():
    rows = [
        article(),
        article(title="600000 上涨", url="https://example.invalid/duplicate"),
        article(
            title="600000 立案调查",
            content="<b>600000 立案调查</b>",
            published="2026-09-09 09:00:00",
            url="https://example.invalid/risk",
        ),
        {
            **article(
                "999999",
                title="其他公司 上涨",
                url="https://example.invalid/unrelated",
                content="其他公司新闻",
            ),
            "stockCode": "999999",
        },
    ]
    client = FakeClient(
        {"600000": response(*rows)},
        community_payloads={"600000": community_response()},
    )

    result = fetch_context(["600000.SH"], client, datetime.fromisoformat("2026-09-10T16:00:00+08:00"))

    assert len(result["evidence"]) == 2
    assert all(item["ts_code"] == "600000.SH" for item in result["evidence"])
    assert all(item["quality"] == "aggregator_timestamp" for item in result["evidence"])
    assert all(
        item["available_at"] == item["fetched_at"] == "2026-09-10T12:00:00+08:00"
        for item in result["evidence"]
    )
    assert result["coverage"]["600000.SH"] == {"news": "available", "community": "missing"}
    assert result["evidence"][1]["risk_flags"] == ["立案调查"]
    assert not any("社区当前缺少" in warning for warning in result["warnings"])
    assert client.calls[0][0] == EASTMONEY_SEARCH_URL
    request = json.loads(client.calls[0][1]["param"])
    assert request["keyword"] == "600000"
    assert request["param"]["cmsArticleWebOld"]["pageSize"] == 100


def test_ordinary_bearish_news_is_not_a_major_event_veto():
    client = FakeClient({"600000": response(article(title="600000 今日下跌，股东减持"))})
    result = fetch_context(["600000.SH"], client, datetime.fromisoformat("2026-09-10T16:00:00+08:00"))
    assert result["evidence"][0]["sentiment"] < 0
    assert result["evidence"][0]["risk_flags"] == []


def test_fetch_context_accepts_real_date_field_and_versions_ids_by_fetch_time():
    row = article()
    row["date"] = "2026-09-10 16:58:59"
    row.pop("showTime")
    first = fetch_context(
        ["600000.SH"],
        FakeClient(
            {"600000": response(row)},
            community_payloads={"600000": community_response()},
            fetched_at="2026-09-10T17:00:00+08:00",
        ),
        datetime.fromisoformat("2026-09-10T23:00:00+08:00"),
    )
    second = fetch_context(
        ["600000.SH"],
        FakeClient(
            {"600000": response(row)},
            community_payloads={"600000": community_response()},
            fetched_at="2026-09-10T18:00:00+08:00",
        ),
        datetime.fromisoformat("2026-09-10T23:00:00+08:00"),
    )

    assert len(first["evidence"]) == len(second["evidence"]) == 1
    assert first["evidence"][0]["published_at"] == "2026-09-10T16:58:59+08:00"
    assert first["evidence"][0]["evidence_id"] != second["evidence"][0]["evidence_id"]


@pytest.mark.parametrize(
    "row",
    [
        article(published="2026-09-10 18:00:00"),
        article(published="2026-09-11 10:00:00"),
        article(published=None),
    ],
)
def test_fetch_context_rejects_future_or_missing_source_time(row):
    client = FakeClient({"600000": response(row)})

    result = fetch_context(["600000.SH"], client, datetime.fromisoformat("2026-09-10T16:00:00+08:00"))

    assert result["evidence"] == []
    assert result["coverage"]["600000.SH"]["news"] == "missing"
    assert any("发布时间" in warning for warning in result["warnings"])


def test_fetch_context_isolates_one_code_failure_and_keeps_other_codes():
    client = FakeClient(
        {"600000": response(article()), "000001": response(article("000001", "000001 增长"))},
        failures={"600000"},
    )

    result = fetch_context(
        ["600000.SH", "000001.SZ"],
        client,
        datetime.fromisoformat("2026-09-10T16:00:00+08:00"),
    )

    assert result["coverage"]["600000.SH"]["news"] == "missing"
    assert result["coverage"]["000001.SZ"]["news"] == "available"
    assert {item["ts_code"] for item in result["evidence"]} == {"000001.SZ"}
    assert any("600000.SH" in warning and "失败" in warning for warning in result["warnings"])


def test_community_evidence_uses_publish_time_fixed_url_and_never_risk_flags():
    row = community_row(title="600000 风险提示与上涨", published="2026-09-10 15:00:00")
    client = FakeClient(
        {"600000": response()},
        community_payloads={"600000": community_response(row)},
        fetched_at="2026-09-10T16:00:00+08:00",
    )

    result = fetch_context(["600000.SH"], client, datetime.fromisoformat("2026-09-10T16:30:00+08:00"))

    item = result["evidence"][0]
    assert item["kind"] == "community"
    assert item["quality"] == "community_timestamp"
    assert item["published_at"] == "2026-09-10T15:00:00+08:00"
    assert item["url"] == "https://guba.eastmoney.com/news,600000,1771542407.html"
    assert item["risk_flags"] == []
    assert "must-not-be-stored" not in item["content"]
    assert "192.0.2.1" not in item["content"]
    assert result["coverage"]["600000.SH"]["community"] == "insufficient"


def test_community_accepts_prefixed_stockbar_code_from_page_variant():
    row = community_row()
    row["stockbar_code"] = "SH600000"
    result = fetch_context(
        ["600000.SH"],
        FakeClient({}, community_payloads={"600000": community_response(row)}),
        datetime(2026, 9, 11, 16, 0, tzinfo=SHANGHAI),
    )
    assert result["evidence"][0]["ts_code"] == "600000.SH"


@pytest.mark.parametrize("value,reason", [
    ("zssh000001", "other_channel"), ("cfhpl", "other_channel"),
    ("SZ600000", "conflicting_code"), ("SH600000.SZ", "conflicting_code"),
    ("text600000text", "invalid_code"), ({"code": "600000"}, "invalid_code"),
    ("600001", "other_security"), (None, "missing_code"),
])
def test_community_mapping_rejects_ambiguous_and_cross_market_values(value, reason):
    from ashare_agent.context_feed import CommunityMappingError, _community_stock_code
    with pytest.raises(CommunityMappingError) as exc:
        _community_stock_code({"stockbar_code": value}, "600000.SH")
    assert exc.value.reason == reason


def test_community_alias_conflict_cannot_be_hidden_by_first_field():
    from ashare_agent.context_feed import CommunityMappingError, _community_stock_code
    with pytest.raises(CommunityMappingError, match="冲突"):
        _community_stock_code({"stockbar_code": "600000", "stockBarCode": "600001"}, "600000.SH")


def test_community_diagnostics_partition_rows_and_aggregate_warnings():
    rows = [community_row(code="600001", post_id=i) for i in range(1, 4)]
    rows += [community_row(), community_row(), community_row(post_id=10, published="2026-08-01 10:00:00")]
    client = FakeClient({}, community_payloads={"600000": community_response(*rows)})
    result = fetch_context(["600000.SH"], client, datetime(2026, 9, 10, 16, tzinfo=SHANGHAI))
    stats = result["community_diagnostics"]["600000.SH"]
    assert stats["examined"] == 6
    assert (stats["accepted"], stats["rejected"], stats["duplicate"], stats["outside_window"]) == (1, 3, 1, 1)
    assert stats["rejection_reasons"] == {"other_security": 3}
    assert len(result["warnings"]) == 1
    assert "共 3 次" in result["warnings"][0]
    assert sum(result["warning_counts"].values()) == 3
    assert result["coverage"]["600000.SH"]["community"] == "insufficient"


def test_community_rejects_old_future_mismatched_and_missing_timestamps():
    rows = [
        community_row(published="2026-09-01 10:00:00"),
        community_row(post_id=2, published="2026-09-10 18:00:00"),
        community_row(post_id=3, code="000001", published="2026-09-10 10:00:00"),
        {**community_row(post_id=4), "post_publish_time": None},
    ]
    client = FakeClient(
        {"600000": response()},
        community_payloads={"600000": community_response(*rows)},
        fetched_at="2026-09-10T16:00:00+08:00",
    )

    result = fetch_context(["600000.SH"], client, datetime.fromisoformat("2026-09-10T16:30:00+08:00"))

    assert not [item for item in result["evidence"] if item["kind"] == "community"]
    assert result["coverage"]["600000.SH"]["community"] == "missing"
    assert any("发布时间" in warning for warning in result["warnings"])
    assert any("代码不匹配" in warning for warning in result["warnings"])
    assert any("缺少" in warning for warning in result["warnings"])


def test_community_parser_does_not_eval_trailing_javascript():
    row = community_row()
    client = FakeClient(
        {"600000": response()},
        community_payloads={"600000": community_response(row, suffix="; throw new Error('no-eval');")},
        fetched_at="2026-09-10T12:00:00+08:00",
    )

    result = fetch_context(["600000.SH"], client, datetime.fromisoformat("2026-09-10T16:00:00+08:00"))

    assert [item["kind"] for item in result["evidence"]] == ["community"]


def test_fetch_context_validates_bound_and_timezone_inputs():
    client = FakeClient({})
    with pytest.raises(ValueError, match="最多 30"):
        fetch_context(["600000.SH"] * 31, client, datetime.fromisoformat("2026-09-10T16:00:00+08:00"))
    with pytest.raises(ValueError, match="标准"):
        fetch_context(["600000"], client, datetime.fromisoformat("2026-09-10T16:00:00+08:00"))
    with pytest.raises(ValueError, match="带时区"):
        fetch_context(["600000.SH"], client, datetime(2026, 9, 10, 16))
