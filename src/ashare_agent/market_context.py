"""Prospective macro news samples, isolated from ranking and order mathematics."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .context_feed import (
    EASTMONEY_SEARCH_URL,
    _field,
    _parse_jsonp,
    _parse_source_time,
    _plain,
    _request_params,
    _rows,
)
from .current_data import SHANGHAI

FED_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
TOPICS = {
    "cn_policy": dict(
        label="国内货币与财政政策",
        query="货币政策",
        terms=("央行", "货币政策", "降准", "降息", "财政政策"),
        channels="融资成本、信用和内需",
        positive="若宽松措施实际落地且信用传导有效，可能支持融资与需求。",
        negative="若政策收紧或传导弱于预期，融资敏感行业可能承压。",
    ),
    "cn_economy": dict(
        label="国内经济与产业",
        query="中国经济",
        terms=("中国经济", "国家统计局", "工业增加值", "社会消费品", "PMI", "产业政策"),
        channels="消费、工业订单和企业盈利",
        positive="若需求与订单改善且超出预期，相关企业盈利可能受益。",
        negative="若需求走弱或政策支持不及预期，相关行业盈利可能承压。",
    ),
    "global_rates": dict(
        label="海外利率与汇率",
        query="美联储",
        terms=("美联储", "美元", "美国通胀", "非农", "Federal Reserve", "FOMC"),
        channels="全球融资、汇率与风险偏好",
        positive="若融资条件改善且并非由衰退恶化驱动，可能支持风险偏好。",
        negative="若融资收紧或衰退风险上升，外需和风险资产可能承压。",
    ),
    "trade": dict(
        label="国际贸易与技术限制",
        query="关税",
        terms=("关税", "出口管制", "制裁", "贸易限制"),
        channels="出口、进口成本和供应链替代",
        positive="若限制放松，相关贸易链可能受益；替代需求也可能利好部分本土供应商。",
        negative="若关税或限制加重，受影响企业的订单、采购成本可能承压。",
    ),
    "energy": dict(
        label="能源与地缘风险",
        query="原油",
        terms=("原油", "石油", "天然气", "地缘", "中东", "航运"),
        channels="能源上游、航空、化工和运输成本",
        positive="若能源价格上涨，上游可能受益；若下降，部分下游成本可能改善。",
        negative="若能源或运输成本上涨，部分下游利润可能受压；上游也有价格下跌风险。",
    ),
}


def _url(value):
    value = str(value or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("来源链接无效")
    return value


def _receipt(client, url, params):
    if not client.receipts:
        raise ValueError("缺少请求回执")
    receipt = client.receipts[-1]
    if receipt.get("url") != url or receipt.get("params", {}) != (params or {}):
        raise ValueError("回执与当前请求不匹配")
    stamps = []
    for key in ("fetched_at", "available_at"):
        stamp = datetime.fromisoformat(receipt[key])
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("回执时间缺少时区")
        stamps.append(stamp.astimezone(SHANGHAI))
    fetched, available = stamps
    if available < fetched:
        raise ValueError("回执时间倒置")
    return fetched, available


def _rss_rows(raw):
    if len(raw) > 2_000_000 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("RSS 格式或大小不受支持")
    root = ElementTree.fromstring(raw)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("RSS 缺少频道")
    return [
        dict(
            title=item.findtext("title"),
            content=item.findtext("description"),
            url=item.findtext("link"),
            published_at=item.findtext("pubDate"),
        )
        for item in root.findall("./channel/item")[:100]
    ]


def _article(row, topic, source, fetched, available, cutoff):
    title = _plain(_field(row, "title", "Title", "Art_Title", "artTitle"))
    content = _plain(_field(row, "content", "Content", "Art_Content", "summary", "digest")) or title
    url = _url(_field(row, "url", "Url", "Art_Url", "artUrl", "link"))
    if not title or (
        source != "fed"
        and not any(term.casefold() in f"{title} {content}".casefold() for term in TOPICS[topic]["terms"])
    ):
        raise ValueError("材料未匹配主题")
    value = _field(row, "published_at", "publishedAt", "showTime", "ShowTime", "Art_ShowTime", "date", "time")
    if source == "fed":
        published = parsedate_to_datetime(value)
        if published.tzinfo is None or published.utcoffset() is None:
            raise ValueError("海外发布时间缺少时区")
        if urlsplit(url).hostname != "www.federalreserve.gov":
            raise ValueError("官方 RSS 链接域名不匹配")
    else:
        published = _parse_source_time(value)
    published = published.astimezone(SHANGHAI)
    if not published <= fetched <= available <= cutoff:
        raise ValueError("发布时间/实际可见时间不在截止时点内")
    if published < cutoff - timedelta(days=7):
        return None
    digest = hashlib.sha256(f"{title}\n{content}".encode()).hexdigest()
    identity = f"macro-v1|{source}|{url}|{published.isoformat()}|{available.isoformat()}|{digest}"
    return dict(
        evidence_id=hashlib.sha256(identity.encode()).hexdigest(),
        kind="macro",
        topics=[topic],
        title=title,
        content=content,
        url=url,
        content_hash=digest,
        published_at=published.isoformat(),
        fetched_at=fetched.isoformat(),
        available_at=available.isoformat(),
        source=source,
        source_label="美联储官方 RSS" if source == "fed" else "东方财富聚合检索",
        quality="official_feed_timestamp" if source == "fed" else "aggregator_timestamp",
        verification="来源公告摘要；未核验全文及实际影响"
        if source == "fed"
        else "聚合报道；原始出处及事实待核实",
        direction="未判定",
        impact_status="条件情景，未确认发生",
        trading_eligible=False,
    )


def fetch_market_context(client, cutoff=None):
    """Fetch each fixed source once; freeze live cutoff AFTER acquiring receipts.

    An explicit cutoff is supported for replay tests: future receipts are rejected.
    No external timestamp or provider classification may enable historical trading.
    """
    if cutoff is not None and (cutoff.tzinfo is None or cutoff.utcoffset() is None):
        raise ValueError("cutoff 必须带时区")
    pending, coverage, warnings = [], {}, []
    specs = [
        (key, "eastmoney", EASTMONEY_SEARCH_URL, _request_params(spec["query"]))
        for key, spec in TOPICS.items()
    ]
    specs.append(("global_rates", "fed", FED_URL, {}))
    for topic, source, url, params in specs:
        key = f"{source}:{topic}"
        coverage[key] = dict(status="missing", accepted=0, rejected=0, topic=topic, source=source)
        try:
            raw = client.get(url, params, refresh=True)
            fetched, available = _receipt(client, url, params)
            if source == "fed":
                rows = _rss_rows(raw)
            else:
                payload = _parse_jsonp(raw)
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("result"), dict)
                    or not isinstance(payload["result"].get("cmsArticleWebOld"), list)
                ):
                    raise ValueError("主题检索响应结构异常")
                rows = _rows(payload)[:100]
            pending.append((key, topic, source, fetched, available, rows))
        except Exception as exc:
            coverage[key]["error"] = type(exc).__name__
            warnings.append(f"{TOPICS[topic]['label']} / {source} 获取失败（{type(exc).__name__}）")
    cutoff = (cutoff or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    evidence = {}
    for key, topic, source, fetched, available, rows in pending:
        entry = coverage[key]
        if not fetched <= available <= cutoff:
            entry.update(status="invalid_time", rejected=len(rows))
            warnings.append(f"{key} 回执晚于截止时点，已拒绝")
            continue
        entry.update(
            status="no_recent_evidence", fetched_at=fetched.isoformat(), available_at=available.isoformat()
        )
        accepted = []
        for row in rows:
            try:
                item = _article(row, topic, source, fetched, available, cutoff)
                if item is not None:
                    accepted.append(item)
            except (ValueError, TypeError, OverflowError):
                entry["rejected"] += 1
        accepted.sort(key=lambda item: item["published_at"], reverse=True)
        unique = {}
        for item in accepted:
            unique.setdefault((item["url"], item["content_hash"]), item)
        for item in list(unique.values())[:5]:
            # Repeated query hits merge topics; they do not add independent votes.
            identity = (item["source"], item["url"], item["published_at"], item["content_hash"])
            if identity in evidence:
                previous = evidence[identity]
                previous["topics"] = sorted(set(previous["topics"] + [topic]))
            else:
                evidence[identity] = item
            entry["accepted"] += 1
        if entry["accepted"]:
            entry["status"] = "sample_available"
        elif entry["rejected"]:
            entry["status"] = "unverified"
    return dict(
        schema_version=1,
        as_of=cutoff.isoformat(),
        status="sample_only",
        scope="近7日有限来源样本，不代表全市场新闻覆盖",
        decision_mode="参考，不参与排名、股数、止损或历史回测",
        evidence=sorted(evidence.values(), key=lambda e: e["published_at"], reverse=True),
        coverage=coverage,
        scenarios={
            k: {f: v[f] for f in ("label", "channels", "positive", "negative")} for k, v in TOPICS.items()
        },
        warnings=warnings,
    )
