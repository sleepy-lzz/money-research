"""Current financial summaries with receipt provenance, never historical PIT inputs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .current_data import SHANGHAI, mainboard_code
from .market_context import _receipt

FINANCIAL_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
METRICS = {
    "TOTALOPERATEREVE": ("营业总收入", "元"),
    "PARENTNETPROFIT": ("归母净利润", "元"),
    "KCFJCXSYJLR": ("扣非归母净利润", "元"),
    "MGJYXJJE": ("每股经营现金流", "元/股"),
    "ROEJQ": ("加权净资产收益率", "%"),
    "ZCFZL": ("资产负债率", "%"),
    "TOTALOPERATEREVETZ": ("营业总收入同比", "%"),
    "PARENTNETPROFITTZ": ("归母净利润同比", "%"),
}


def _day(value):
    # Vendor returns midnight placeholders. Preserve day precision, never invent release time.
    if not isinstance(value, str):
        raise ValueError("缺少来源日期")
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    if parsed.strftime("%Y-%m-%d %H:%M:%S") != value:
        raise ValueError("来源日期格式无效")
    return parsed.date()


def _record(row, code, fetched, available, cutoff):
    if row.get("SECUCODE") != code or row.get("SECURITY_CODE") != code[:6]:
        raise ValueError("财报代码不匹配")
    if row.get("CURRENCY") != "CNY":
        raise ValueError("财报币种不是已支持的人民币")
    period = _day(row.get("REPORT_DATE"))
    announced = _day(row.get("NOTICE_DATE"))
    updated = _day(row.get("UPDATE_DATE"))
    if not period <= announced <= updated <= fetched.date() or not fetched <= available <= cutoff:
        raise ValueError("报告期、公告、更新或实际可见时间无效")
    if (period.month, period.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
        raise ValueError("未支持的报告期")
    if not row.get("REPORT_TYPE") or not row.get("ORG_TYPE"):
        raise ValueError("缺少报告或公司口径")
    metrics, missing = {}, []
    for key in METRICS:
        value = row.get(key)
        if value is None or value in {"", "-", "--"}:
            metrics[key] = None
            missing.append(key)
            continue
        try:
            value = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("财务字段不是数值") from exc
        if not value.is_finite():
            raise ValueError("财务字段不是有限数值")
        metrics[key] = str(value)
    if len(missing) == len(METRICS):
        raise ValueError("财务指标全部缺失")
    digest = hashlib.sha256(
        json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    identity = f"financial-v1|{code}|{period}|{digest}|{available.isoformat()}"
    return dict(
        evidence_id=hashlib.sha256(identity.encode()).hexdigest(),
        ts_code=code,
        name=row.get("SECURITY_NAME_ABBR"),
        report_period=period.isoformat(),
        report_type=row["REPORT_TYPE"],
        org_type=row["ORG_TYPE"],
        currency="CNY",
        source_announced_date=announced.isoformat(),
        source_updated_date=updated.isoformat(),
        published_at=None,
        publication_precision="day",
        fetched_at=fetched.isoformat(),
        available_at=available.isoformat(),
        content_hash=digest,
        quality="aggregator_unverified",
        trading_eligible=False,
        metrics=metrics,
        missing_fields=missing,
        url=f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index?type=web&code={code[-2:]}{code[:6]}",
        basis="来源按报告期口径；利润/现金流为年初至报告期累计，非单季、非TTM；比率保留供应方口径",
    )


def fetch_financial_context(codes, client, cutoff=None):
    codes = list(dict.fromkeys(codes))
    if len(codes) > 50:
        raise ValueError("财报参考最多50只")
    for code in codes:
        if len(code) != 9 or code != code.upper() or code[-3:] not in {".SH", ".SZ"}:
            raise ValueError("财报需要标准沪深代码")
        mainboard_code(code)
    if cutoff is not None and (cutoff.tzinfo is None or cutoff.utcoffset() is None):
        raise ValueError("cutoff 必须带时区")
    pending, coverage, evidence = [], {}, []
    for code in codes:
        coverage[code] = dict(status="missing", accepted=0)
        params = dict(
            reportName="RPT_F10_FINANCE_MAINFINADATA",
            columns="ALL",
            filter=f'(SECUCODE="{code}")',
            pageNumber="1",
            pageSize="8",
            sortColumns="REPORT_DATE",
            sortTypes="-1",
            source="HSF10",
            client="PC",
        )
        try:
            raw = client.get(FINANCIAL_URL, params, refresh=True)
            fetched, available = _receipt(client, FINANCIAL_URL, params)
            body = json.loads(raw, parse_float=Decimal)
            if body.get("success") is not True or body.get("code") != 0:
                raise ValueError("财报接口未返回成功状态")
            result = body.get("result")
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("data"), list)
                or len(result["data"]) > 8
            ):
                raise ValueError("财报响应结构无效")
            pending.append((code, fetched, available, result["data"]))
        except Exception as exc:
            coverage[code]["reason"] = f"财报获取或响应校验失败（{type(exc).__name__}）"
    cutoff = (cutoff or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    for code, fetched, available, rows in pending:
        entry = coverage[code]
        try:
            if not fetched <= available <= cutoff:
                raise ValueError("回执晚于截止时间")
            records = {}
            for row in rows:
                item = _record(row, code, fetched, available, cutoff)
                key = item["report_period"]
                if key in records and records[key]["content_hash"] != item["content_hash"]:
                    raise ValueError("同一报告期存在版本或口径冲突")
                records[key] = item
            selected = sorted(records.values(), key=lambda item: item["report_period"], reverse=True)[:4]
            evidence.extend(selected)
            entry.update(
                status="sample_available" if selected else "no_reports",
                accepted=len(selected),
                fetched_at=fetched.isoformat(),
                available_at=available.isoformat(),
            )
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            entry.update(status="unverified", reason=str(exc))
    return dict(
        schema_version=1,
        as_of=cutoff.isoformat(),
        evidence=evidence,
        coverage=coverage,
        metric_definitions={key: dict(label=v[0], unit=v[1]) for key, v in METRICS.items()},
        scope="每只股票最多展示4期可校验财务摘要，非完整三大报表；缺失不表示财务健康",
        decision_mode="仅参考；未核对原始公告/审计意见，不参与选股权重、仓位或历史回测",
        warnings=[
            "公告和更新日期只有日精度；可见时间采用本次真实获取回执，不能将报告期或公告日冒充历史可用时刻。",
            "银行、保险和一般企业口径不同，不跨行业套用统一财务阈值；同比增长不等于超出市场预期。",
        ],
    )
