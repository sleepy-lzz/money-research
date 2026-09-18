"""Forward-created research lists, intentionally separate from historic simulation Snapshots."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

from .current_data import (
    SHANGHAI,
    CurrentClient,
    now_iso,
    parse_symbols,
    possible_limit_state,
    public_code,
    risk_name,
)
from .session_calendar import calendar_manifest, next_sessions, sessions


def screen_rule_hash(calendar=None):
    calendar = calendar_manifest() if calendar is None else calendar
    digest = hashlib.sha256()
    for name in ("current_screen.py", "current_data.py", "session_calendar.py"):
        digest.update(Path(__file__).with_name(name).read_bytes())
    digest.update(json.dumps(calendar, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()


def validate_selection_inputs(selection, benchmark, cutoff):
    """Do not re-label a cached pre-hardening screen as a current validated plan."""
    calendar = selection.get("calendar_manifest")
    if not isinstance(calendar, dict) or selection.get("source_hash") != screen_rule_hash(calendar):
        raise ValueError("选股缓存规则/日历身份已失效，请重新选股")
    current_calendar = calendar_manifest()
    for field in ("schema", "revision", "covered_start", "covered_end", "rules"):
        if calendar.get(field) != current_calendar.get(field):
            raise ValueError("选股缓存使用旧交易日历，请重新选股")
    parameters = selection.get("parameters", {})
    if not isinstance(parameters, dict) or not {"top", "min_amount", "momentum", "trend", "symbols", "universe_policy"} <= parameters.keys():
        raise ValueError("选股缓存缺少完整参数身份，请重新选股")
    if selection.get("synthetic") is not False or selection.get("as_of") != cutoff.astimezone(SHANGHAI).date().isoformat():
        raise ValueError("选股缓存不是今日真实研究")
    expected_next = next_sessions(selection["as_of"], 1, calendar)[0]
    if selection.get("signal_policy", "next_session_only") != "next_session_only":
        raise ValueError("选股缓存的信号有效期策略无法核验")
    if selection.get("signal_valid_for", expected_next) != expected_next:
        raise ValueError("选股缓存的信号有效日与独立交易日历不一致")
    for field in ("started_at", "completed_at"):
        stamp = datetime.fromisoformat(str(selection.get(field, "")))
        if stamp.tzinfo is None or stamp > cutoff or stamp.astimezone(SHANGHAI).date().isoformat() != selection["as_of"]:
            raise ValueError("选股缓存生成时间不可核验或晚于复核时点")
    receipts = selection.get("receipts")
    if not isinstance(receipts, list) or not receipts:
        raise ValueError("选股缓存缺少来源 receipts")
    for receipt in receipts:
        for field in ("available_at", "fetched_at"):
            stamp = datetime.fromisoformat(str(receipt.get(field, "")))
            if stamp.tzinfo is None or stamp > cutoff:
                raise ValueError("选股缓存来源时间不可核验或晚于复核时点")
    for source in calendar["sources"]:
        fetched = datetime.fromisoformat(source["fetched_at"])
        if fetched.tzinfo is None or fetched > cutoff or source["published_at"] > selection["as_of"]:
            raise ValueError("选股缓存交易日历在研究时点尚不可见")
    expected = sessions(calendar["covered_start"], selection["as_of"], calendar)[-251:]
    if len(expected) != 251 or expected[-1] != selection["as_of"] or benchmark.index[-251:].tolist() != expected:
        raise ValueError("选股缓存基准缺失独立交易日历窗口，请重新选股")
    if benchmark.index.duplicated().any() or not benchmark.index.is_monotonic_increasing:
        raise ValueError("选股缓存基准日期重复或无序")
    if not pd.to_numeric(benchmark.loc[expected, "close"], errors="coerce").map(lambda v: math.isfinite(v) and v > 0).all():
        raise ValueError("选股缓存基准价格无效")
    return expected


def clean_name(name):
    return re.sub(r"^(XD|XR|DR)", "", str(name).strip().upper())


def verify_current(quote, raw, as_of, tencent_name, cutoff):
    """Data validity precedes every strategy exclusion, including low liquidity and risk names."""
    if not quote or not quote.get("name") or not tencent_name:
        raise ValueError("缺少可核对的当前名称或行情")
    if clean_name(quote["name"]) != clean_name(tencent_name):
        raise ValueError("两个来源的股票名称不一致")
    observed = datetime.fromisoformat(quote["observed_at"])
    if observed.tzinfo is None or observed > cutoff or quote["date"] != as_of or observed.hour < 15:
        raise ValueError("交叉行情日期/时间不符或尚未收盘")
    if raw.index.max() != as_of:
        raise ValueError("价格序列未更新至本次基准日期")
    last = raw.iloc[-1]
    for field in ("close", "open", "high", "low", "volume", "amount"):
        actual, other = float(last[field]), float(quote[field])
        tolerance = 0.011 if field in {"close", "open", "high", "low"} else max(100, abs(other) * 0.005)
        if not math.isfinite(other) or other <= 0 or abs(actual - other) > tolerance:
            raise ValueError(f"当日 {field} 与交叉来源不一致")


def qualify(code, quote, raw, adjusted, expected_days, tencent_name, min_amount, cutoff):
    """Common data/eligibility pool: deliberately NO trend or momentum-sign filter."""
    if len(expected_days) < 251:
        raise ValueError("基准预热不足 251 个观测交易日")
    as_of = expected_days[-1]
    verify_current(quote, raw, as_of, tencent_name, cutoff)
    if risk_name(quote["name"]) or risk_name(tencent_name):
        return None, "风险警示或退市名称，排除"
    if adjusted.index.max() != as_of:
        raise ValueError("价格序列未更新至本次基准日期")
    if set(expected_days) - set(raw.index) or set(expected_days) - set(adjusted.index):
        raise ValueError("最近 251 个基准交易日有缺失，不能跳过停牌或缺行情日")
    if raw.index.duplicated().any() or adjusted.index.duplicated().any():
        raise ValueError("历史交易日重复")
    raw, adjusted = raw.loc[expected_days], adjusted.loc[expected_days]
    if (raw.volume <= 0).any() or (raw.amount <= 0).any():
        return None, "预热区间存在无成交日，按保守规则排除"
    last = raw.iloc[-1]
    closes = adjusted.close.astype(float)
    if not closes.map(lambda value: math.isfinite(value) and value > 0).all():
        raise ValueError("前复权价格必须为有限正数")
    ratio = float(closes.iloc[-1] / last.close)
    if not 0.98 <= ratio <= 1.02:
        raise ValueError("前复权最新价格与原始价格基准不一致")
    amount20 = float(raw.amount.iloc[-20:].mean())
    if amount20 < min_amount:
        return None, "近 20 日平均成交额低于门槛"
    ma = float(closes.iloc[-120:].mean())
    m60, m120 = float(closes.iloc[-1] / closes.iloc[-61] - 1), float(closes.iloc[-1] / closes.iloc[-121] - 1)
    # These are deliberately descriptive observations.  They do not alter the
    # mechanical rank in V1, but make the known momentum-reversal risk visible
    # before a human considers an entry.
    m20 = float(closes.iloc[-1] / closes.iloc[-21] - 1)
    rolling_high20 = float(closes.iloc[-20:].max())
    drawdown20 = float(closes.iloc[-1] / rolling_high20 - 1) if rolling_high20 else 0.0
    ma_gap = float(closes.iloc[-1] / ma - 1) if ma else 0.0
    previous_close = float(raw.close.iloc[-2]) if len(raw) >= 2 else None
    limit_state = possible_limit_state(float(last.close), previous_close, quote["name"])
    risk_flags = []
    if m20 >= 0.20:
        risk_flags.append("短期20日涨幅≥20%，存在追高/反转风险")
    if ma_gap >= 0.35:
        risk_flags.append("前复权收盘价高于MA120超过35%，趋势偏离较大")
    if drawdown20 <= -0.08:
        risk_flags.append("近20日高点回撤≥8%，动量出现回撤")
    if limit_state == "possible_lower_limit":
        risk_flags.append("收盘价可能接近10%跌停，成交可行性未证明")
    elif limit_state == "possible_upper_limit":
        risk_flags.append("收盘价可能接近10%涨停，不应追价")
    return dict(
        ts_code=public_code(code),
        name=quote["name"],
        close=float(last.close),
        previous_close=previous_close,
        momentum60=m60,
        momentum120=m120,
        momentum20=m20,
        drawdown20=drawdown20,
        amount20=amount20,
        ma120_adjusted=ma,
        ma120_gap=ma_gap,
        limit_state=limit_state,
        risk_flags=risk_flags,
        risk_level="elevated" if risk_flags else "ordinary",
        observed_sessions=len(expected_days),
        eligibility_status="current_name_and_observed_history_verified",
        regulatory_status="not_independently_verified",
        industry=None, market_cap=None, industry_as_of=None, market_cap_as_of=None,
        exposure_source="unknown_no_historical_backfill",
        reason="通过当前名称/双源行情校验、251 日连续观测与量额过滤；尚未应用趋势条件",
    ), None


def trend_reason(row):
    """Original joint trend baseline; changes here require a new research protocol."""
    if row["ma120_gap"] <= 0:
        return "前复权收盘价未高于 MA120"
    if row["momentum60"] <= 0 or row["momentum120"] <= 0:
        return "60/120 日动量未同时为正"
    return None


def assess(code, quote, raw, adjusted, expected_days, tencent_name, min_amount, cutoff):
    """Compatibility entry: retain the original mechanical acceptance criteria."""
    row, reason = qualify(code, quote, raw, adjusted, expected_days, tencent_name, min_amount, cutoff)
    if row is None:
        return None, reason
    reason = trend_reason(row)
    if reason:
        return None, reason
    row["reason"] = "通过当前名称/双源行情校验、251 日连续观测、量额与趋势过滤；风险观察不改变机械排名"
    return row, None


def freeze_exposures(row, adjusted, benchmark, expected_days):
    """Contemporaneous diagnostics only; never use future prices/current industry backfills."""
    stock = adjusted.loc[expected_days, "close"].astype(float).pct_change().iloc[-120:]
    market = benchmark.loc[expected_days, "close"].astype(float).pct_change().iloc[-120:]
    variance = float(market.var(ddof=1))
    row["market_beta120"] = float(stock.cov(market) / variance) if variance > 0 else None
    row["daily_volatility60"] = float(stock.iloc[-60:].std(ddof=1))
    row["exposures_as_of"] = expected_days[-1]
    row["market_beta_source"] = "frozen_trailing120_close_returns_vs_HS300"
    return row


def rank_rows(rows, top):
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    frame["score"] = (frame.momentum60.rank(pct=True) + frame.momentum120.rank(pct=True)) / 2
    frame = frame.sort_values(["score", "ts_code"], ascending=[False, True]).head(top).copy()
    frame["rank"] = range(1, len(frame) + 1)
    return json.loads(frame.to_json(orient="records"))


def write_result(output, result):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    body = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    rows = result.get("candidates", [])
    with (output / "candidates.csv.tmp").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "rank", "ts_code", "name", "close", "momentum20", "momentum60",
            "momentum120", "ma120_gap", "drawdown20", "limit_state", "risk_level",
            "risk_flags", "amount20", "reason",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v
                    for k, v in row.items()
                }
            )

    def escape(x):
        return html.escape(str(x))

    cards = "".join(
        f'<div class="card"><span>{label}</span><strong>{escape(result.get(key, "—"))}</strong></div>'
        for label, key in [
            ("数据日期", "as_of"),
            ("范围股票", "universe_count"),
            ("完成检查", "attempted_count"),
            ("通过过滤", "valid_count"),
        ]
    )
    lines = "".join(
        f"<tr><td>{r['rank']}</td><td>{escape(r['name'])}<small>{escape(r['ts_code'])}</small></td>"
        f"<td>{r['close']:.2f}</td><td>{r.get('momentum20', 0):.2%}</td>"
        f"<td>{r['momentum60']:.2%}</td><td>{r['momentum120']:.2%}</td>"
        f"<td>{r.get('ma120_gap', 0):.2%}</td><td>{escape('；'.join(r.get('risk_flags', [])) or '未见预设风险观察')}</td>"
        f"<td>{r['amount20'] / 1e8:.2f} 亿</td></tr>"
        for r in rows
    )
    warnings = "".join(f"<li>{escape(x)}</li>" for x in result.get("warnings", []))
    exclusions = "".join(
        f"<tr><td>{escape(r['ts_code'])}</td><td>{escape(r['reason'])}</td></tr>"
        for r in result.get("exclusions", [])
    )
    status = {
        "complete": "请求范围检查完成",
        "partial": "部分数据缺失 · 仅供研究",
        "blocked": "数据未就绪",
    }.get(result["status"], result["status"])
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日选股研究报告</title><style>body{{font:15px/1.7 system-ui,"Microsoft YaHei";background:#f3f6fa;color:#172b43;margin:0}}main{{max-width:1100px;margin:50px auto;padding:24px}}h1{{font-size:32px}}small,span{{display:block;color:#65758a}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}}.card,section{{background:white;padding:24px;border:1px solid #dfe7ee;border-radius:14px;margin:16px 0}}strong{{font-size:23px}}table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:14px;border-bottom:1px solid #edf0f3}}.badge{{color:#087f83}}@media(max-width:700px){{.cards{{grid-template-columns:repeat(2,1fr)}}section{{overflow:auto;padding:12px}}}}</style>
<main><span>CURRENT RESEARCH / 当前研究</span><h1>每日选股研究报告</h1><p class="badge">{escape(status)}</p>
<p>生成时间：{escape(result.get("generated_at"))} · 范围：{escape(result.get("scope"))}</p>{'<div class="cards">' + cards + "</div>"}
<section><h2>候选清单</h2><p>这是观察清单，不是买入指令；信号只允许用于下一交易日的人工复核。风险观察不改变机械排名，但出现提示时不得把排名理解为低风险推荐。</p><p>信号有效日：{escape(result.get("signal_valid_for", "未知"))}；超过该日必须重新获取收盘数据。</p><table><thead><tr><th>排名</th><th>股票</th><th>原始收盘</th><th>20 日动量</th><th>60 日动量</th><th>120 日动量</th><th>距 MA120</th><th>风险观察</th><th>20 日均额</th></tr></thead><tbody>{lines or '<tr><td colspan="9">本次没有可展示的候选，请查看数据说明和排除原因。</td></tr>'}</tbody></table></section>
<section><h2>数据与方法说明</h2><ul>{warnings}</ul><p>当前获取的历史价格用于当前研究，未认证历史 PIT；不是原机械回测的等价收益验证。名称过滤不能完全替代交易所风险状态档案。</p></section>
<section><h2>排除原因</h2><table>{exclusions or "<tr><td>无</td></tr>"}</table></section></main></html>"""
    (output / "report.html.tmp").write_text(page, encoding="utf-8")
    (output / "result.json.tmp").write_text(body, encoding="utf-8")
    for name in ("candidates.csv", "report.html", "result.json"):
        (output / (name + ".tmp")).replace(output / name)
    return result


