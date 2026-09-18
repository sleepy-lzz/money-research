"""Offline Markdown/HTML/JSON report rendering."""

from __future__ import annotations

import html
import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (date, datetime, pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, pd.DataFrame):
        return [_jsonable(record) for record in value.to_dict(orient="records")]
    if isinstance(value, pd.Series):
        return [_jsonable(item) for item in value.tolist()]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


_RATIO_METRICS = {
    "total_return",
    "cagr",
    "benchmark_return",
    "benchmark_cagr",
    "excess_return",
    "max_drawdown",
    "max_drawdown_abs",
    "turnover",
    "average_exposure",
    "max_exposure",
    "win_rate",
    "closed_fill_win_rate",
}


def _display(value: Any, metric_key: str | None = None) -> str:
    if value is None:
        return "不可计算 / null"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "不可计算 / null"
        if metric_key in _RATIO_METRICS:
            return f"{value:.2%}"
        return f"{value:,.4f}"
    if isinstance(value, dict):
        if metric_key in {"annual_return", "annual_returns"}:
            formatted = {str(key): _display(item, "total_return") for key, item in value.items()}
            return json.dumps(formatted, ensure_ascii=False, sort_keys=True)
        return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)
    return str(value)


def _markdown_json(value: Any) -> str:
    text = json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True)
    return text.replace("```", "\\`\\`\\`")


