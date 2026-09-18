"""Current research sources. Acquisition timestamps never pretend to be historic availability."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

SHANGHAI = ZoneInfo("Asia/Shanghai")
TENCENT = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
SINA_BASE = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center."


def now_iso():
    return datetime.now(SHANGHAI).isoformat()


def mainboard_code(value):
    code = str(value).strip().lower()
    if re.fullmatch(r"\d{6}\.(sh|sz)", code):
        code = code[-2:] + code[:6]
    if re.fullmatch(r"\d{6}", code):
        code = ("sh" if code.startswith("6") else "sz") + code
    if not re.fullmatch(r"(?:sh60[0135]\d{3}|sz00[0123]\d{3})", code):
        raise ValueError(f"不支持的沪深主板代码：{value}")
    return code


def public_code(code):
    return code[2:] + "." + code[:2].upper()


def parse_symbols(text):
    if not text or not text.strip():
        return []
    codes = [mainboard_code(x) for x in re.split(r"[,，\s]+", text.strip())]
    if len(codes) > 500:
        raise ValueError("自定义列表最多 500 只；完整数据源股票池请留空")
    return sorted(set(codes))


def risk_name(name):
    return not name or bool(re.search(r"ST|PT|退|摘牌", name.upper()))


def possible_limit_state(price, previous_close, name=""):
    """Return a conservative *possible* daily-limit state.

    This is an execution guard, not an exchange limit-price oracle.  The V1
    screen is restricted to the ordinary main board and excludes names that
    look like ST/PT/退市 securities, so a 10% band is a useful warning.  We
    deliberately say ``possible`` because IPO exceptions, corporate actions,
    special trading statuses and tick rounding require an authoritative
    limit-data feed before a fill can be claimed.
    """

    try:
        price, previous_close = float(price), float(previous_close)
    except (TypeError, ValueError):
        return "unknown"
    if not math.isfinite(price) or not math.isfinite(previous_close) or previous_close <= 0:
        return "unknown"
    if risk_name(name):
        return "unknown"
    change = price / previous_close - 1
    if change <= -0.095:
        return "possible_lower_limit"
    if change >= 0.095:
        return "possible_upper_limit"
    return "not_limit"


class CurrentClient:
    def __init__(self, root: Path, network="direct", ttl=1800, interval=0.3):
        if network not in {"direct", "environment"}:
            raise ValueError("Invalid network mode")
        self.root, self.network, self.ttl = Path(root), network, ttl
        self.interval = interval
        self.http = httpx.Client(
            timeout=12,
            trust_env=network == "environment",
            follow_redirects=False,
            headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"},
        )
        self.lock = threading.Lock()
        self.last_request = 0.0
        self.receipts = []

    def close(self):
        self.http.close()

    def get(self, url, params=None, refresh=False):
        params = params or {}
        identity = json.dumps({"url": url, "params": params}, sort_keys=True).encode()
        key = hashlib.sha256(identity).hexdigest()
        folder = self.root / key
        cached = sorted(folder.glob("*/manifest.json"), reverse=True)
        if cached and not refresh:
            meta = json.loads(cached[0].read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(meta["fetched_at"])
            age = (datetime.now(SHANGHAI) - fetched).total_seconds()
            if (
                0 <= age <= self.ttl
                and fetched.date() == datetime.now(SHANGHAI).date()
                and fetched.hour >= 16
            ):
                raw = (cached[0].parent / "response.bin").read_bytes()
                if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
                    raise ValueError("本地行情缓存哈希不一致")
                with self.lock:
                    self.receipts.append(meta)
                return raw
        error = None
        for attempt in range(2):
            with self.lock:
                time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
            try:
                response = self.http.get(url, params=params)
                response.raise_for_status()
                raw = response.content
                revision = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8]
                path = folder / revision
                path.mkdir(parents=True)
                meta = dict(
                    url=url,
                    params=params,
                    fetched_at=now_iso(),
                    available_at=now_iso(),
                    revision=revision,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    bytes=len(raw),
                    network=self.network,
                    status="http_received",
                    source_coverage_verified=False,
                )
                (path / "response.bin").write_bytes(raw)
                (path / "manifest.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                with self.lock:
                    self.receipts.append(meta)
                return raw
            except httpx.HTTPError as exc:
                error = exc
                if attempt == 0:
                    time.sleep(0.5)
        raise RuntimeError(f"行情网络请求失败（{self.network}）：{type(error).__name__}")

    def universe(self, progress=lambda _: None):
        count_url = SINA_BASE + "getHQNodeStockCount"

        def count(refresh=False):
            value = json.loads(self.get(count_url, {"node": "hs_a"}, refresh=refresh))
            total = int(value)
            if not 1000 <= total <= 15000:
                raise ValueError("证券列表总数异常")
            return total

        total = count()
        rows = []
        for page in range(1, math.ceil(total / 80) + 1):
            progress(f"读取证券目录 {page}/{math.ceil(total / 80)}")
            part = json.loads(
                self.get(
                    SINA_BASE + "getHQNodeData",
                    {
                        "page": page,
                        "num": 80,
                        "sort": "symbol",
                        "asc": 1,
                        "node": "hs_a",
                        "symbol": "",
                        "_s_r_a": "page",
                    },
                )
            )
            expected = min(80, total - (page - 1) * 80)
            if not isinstance(part, list) or len(part) != expected:
                raise ValueError("证券目录分页不完整")
            rows.extend(part)
        codes = [r["symbol"] for r in rows]
        if count(refresh=True) != total or len(set(codes)) != total or codes != sorted(codes):
            raise ValueError("证券目录重复、排序或首尾总数发生变化，请稍后重试")
        result = []
        for row in rows:
            try:
                code = mainboard_code(row["symbol"])
            except ValueError:
                continue
            if not row.get("name"):
                raise ValueError("证券目录缺少名称")
            result.append(dict(code=code, name=row["name"]))
        return result

    def quotes(self, codes):
        result = {}
        for offset in range(0, len(codes), 50):
            group = codes[offset : offset + 50]
            text = self.get("https://hq.sinajs.cn/list=" + ",".join(group)).decode("gb18030")
            for code, body in re.findall(r'var hq_str_([a-z]{2}\d{6})="([^"\r\n]*)";', text):
                parts = body.split(",")
                if code not in group or len(parts) < 32 or code in result:
                    continue
                try:
                    observed = datetime.fromisoformat(parts[30] + "T" + parts[31]).replace(tzinfo=SHANGHAI)
                    result[code] = dict(
                        code=code,
                        name=parts[0],
                        open=float(parts[1]),
                        close=float(parts[3]),
                        high=float(parts[4]),
                        low=float(parts[5]),
                        volume=float(parts[8]),
                        amount=float(parts[9]),
                        date=parts[30],
                        observed_at=observed.isoformat(),
                    )
                except ValueError:
                    continue
        return result

    def bars(self, code, adjust="", end=None):
        end = end or datetime.now(SHANGHAI).date().isoformat()
        start = (datetime.fromisoformat(end).date() - timedelta(days=1100)).isoformat()
        raw = self.get(
            TENCENT, {"_var": f"kline_day{adjust}", "param": f"{code},day,{start},{end},640,{adjust}"}
        )
        text = raw.decode("utf-8")
        pos = text.find("={")
        body = json.loads(text[pos + 1 :] if pos >= 0 else text)
        if not isinstance(body, dict) or body.get("code") != 0:
            raise ValueError("腾讯行情返回错误状态")
        securities = body.get("data")
        if not isinstance(securities, dict) or not isinstance(securities.get(code), dict):
            raise ValueError("腾讯行情证券节点格式异常")
        data = securities[code]
        key = "qfqday" if adjust == "qfq" else "day"
        rows = data.get(key)
        # Never substitute raw bars for a missing adjusted series.
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"缺少 {key} 行情")
        frame = normalize_bars(rows, code, end, adjust)
        quotes = data.get("qt")
        quote = quotes.get(code, []) if isinstance(quotes, dict) else []
        name = quote[1] if isinstance(quote, list) and len(quote) > 2 and isinstance(quote[1], str) else ""
        return frame, name


def normalize_bars(rows, code, end, adjust=""):
    result = []
    seen = set()
    previous = ""
    for row in rows:
        if not isinstance(row, list) or len(row) < 9:
            raise ValueError("行情缺少真实成交额字段，不能用量价估算")
        day = datetime.fromisoformat(row[0]).date().isoformat()
        if day in seen or day <= previous:
            raise ValueError("行情日期重复或非递增")
        seen.add(day)
        previous = day
        if day > end:
            raise ValueError("行情包含请求日之后的数据")
        op, close, hi, lo, volume = map(float, row[1:6])
        amount = float(row[8]) * 10000
        values = (op, close, hi, lo, volume, amount)
        if not all(math.isfinite(x) for x in values) or not 0 < lo <= min(op, close) <= max(op, close) <= hi:
            raise ValueError("OHLC 非法")
        if volume < 0 or amount < 0:
            raise ValueError("量额为负")
        # Supported MAIN stocks only. Index volumes are not used in stock sizing/ranking.
        shares = volume * 100 if code != "sh000300" else volume
        if not adjust and code != "sh000300" and shares > 0:
            if not lo * 0.98 <= amount / shares <= hi * 1.02:
                raise ValueError("量额单位或成交均价不一致")
        result.append(
            dict(trade_date=day, open=op, close=close, high=hi, low=lo, volume=shares, amount=amount)
        )
    return pd.DataFrame(result).set_index("trade_date", drop=False)