def screen(
    output,
    network="direct",
    symbols="",
    top=20,
    min_amount=100_000_000,
    progress=lambda _: None,
    client=None,
    cutoff=None,
):
    if not 1 <= top <= 100 or not math.isfinite(min_amount) or not 0 <= min_amount <= 1e12:
        raise ValueError("候选数量需 1–100，成交额门槛需为有效非负金额")
    codes = parse_symbols(symbols)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "result.json").exists():
        raise ValueError("结果目录已存在，请使用新目录以保留原研究")
    cutoff = cutoff or datetime.now(SHANGHAI)
    if cutoff.tzinfo is None:
        raise ValueError("研究时间必须带时区")
    owned = client is None
    source_hash = hashlib.sha256(
        Path(__file__).read_bytes() + Path(__file__).with_name("current_data.py").read_bytes()
    ).hexdigest()
    client = client or CurrentClient(Path("runtime/current-cache"), network)
    result = dict(
        mode="CURRENT_RESEARCH",
        status="blocked",
        generated_at=cutoff.isoformat(),
        scope="自定义股票列表" if codes else "新浪返回的沪深主板目录（未认证交易所全集）",
        network=network,
        synthetic=False,
        universe_count=0,
        attempted_count=0,
        valid_count=0,
        candidates=[],
        exclusions=[],
        warnings=[
            "当前研究，不是历史 PIT 回测；不生成订单。",
            "采用最近 251 个基准交易日连续有成交的保守过滤，不等同于交易所上市年龄字段。",
            "未提供行业仓位限制；本模式没有仓位或组合建议。",
            "风险警示基于两个来源的当前名称；不保证覆盖所有未体现在名称中的风险状态。",
            "机械基线按 60/120 日动量排名；短期过热、回撤和可能涨跌停只作为风险观察，不改变排名。",
            "候选信号只允许用于下一交易日的人工复核；过期后必须重新获取收盘数据。",
        ],
        parameters=dict(
            top=top, min_amount=min_amount, momentum=[60, 120], trend=120,
            symbols=sorted(codes) if codes else None,
            universe_policy="explicit_symbols" if codes else "sina_current_mainboard_directory",
        ),
    )
    try:
        calendar = calendar_manifest()
        for source in calendar["sources"]:
            fetched = datetime.fromisoformat(source["fetched_at"])
            if fetched.tzinfo is None or fetched > cutoff:
                raise ValueError("交易日历实际获取时间晚于本次研究时间，不能补造历史可见性")
            if source["published_at"] > cutoff.astimezone(SHANGHAI).date().isoformat():
                raise ValueError("交易日历包含研究时点尚未发布的来源")
        result["calendar_manifest"] = calendar
        source_hash = screen_rule_hash(calendar)
        if cutoff.astimezone(SHANGHAI).hour < 16:
            raise ValueError("当前研究在北京时间 16:00 后运行，避免未完成的当日日线")
        progress("核对基准日期与最近交易日观测")
        benchmark, _ = client.bars("sh000300", end=cutoff.date().isoformat())
        as_of = benchmark.index.max()
        result["as_of"] = as_of
        trailing = benchmark.loc[benchmark.index <= as_of, "close"].astype(float)
        ma120 = float(trailing.iloc[-120:].mean()) if len(trailing) >= 120 else None
        result["benchmark_diagnostics"] = dict(
            ts_code="000300.SH", as_of=as_of, definition="close_above_trailing_MA120_at_freeze",
            regime=("above_ma120" if trailing.iloc[-1] > ma120 else "not_above_ma120") if ma120 else "unknown",
            close=float(trailing.iloc[-1]), ma120=ma120,
            use="diagnostic_only_no_position_or_stop_changes",
        )
        result["signal_policy"] = "next_session_only"
        result["signal_valid_for"] = next_sessions(as_of, 1, calendar)[0]
        result["signal_valid_until"] = result["signal_valid_for"] + "T15:00:00+08:00"
        if as_of != cutoff.astimezone(SHANGHAI).date().isoformat():
            raise ValueError(f"最后可观测基准日期为 {as_of}，不能确认今天是否完整；本次不生成当前候选")
        expected = sessions(calendar["covered_start"], as_of, calendar)[-251:]
        if len(expected) < 251:
            raise ValueError("独立交易日历覆盖的预热交易日不足 251 日")
        if expected[-1] != as_of or benchmark.index[-251:].tolist() != expected:
            raise ValueError("基准日期与独立交易日历不一致：缺失/额外交易日，不能顺延预热窗口")
        (output / "calendar.json").write_text(json.dumps(calendar, ensure_ascii=False, indent=2), encoding="utf-8")
        benchmark.to_parquet(output / "benchmark.parquet", index=False)
        if not codes:
            universe = client.universe(progress)
            codes = [r["code"] for r in universe]
        result["universe_count"] = len(codes)
        (output / "universe.json").write_text(json.dumps(codes), encoding="utf-8")
        if not codes:
            raise ValueError("没有可检查的沪深主板股票")
        progress(f"范围已冻结：{len(codes)} 只，读取交叉行情")
        quotes = client.quotes(codes)
        (output / "quotes.json").write_text(json.dumps(quotes, ensure_ascii=False), encoding="utf-8")
        candidates, common_rows, data_failures = [], [], 0
        history_dir = output / "history"
        history_dir.mkdir(exist_ok=True)

        def process(code):
            quote = quotes.get(code)
            try:
                if not quote:
                    raise ValueError("缺少新浪当日报价")
                raw, name = client.bars(code, end=as_of)
                verify_current(quote, raw, as_of, name, datetime.now(SHANGHAI) if owned else cutoff)
                if risk_name(quote["name"]) or risk_name(name):
                    return code, None, "风险警示或退市名称，排除", False
                if set(expected) - set(raw.index):
                    raise ValueError("最近 251 个基准交易日有缺失，不能跳过停牌或缺行情日")
                window = raw.loc[expected]
                if (window.volume <= 0).any() or (window.amount <= 0).any():
                    return code, None, "预热区间存在无成交日，按保守规则排除", False
                if float(window.amount.iloc[-20:].mean()) < min_amount:
                    return code, None, "近 20 日平均成交额低于门槛", False
                adjusted, _ = client.bars(code, adjust="qfq", end=as_of)
                raw.to_parquet(history_dir / f"{code}-raw.parquet", index=False)
                adjusted.to_parquet(history_dir / f"{code}-qfq.parquet", index=False)
                row, reason = qualify(
                    code,
                    quote,
                    raw,
                    adjusted,
                    expected,
                    name,
                    min_amount,
                    datetime.now(SHANGHAI) if owned else cutoff,
                )
                if row is not None:
                    freeze_exposures(row, adjusted, benchmark, expected)
                return code, row, reason, False
            except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
                return code, None, str(exc), True

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(process, code) for code in codes]
            for future in as_completed(futures):
                code, row, reason, failed = future.result()
                result["attempted_count"] += 1
                data_failures += int(failed)
                if row:
                    common_rows.append(dict(row))
                    trend = trend_reason(row)
                    if trend is None:
                        row = dict(row)
                        row["reason"] = "通过当前名称/双源行情校验、251 日连续观测、量额与趋势过滤；风险观察不改变机械排名"
                        candidates.append(row)
                    else:
                        result["exclusions"].append(dict(ts_code=public_code(code), reason=trend))
                else:
                    result["exclusions"].append(dict(ts_code=public_code(code), reason=reason))
                progress(
                    f"检查 {result['attempted_count']}/{len(codes)}；通过过滤 {len(candidates)}；数据问题 {data_failures}"
                )
        result["research_universe"] = dict(
            schema="common-eligibility-v2", trend_prefiltered=False,
            rows=sorted(common_rows, key=lambda row: row["ts_code"]),
            source_universe_count=len(codes), attempted_count=result["attempted_count"],
            eligible_count=len(common_rows), data_failure_count=data_failures,
            policy="MAIN,current-name-crosscheck,251-contiguous-sessions,amount20;NO-trend-filter",
            point_in_time_scope="contemporaneous_freeze_only_not_historical_universe",
            execution_eligibility="unverified_no_opening_or_full_regulatory_evidence",
        )
        result["valid_count"] = len(candidates)
        result["candidates"] = rank_rows(candidates, top)
        result["status"] = "partial" if data_failures else "complete"
        result["exclusions"].sort(key=lambda x: x["ts_code"])
        if data_failures:
            result["warnings"].append(f"{data_failures} 只股票因数据问题被排除；排名仅针对成功检查的子集。")
        if symbols:
            result["warnings"].append("本次为自定义列表，不能称为全市场排名。")
    except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
        result["warnings"].append(str(exc))
        result["status"] = "blocked"
    finally:
        result["completed_at"] = now_iso()
        result["started_at"] = result["generated_at"]
        result["generated_at"] = result["completed_at"]
        if owned and result.get("as_of") != datetime.now(SHANGHAI).date().isoformat():
            result["status"] = "blocked"
            result["candidates"] = []
            result["warnings"].append("运行跨越日期或数据非今日，未发布当前候选")
        result["receipts"] = list(client.receipts)
        result["source_hash"] = source_hash
        if owned:
            client.close()
    write_result(output, result)
    return result


def doctor(output, network="direct", progress=lambda _: None):
    result = dict(
        mode="CURRENT_RESEARCH",
        status="blocked",
        generated_at=now_iso(),
        scope="接口连接小样",
        warnings=[],
        candidates=[],
        exclusions=[],
        checks=[],
    )
    client = CurrentClient(Path("runtime/current-cache"), network, ttl=0)
    try:
        for name, fn in [
            ("腾讯原始日线", lambda: client.bars("sh600000")),
            ("腾讯前复权日线", lambda: client.bars("sh600000", "qfq")),
            ("新浪交叉报价", lambda: client.quotes(["sh600000"])),
        ]:
            progress(f"检查 {name}")
            try:
                data = fn()
                count = len(data[0]) if isinstance(data, tuple) else len(data)
                result["checks"].append(dict(name=name, ok=count > 0, rows=count))
            except Exception as exc:
                result["checks"].append(dict(name=name, ok=False, error=str(exc)))
        result["status"] = "complete" if all(c["ok"] for c in result["checks"]) else "blocked"
        result["warnings"] = ["这里只验证连接及小样字段，不代表全量数据完整或股票适合买入。"]
        result["receipts"] = client.receipts
    finally:
        client.close()
    return write_result(output, result)