def _cell(value: Any) -> str:
    """Compact, pipe-safe table cell text; full values remain in raw JSON."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return _display(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


_TABLE_COLUMNS: dict[str, list[str]] = {
    "候选": ["ts_code", "rank", "score", "eligible", "reason", "close", "industry"],
    "订单": [
        "order_id",
        "proposal_id",
        "ts_code",
        "side",
        "quantity",
        "decision_date",
        "execute_date",
        "status",
        "filled",
        "reason",
    ],
    "成交": [
        "fill_id",
        "order_id",
        "ts_code",
        "side",
        "quantity",
        "price",
        "fee",
        "trade_date",
        "realized_pnl",
    ],
    "持仓": ["ts_code", "quantity", "available_to_sell", "cost", "industry"],
    "权益曲线数据": ["trade_date", "equity", "cash", "receivable", "exposure", "regime"],
}


def _record_rows(records: Any) -> list[dict[str, Any]]:
    if records is None:
        return []
    if isinstance(records, dict):
        return [records]
    if isinstance(records, list):
        return [row for row in records if isinstance(row, dict)]
    return []


def _columns_for(title: str, rows: list[dict[str, Any]]) -> list[str]:
    preferred = _TABLE_COLUMNS.get(title, [])
    seen = set(preferred)
    extras = [key for row in rows for key in row if key not in seen]
    return preferred + list(dict.fromkeys(extras))


def _record_table_markdown(title: str, records: Any) -> str:
    rows = _record_rows(records)
    if not rows:
        return "暂无记录。"
    columns = _columns_for(title, rows)
    header = "| " + " | ".join(columns) + " |\n| " + " | ".join("---" for _ in columns) + " |"
    body = "\n".join("| " + " | ".join(_cell(row.get(column)) for column in columns) + " |" for row in rows)
    return header + "\n" + body


def _period_coverage_markdown(coverage: Any) -> str:
    if not isinstance(coverage, dict) or not coverage.get("years"):
        return "暂无年度覆盖记录。"
    lines = [
        f"样本期：{_cell(coverage.get('sample_start'))} 至 {_cell(coverage.get('sample_end'))}，"
        f"{_cell(coverage.get('sample_sessions'))} 个交易日；总体覆盖：`{_cell(coverage.get('sample_coverage'))}`。",
        "",
        "| 年份 | 样本收益 | 覆盖标签 | 样本起止 | 交易日数 |",
        "| --- | ---: | --- | --- | ---: |",
    ]
    for year, details in coverage["years"].items():
        if not isinstance(details, dict):
            continue
        lines.append(
            "| {year} | {return_value} | `{coverage}` | {start} 至 {end} | {sessions} |".format(
                year=year,
                return_value=_display(details.get("return"), "total_return"),
                coverage=_cell(details.get("coverage")),
                start=_cell(details.get("sample_start")),
                end=_cell(details.get("sample_end")),
                sessions=_cell(details.get("sessions")),
            )
        )
    return "\n".join(lines)


def _period_coverage_html(coverage: Any) -> str:
    if not isinstance(coverage, dict) or not coverage.get("years"):
        return "<p>暂无年度覆盖记录。</p>"
    rows = []
    for year, details in coverage["years"].items():
        if not isinstance(details, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(year))}</td>"
            f"<td>{html.escape(_display(details.get('return'), 'total_return'))}</td>"
            f"<td>{html.escape(_cell(details.get('coverage')))}</td>"
            f"<td>{html.escape(_cell(details.get('sample_start')))} 至 {html.escape(_cell(details.get('sample_end')))}</td>"
            f"<td>{html.escape(_cell(details.get('sessions')))}</td>"
            "</tr>"
        )
    return (
        f"<p>样本期：{html.escape(_cell(coverage.get('sample_start')))} 至 "
        f"{html.escape(_cell(coverage.get('sample_end')))}，"
        f"{html.escape(_cell(coverage.get('sample_sessions')))} 个交易日；"
        f"总体覆盖：<code>{html.escape(_cell(coverage.get('sample_coverage')))}</code>。</p>"
        '<div class="table-scroll"><table><thead><tr>'
        "<th>年份</th><th>样本收益</th><th>覆盖标签</th><th>样本起止</th><th>交易日数</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _equity_values(payload: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for row in payload.get("equity", []) or []:
        if isinstance(row, dict):
            raw = row.get("equity")
        else:
            raw = None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    return values


def _equity_svg(payload: dict[str, Any]) -> str:
    values = _equity_values(payload)
    width, height = 720, 240
    left, top, right, bottom = 46, 18, 14, 30
    chart_width = width - left - right
    chart_height = height - top - bottom
    if not values:
        return '<svg viewBox="0 0 720 240" role="img" aria-label="无权益数据"><text x="46" y="120" fill="#555">无权益数据</text></svg>'
    low, high = min(values), max(values)
    span = high - low
    if abs(span) < 1e-12:
        span = max(abs(high) * 0.01, 1.0)
        low -= span / 2
        high += span / 2
    points = []
    for index, value in enumerate(values):
        x = left if len(values) == 1 else left + chart_width * index / (len(values) - 1)
        y = top + chart_height * (high - value) / (high - low)
        points.append(f"{x:.2f},{y:.2f}")
    polyline = " ".join(points)
    return (
        f'<svg viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="权益曲线"><rect width="100%" height="100%" fill="#fff"/>'
        f'<line x1="{left}" y1="{top + chart_height}" x2="{width - right}" y2="{top + chart_height}" stroke="#bbb"/>'
        f'<polyline points="{polyline}" fill="none" stroke="#1565c0" stroke-width="2"/>'
        f'<text x="{left}" y="{height - 8}" font-size="12" fill="#555">首日</text>'
        f'<text x="{width - right - 26}" y="{height - 8}" font-size="12" fill="#555">末日</text>'
        f'<text x="{left}" y="12" font-size="12" fill="#555">{high:,.2f}</text>'
        f'<text x="{left}" y="{height - bottom + 2}" font-size="12" fill="#555">{low:,.2f}</text></svg>'
    )


def _record_section_markdown(title: str, records: Any) -> str:
    return (
        f"### {title}\n\n"
        f"{_record_table_markdown(title, records)}\n\n"
        f"<details><summary>查看原始 JSON</summary>\n\n```json\n"
        f"{_markdown_json(records if records is not None else [])}\n```\n</details>"
    )


def _html_records(title: str, records: Any) -> str:
    encoded_title = html.escape(title)
    rows = _record_rows(records)
    if not rows:
        table = "<p>暂无记录。</p>"
    else:
        columns = _columns_for(title, rows)
        headers = "".join(f"<th>{html.escape(str(column))}</th>" for column in columns)
        body = "".join(
            "<tr>"
            + "".join(f"<td>{html.escape(_cell(row.get(column)))}</td>" for column in columns)
            + "</tr>"
            for row in rows
        )
        table = (
            f'<div class="table-scroll"><table><thead><tr>{headers}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>"
        )
    encoded = html.escape(
        json.dumps(
            _jsonable(records if records is not None else []), ensure_ascii=False, indent=2, sort_keys=True
        )
    )
    return f"<h2>{encoded_title}</h2>{table}<details><summary>查看原始 JSON</summary><pre>{encoded}</pre></details>"


def write_report(output_dir: Path, payload: dict[str, Any]) -> dict[str, str]:
    """Write a self-contained Markdown report, HTML report, and JSON payload."""

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    clean_payload = _jsonable(payload)
    if not isinstance(clean_payload, dict):
        raise TypeError("payload must be a mapping")
    metrics = clean_payload.get("metrics") or {}
    run_id = clean_payload.get("run_id", "未命名运行")
    mode = clean_payload.get("mode", "unknown")
    synthetic = bool(clean_payload.get("synthetic", False))
    synthetic_label = (
        "合成数据（synthetic fixture）" if synthetic else "合成数据：否（请结合数据质量字段阅读）"
    )
    no_alpha = "本报告不宣称历史 AI Alpha；研究字段仅作记录，不构成交易归因。"
    execution_note = "成交采用日频近似成交（daily approximate fills），不代表真实逐笔成交。"
    trade_note = "平仓口径：若引擎提供，realized_pnl 是含买卖手续费的 FIFO 平仓净额；现金分红与送转股（新增股成本为零）属于独立账本事件。"
    metric_note = "兼容字段 trade_count/win_rate/profit_factor 已 deprecated，仍只表示 closed-fill；胜率是比例，盈亏比是倍数。"
    window_note = "固定规则滚动 OOS（no parameter fitting）；仅保留完整测试窗口，末尾不足 test_sessions 的窗口剔除；各窗口独立初始化，窗口权益不拼接为一条可交易净值曲线。"

    metric_rows = [
        ("总收益", "total_return", metrics.get("total_return")),
        ("CAGR（年化）", "cagr", metrics.get("cagr")),
        (
            "年度收益（按样本覆盖）",
            "annual_return",
            metrics.get("annual_return", metrics.get("annual_returns")),
        ),
        ("基准收益", "benchmark_return", metrics.get("benchmark_return")),
        ("超额收益", "excess_return", metrics.get("excess_return")),
        ("最大回撤", "max_drawdown", metrics.get("max_drawdown")),
        (
            "最大回撤持续交易日",
            "max_drawdown_duration_sessions",
            metrics.get("max_drawdown_duration_sessions", metrics.get("max_drawdown_duration")),
        ),
        ("Sharpe", "sharpe", metrics.get("sharpe")),
        ("Sortino", "sortino", metrics.get("sortino")),
        ("Calmar", "calmar", metrics.get("calmar")),
        ("换手率", "turnover", metrics.get("turnover")),
        ("交易成本", "costs", metrics.get("costs", metrics.get("total_costs"))),
        ("平均暴露", "average_exposure", metrics.get("average_exposure")),
        ("成交数", "fill_count", metrics.get("fill_count")),
        (
            "平仓成交数（closed-fill）",
            "closed_fill_count",
            metrics.get("closed_fill_count", metrics.get("close_fill_count")),
        ),
        (
            "平仓成交胜率（closed-fill）",
            "closed_fill_win_rate",
            metrics.get("closed_fill_win_rate", metrics.get("win_rate")),
        ),
        (
            "平仓成交盈亏比（closed-fill）",
            "closed_fill_profit_factor",
            metrics.get("closed_fill_profit_factor", metrics.get("profit_factor")),
        ),
    ]
    markdown_metrics = "\n".join(
        f"| {name} | {_display(value, metric_key)} |" for name, metric_key, value in metric_rows
    )
    markdown = f"""# A 股回测报告：{run_id}

