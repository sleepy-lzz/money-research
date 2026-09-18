"""Bounded, evidence-first current news context.

This module is a context sidecar.  It never ranks securities, creates orders, or
turns keyword counts into trading facts.  The acquisition timestamp always comes
from the ``CurrentClient`` receipt produced by the actual request or cache read.
"""

from __future__ import annotations

import hashlib
import html.parser
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from .current_data import SHANGHAI, CurrentClient

EASTMONEY_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"
GUBA_URL = "https://guba.eastmoney.com/list,{code}.html"
MAX_CODES = 30
MAX_NEWS_PER_CODE = 10
MAX_COMMUNITY_ROWS = 40
NEWS_WINDOW = timedelta(days=7)

_POSITIVE_WORDS = (
    "上涨",
    "增长",
    "利好",
    "增持",
    "回购",
    "突破",
    "盈利",
    "预增",
    "中标",
    "签约",
    "创新高",
    "净利",
)
_NEGATIVE_WORDS = (
    "下跌",
    "风险",
    "减持",
    "亏损",
    "预亏",
    "暴雷",
    "违规",
    "处罚",
    "退市",
    "跌停",
    "诉讼",
    "减值",
    "警示",
    "监管",
    "问询",
)


class _PlainText(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _plain(value: Any) -> str:
    text = "" if value is None else str(value)
    parser = _PlainText()
    try:
        parser.feed(text)
        parser.close()
        text = " ".join(parser.parts)
    except (TypeError, ValueError):
        text = re.sub(r"<[^>]*>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _field(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _parse_source_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text or re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text):
            raise ValueError("新闻缺少绝对发布时间")
        text = text.replace("/", "-").replace("Z", "+00:00")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:[+-]\d{2}:\d{2})?", text):
            raise ValueError("新闻发布时间必须明确包含日期、小时和分钟")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("新闻发布时间格式无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _receipt_time(client: CurrentClient) -> datetime:
    receipts = getattr(client, "receipts", None)
    if not isinstance(receipts, list) or not receipts or not isinstance(receipts[-1], dict):
        raise ValueError("新闻请求缺少 CurrentClient 采集回执")
    value = receipts[-1].get("fetched_at")
    if not value:
        raise ValueError("新闻回执缺少真实 fetched_at")
    return _parse_source_time(value)


def _parse_jsonp(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise ValueError("新闻响应为空")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("新闻 JSONP 响应格式异常")
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("新闻 JSONP 响应格式异常") from exc


def _parse_guba(raw: bytes) -> list[dict[str, Any]]:
    """Parse only the JSON after the fixed article_list assignment.

    ``raw_decode`` deliberately leaves the rest of the page untouched; no
    JavaScript is executed or interpreted.
    """

    text = raw.decode("utf-8", errors="replace")
    marker = "var article_list="
    start = text.find(marker)
    if start < 0:
        raise ValueError("社区页面缺少 article_list")
    decoder = json.JSONDecoder()
    try:
        payload, _ = decoder.raw_decode(text[start + len(marker) :].lstrip())
    except json.JSONDecodeError as exc:
        raise ValueError("社区 article_list JSON 格式异常") from exc
    rows = payload.get("re") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("社区 article_list 缺少帖子列表")
    return [row for row in rows if isinstance(row, dict)]


def _rows(payload: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            marker = id(value)
            if marker in seen:
                return
            seen.add(marker)
            has_article_field = any(
                key in value
                for key in (
                    "title",
                    "Title",
                    "Art_Title",
                    "url",
                    "Art_Url",
                    "showTime",
                    "Art_ShowTime",
                )
            )
            if has_article_field:
                result.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return result


def _stock_matches(row: dict[str, Any], code: str, title: str, content: str) -> bool:
    digits = code[:6]
    for key in ("stockCode", "StockCode", "stock_code", "secCode", "code", "symbol"):
        value = row.get(key)
        if value is None:
            continue
        if digits in str(value) or code in str(value).upper():
            return True
    return digits in f"{title} {content}"


def _sentiment(title: str) -> tuple[float, list[str]]:
    positive = [word for word in _POSITIVE_WORDS if word in title]
    negative = [word for word in _NEGATIVE_WORDS if word in title]
    total = len(positive) + len(negative)
    score = (len(positive) - len(negative)) / total if total else 0.0
    # Ordinary bearish words affect the displayed sentiment, not the risk gate.
    # These matches are review prompts, never assertions that an event is confirmed.
    flags = [
        word
        for word in ("立案调查", "财务造假", "终止上市", "退市风险警示", "重大违法", "破产清算")
        if word in title
    ]
    return max(-1.0, min(1.0, score)), flags


def _request_params(code: str) -> dict[str, str]:
    param = {
        "uid": "",
        "keyword": code[:6],
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": 100,
                "preTag": "<em>",
                "postTag": "</em>",
            }
        },
    }
    return {
        "cb": "jQuery_context_feed",
        "param": json.dumps(param, ensure_ascii=False, separators=(",", ":")),
    }


def _evidence_id(published_at: str, url: str, content_hash: str, fetched_at: str, code: str) -> str:
    return hashlib.sha256(
        f"context-v2|{published_at}|{fetched_at}|{code}|{url}|{content_hash}".encode("utf-8")
    ).hexdigest()


def _article(row: dict[str, Any], code: str, cutoff: datetime, fetched_at: datetime) -> dict[str, Any] | None:
    title = _plain(_field(row, "title", "Title", "Art_Title", "artTitle"))
    content = _plain(_field(row, "content", "Content", "Art_Content", "summary", "digest"))
    if not content:
        content = title
    url = str(_field(row, "url", "Url", "Art_Url", "artUrl", "link") or "").strip()
    if not title or not content or not url:
        raise ValueError("新闻缺少标题、内容或 URL")
    published = _parse_source_time(
        _field(row, "published_at", "publishedAt", "showTime", "ShowTime", "Art_ShowTime", "date", "time")
    )
    if published > fetched_at:
        raise ValueError("新闻发布时间晚于本次采集时间")
    if published > cutoff:
        raise ValueError("新闻发布时间晚于截止时间")
    if published < cutoff - NEWS_WINDOW:
        return None
    if not _stock_matches(row, code, title, content):
        raise ValueError("新闻未能可靠映射到请求代码")
    title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
    content_hash = hashlib.sha256(f"{title}\n{content}".encode("utf-8")).hexdigest()
    sentiment, risk_flags = _sentiment(title)
    published_text = published.isoformat()
    return dict(
        evidence_id=_evidence_id(published_text, url, content_hash, fetched_at.isoformat(), code),
        ts_code=code,
        kind="news",
        title=title,
        url=url,
        published_at=published_text,
        available_at=fetched_at.isoformat(),
        fetched_at=fetched_at.isoformat(),
        content_hash=content_hash,
        content=content,
        quality="aggregator_timestamp",
        sentiment=sentiment,
        risk_flags=risk_flags,
        _title_hash=title_hash,
    )


class CommunityMappingError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _community_stock_code(row: dict[str, Any], code: str) -> None:
    values = [row[key] for key in ("stockbar_code", "stockBarCode", "stock_bar_code")
              if key in row and row[key] not in (None, "")]
    if not values:
        raise CommunityMappingError("missing_code", "社区帖子代码缺失，已丢弃")
    identities = set()
    for value in values:
        if not isinstance(value, str):
            raise CommunityMappingError("invalid_code", "社区代码格式异常，已丢弃")
        value = value.strip().upper()
        if value == "CFHPL" or value.startswith("ZS"):
            raise CommunityMappingError("other_channel", "社区非个股栏目，已丢弃")
        match = re.fullmatch(r"(?:(SH|SZ))?(\d{6})(?:\.(SH|SZ))?", value)
        if not match:
            raise CommunityMappingError("invalid_code", "社区代码格式异常，已丢弃")
        prefix, digits, suffix = match.groups()
        market = "SH" if digits.startswith("6") else "SZ" if digits.startswith(("0", "3")) else None
        if prefix and suffix and prefix != suffix:
            raise CommunityMappingError("conflicting_code", "社区代码字段冲突，已丢弃")
        if market and any(v and v != market for v in (prefix, suffix)):
            raise CommunityMappingError("conflicting_code", "社区代码市场冲突，已丢弃")
        identities.add((digits, prefix or suffix or market))
    if len(identities) != 1:
        raise CommunityMappingError("conflicting_code", "社区代码字段冲突，已丢弃")
    digits, market = identities.pop()
    if digits != code[:6] or market != code[-2:]:
        raise CommunityMappingError("other_security", "社区帖子代码不匹配（其他证券），已丢弃")


def _community_article(
    row: dict[str, Any], code: str, cutoff: datetime, fetched_at: datetime
) -> dict[str, Any] | None:
    _community_stock_code(row, code)
    post_id = str(_field(row, "post_id", "postId") or "").strip()
    if not re.fullmatch(r"\d+", post_id):
        raise ValueError("社区帖子缺少有效 post_id")
    title = _plain(_field(row, "post_title", "title", "Title"))
    summary = _plain(_field(row, "post_summary", "summary", "post_content", "content"))
    content = " ".join(part for part in (title, summary) if part).strip()
    if not title or not content:
        raise ValueError("社区帖子缺少标题或摘要")
    published = _parse_source_time(_field(row, "post_publish_time", "publish_time", "published_at"))
    if published > fetched_at:
        raise ValueError("社区发布时间晚于本次采集时间")
    if published > cutoff:
        raise ValueError("社区发布时间晚于截止时间")
    if published < cutoff - NEWS_WINDOW:
        return None
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    published_text = published.isoformat()
    sentiment, _ = _sentiment(title)
    return dict(
        evidence_id=_evidence_id(
            published_text,
            f"https://guba.eastmoney.com/news,{code[:6]},{post_id}.html",
            content_hash,
            fetched_at.isoformat(),
            code,
        ),
        ts_code=code,
        kind="community",
        title=title,
        url=f"https://guba.eastmoney.com/news,{code[:6]},{post_id}.html",
        published_at=published_text,
        available_at=fetched_at.isoformat(),
        fetched_at=fetched_at.isoformat(),
        content_hash=content_hash,
        content=content,
        quality="community_timestamp",
        sentiment=sentiment,
        risk_flags=[],
        _title_hash=hashlib.sha256(title.encode("utf-8")).hexdigest(),
    )


def _normalise_codes(codes: list[str]) -> list[str]:
    if not isinstance(codes, list) or len(codes) > MAX_CODES:
        raise ValueError("新闻代码列表最多 30 只")
    result = []
    seen = set()
    for value in codes:
        code = str(value).strip().upper()
        if not re.fullmatch(r"\d{6}\.(SH|SZ)", code):
            raise ValueError(f"代码必须是标准 600000.SH/000001.SZ 格式：{value}")
        if code not in seen:
            seen.add(code)
            result.append(code)
    return result


def fetch_context(codes: list[str], client: CurrentClient, cutoff: datetime) -> dict[str, Any]:
    """Fetch bounded, code-mapped news and community context per code."""

    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff 必须是带时区的 datetime")
    cutoff = cutoff.astimezone(SHANGHAI)
    normalised = _normalise_codes(codes)
    coverage = {code: {"news": "missing", "community": "missing"} for code in normalised}
    warnings: list[str] = []
    community_diagnostics = {}
    evidence: list[dict[str, Any]] = []
    seen_content: set[str] = set()
    seen_title: set[str] = set()

    for code in normalised:
        try:
            raw = client.get(EASTMONEY_SEARCH_URL, _request_params(code))
            fetched_at = _receipt_time(client)
            rows = _rows(_parse_jsonp(raw))
            accepted: list[dict[str, Any]] = []
            for row in rows:
                try:
                    if not _stock_matches(
                        row,
                        code,
                        _plain(_field(row, "title", "Title", "Art_Title", "artTitle")),
                        _plain(_field(row, "content", "Content", "Art_Content", "summary", "digest")),
                    ):
                        warnings.append(f"{code}：新闻未匹配代码，已丢弃")
                        continue
                    item = _article(row, code, cutoff, fetched_at)
                    if item is None:
                        continue
                    if item["content_hash"] in seen_content or item["_title_hash"] in seen_title:
                        continue
                    seen_content.add(item["content_hash"])
                    seen_title.add(item["_title_hash"])
                    accepted.append(item)
                except ValueError as exc:
                    warnings.append(f"{code}：{exc}")
            accepted.sort(key=lambda item: (item["published_at"], item["url"]), reverse=True)
            for item in accepted[:MAX_NEWS_PER_CODE]:
                item.pop("_title_hash", None)
                evidence.append(item)
            if accepted:
                coverage[code]["news"] = "available"
        except Exception as exc:
            warnings.append(f"{code}：新闻请求失败（{type(exc).__name__}）")

        stats = dict(examined=0, accepted=0, rejected=0, outside_window=0, duplicate=0,
                     rejection_reasons={}, request_status="not_started")
        community_diagnostics[code] = stats
        try:
            raw = client.get(GUBA_URL.format(code=code[:6]))
            fetched_at = _receipt_time(client)
            accepted: list[dict[str, Any]] = []
            stats["request_status"] = "parsing"
            for row in _parse_guba(raw)[:MAX_COMMUNITY_ROWS]:
                stats["examined"] += 1
                try:
                    item = _community_article(row, code, cutoff, fetched_at)
                    if item is None:
                        stats["outside_window"] += 1
                        continue
                    if item["content_hash"] in seen_content or item["_title_hash"] in seen_title:
                        stats["duplicate"] += 1
                        continue
                    seen_content.add(item["content_hash"])
                    seen_title.add(item["_title_hash"])
                    accepted.append(item)
                except ValueError as exc:
                    reason = exc.reason if isinstance(exc, CommunityMappingError) else "invalid_evidence"
                    stats["rejected"] += 1
                    stats["rejection_reasons"][reason] = stats["rejection_reasons"].get(reason, 0) + 1
                    warnings.append(f"{code}：{exc}")
            stats["accepted"] = len(accepted)
            stats["request_status"] = "complete"
            accepted.sort(key=lambda item: (item["published_at"], item["url"]), reverse=True)
            for item in accepted:
                item.pop("_title_hash", None)
                evidence.append(item)
            if accepted:
                coverage[code]["community"] = "available" if len(accepted) >= 10 else "insufficient"
        except Exception as exc:
            stats["request_status"] = "failed"
            warnings.append(f"{code}：社区请求失败（{type(exc).__name__}）")
    counts = Counter(warnings)
    return {"evidence": evidence,
            "warnings": [message if count == 1 else f"{message}（共 {count} 次）"
                         for message, count in counts.items()],
            "warning_counts": dict(counts), "community_diagnostics": community_diagnostics,
            "coverage": coverage}


__all__ = ["EASTMONEY_SEARCH_URL", "GUBA_URL", "fetch_context"]