> 数据标记：{synthetic_label}
>
> Alpha 声明：{no_alpha}
>
> 执行口径：{execution_note}
>
> 窗口口径：{window_note}

## 运行信息

| 字段 | 值 |
| --- | --- |
| run_id | {run_id} |
| mode | {mode} |
| snapshot_id | {clean_payload.get("snapshot_id", "—")} |
| as_of | {clean_payload.get("as_of", "—")} |
| synthetic | {str(synthetic).lower()} |

## 指标

| 指标 | 数值 |
| --- | --- |
{markdown_metrics}

基准锚点：{metrics.get("benchmark_anchor_date", "不可用")}；首日无前一基准日时，首日基准收益保持 null。

## 样本期覆盖

{_period_coverage_markdown(metrics.get("period_coverage", {}))}

CAGR（年化）按整个样本交易日数计算；年度收益是样本覆盖到的该年区间收益，不代表未覆盖边界的完整自然年收益。

{trade_note} 平仓成交胜率和盈亏比只使用 `fills.realized_pnl` 非空的 closed-fill 记录；买入行的空值不会被当作亏损。交易级 round-trip/episode 统计尚未实现。{metric_note}

## 数据质量与警告

{_markdown_json(clean_payload.get("data_quality", {}))}

{_record_section_markdown("候选", clean_payload.get("candidates", []))}

{_record_section_markdown("研究记录", clean_payload.get("research", []))}

{_record_section_markdown("风险状态", clean_payload.get("risk", {}))}

{_record_section_markdown("当前市场状态", clean_payload.get("regime", {}))}

{_record_section_markdown("订单", clean_payload.get("orders", []))}

{_record_section_markdown("成交", clean_payload.get("fills", []))}

{_record_section_markdown("持仓", clean_payload.get("positions", []))}

{_record_section_markdown("权益曲线数据", clean_payload.get("equity", []))}

### 警告列表

{_markdown_json(clean_payload.get("warnings", []))}
"""

    escaped_run_id = html.escape(str(run_id))
    escaped_mode = html.escape(str(mode))
    escaped_snapshot = html.escape(str(clean_payload.get("snapshot_id", "—")))
    escaped_as_of = html.escape(str(clean_payload.get("as_of", "—")))
    html_rows = "\n".join(
        f"<tr><th>{html.escape(str(name))}</th><td>{html.escape(_display(value, metric_key))}</td></tr>"
        for name, metric_key, value in metric_rows
    )
    warning_json = html.escape(
        json.dumps(_jsonable(clean_payload.get("warnings", [])), ensure_ascii=False, indent=2)
    )
    data_quality_json = html.escape(
        json.dumps(_jsonable(clean_payload.get("data_quality", {})), ensure_ascii=False, indent=2)
    )
    chart = _equity_svg(clean_payload)
    html_report = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>A 股回测报告：{escaped_run_id}</title>
<style>body{{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;width:calc(100vw - 2rem);max-width:980px;box-sizing:border-box;overflow-x:hidden;margin:2rem auto;padding:0 1rem;color:#202124;line-height:1.5}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}.table-scroll{{max-width:100%;overflow-x:auto;margin:1rem 0}}.table-scroll table{{width:max-content;min-width:100%;margin:0}}th,td{{border:1px solid #ddd;padding:.45rem;text-align:left;overflow-wrap:anywhere;word-break:break-word;max-width:28rem}}th{{background:#f5f7fa}}pre{{background:#f6f8fa;padding:1rem;max-width:100%;box-sizing:border-box;overflow:auto;overflow-wrap:anywhere;white-space:pre-wrap}}.notice{{padding:.8rem 1rem;background:#fff8e1;border-left:4px solid #f9a825}}.chart{{border:1px solid #ddd;padding:.5rem;overflow:auto}}</style></head>
<body><h1>A 股回测报告：{escaped_run_id}</h1>
<div class="notice"><strong>数据标记：</strong>{html.escape(synthetic_label)}<br><strong>Alpha 声明：</strong>{html.escape(no_alpha)}<br><strong>执行口径：</strong>{html.escape(execution_note)}<br><strong>窗口口径：</strong>{html.escape(window_note)}</div>
<h2>运行信息</h2><table><tr><th>字段</th><th>值</th></tr><tr><td>run_id</td><td>{escaped_run_id}</td></tr><tr><td>mode</td><td>{escaped_mode}</td></tr><tr><td>snapshot_id</td><td>{escaped_snapshot}</td></tr><tr><td>as_of</td><td>{escaped_as_of}</td></tr><tr><td>synthetic</td><td>{str(synthetic).lower()}</td></tr></table>
<h2>权益曲线</h2><div class="chart">{chart}</div>
<h2>指标</h2><table><tr><th>指标</th><th>数值</th></tr>{html_rows}</table>
<p>基准锚点：{html.escape(str(metrics.get("benchmark_anchor_date", "不可用")))}；首日无前一基准日时，首日基准收益保持 null。</p>
<h2>样本期覆盖</h2>{_period_coverage_html(metrics.get("period_coverage", {}))}
<p>CAGR（年化）按整个样本交易日数计算；年度收益是样本覆盖到的该年区间收益，不代表未覆盖边界的完整自然年收益。</p>
<p>{html.escape(trade_note)} 平仓成交胜率和盈亏比只使用 <code>fills.realized_pnl</code> 非空的 closed-fill 记录；买入行的空值不会被当作亏损。交易级 round-trip/episode 统计尚未实现。{html.escape(metric_note)}</p>
<h2>数据质量</h2><pre>{data_quality_json}</pre><h2>警告</h2><pre>{warning_json}</pre>
{_html_records("候选", clean_payload.get("candidates", []))}
{_html_records("研究记录", clean_payload.get("research", []))}
{_html_records("风险状态", clean_payload.get("risk", {}))}
{_html_records("当前市场状态", clean_payload.get("regime", {}))}
{_html_records("订单", clean_payload.get("orders", []))}
{_html_records("成交", clean_payload.get("fills", []))}
{_html_records("持仓", clean_payload.get("positions", []))}
{_html_records("权益曲线数据", clean_payload.get("equity", []))}
</body></html>
"""

    markdown_path = directory / "report.md"
    html_path = directory / "report.html"
    json_path = directory / "report.json"
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html_report, encoding="utf-8")
    json_path.write_text(
        json.dumps(clean_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"markdown": str(markdown_path), "html": str(html_path), "json": str(json_path)}
