"""Cohort-level diagnostics; price observations are never compounded into NAV."""
from __future__ import annotations

import html
import math
from pathlib import Path

import numpy as np

from .forward_lab import _dt
from .research_protocol import K, PROTOCOL, canonical, strict_loads
from .session_calendar import next_sessions, sessions


def summarize_exposures(arm, previous=None):
    weights = arm["weights"]
    cash = arm["cash_weight"]
    result = dict(stock_HHI=sum(w * w for w in weights.values()), max_stock_weight=max(weights.values(), default=0),
                  cash_weight=cash, holdings_count=len(weights), industry_weights={}, industry_known_weight=0,
                  market_cap_known_weight=0, beta_known_weight=0, volatility_known_weight=0,
                  weighted_log_market_cap=None, weighted_beta120=None, weighted_daily_volatility60=None,
                  target_selection_turnover=None, turnover_is_actual_trading=False,
                  turnover_definition="0.5*L1 distance between consecutive observed target vectors including cash; not fills")
    beta_sum = cap_sum = vol_sum = 0.0
    for row in arm["rows"]:
        weight = weights[row["ts_code"]]
        as_of = row.get("exposures_as_of")
        sector = row.get("industry")
        valid_sector = sector and row.get("industry_as_of") and as_of and row["industry_as_of"] <= as_of
        name = sector if valid_sector else "unknown"
        result["industry_weights"][name] = result["industry_weights"].get(name, 0) + weight
        result["industry_known_weight"] += weight if valid_sector else 0
        cap = row.get("market_cap")
        if isinstance(cap, (int, float)) and math.isfinite(cap) and cap > 0 and as_of and row.get("market_cap_as_of") and row["market_cap_as_of"] <= as_of:
            cap_sum += weight * math.log(cap)
            result["market_cap_known_weight"] += weight
        for key, coverage, output in (("market_beta120", "beta_known_weight", "beta"), ("daily_volatility60", "volatility_known_weight", "vol")):
            value = row.get(key)
            if isinstance(value, (int, float)) and math.isfinite(value) and as_of:
                result[coverage] += weight
                if output == "beta":
                    beta_sum += weight * value
                else:
                    vol_sum += weight * value
    invested = sum(weights.values())
    # Unknown exposure never silently becomes zero. Cash is explicitly zero-beta in this descriptive proxy.
    if abs(result["beta_known_weight"] - invested) < 1e-9:
        result["weighted_beta120"] = beta_sum
    if abs(result["volatility_known_weight"] - invested) < 1e-9:
        result["weighted_daily_volatility60"] = vol_sum
    if invested and abs(result["market_cap_known_weight"] - invested) < 1e-9:
        result["weighted_log_market_cap"] = cap_sum / invested
    if previous is not None:
        before = previous["weights"]
        result["target_selection_turnover"] = 0.5 * (sum(abs(weights.get(code, 0) - before.get(code, 0)) for code in set(weights) | set(before)) + abs(cash - previous["cash_weight"]))
    result["portfolio_volatility_warning"] = "weighted_single_stock_volatility_is_an_exposure_summary_not_portfolio_volatility"
    return result


def paired_block_interval(grid, *, block=20, replicates=1999, seed=20260916, family=4):
    """Resample date blocks jointly AFTER collapsing each cross-section.

    None slots retain calendar spacing. Do not compress missing dates before
    resampling, and never treat stock/horizon rows as independent draws.
    """
    values = np.array([np.nan if value is None else value for value in grid], dtype=float)
    n = len(values)
    finite = np.isfinite(values)
    count = int(finite.sum())
    result = dict(date_grid_count=n, paired_dates=count, missing_dates=n - count,
                  mean=float(values[finite].mean()) if count else None, ci=None,
                  method="moving-block-on-calendar-grid", block_sessions=block,
                  minimum_floor_is_not_sufficient_evidence=True)
    minimum = PROTOCOL["statistics"]["min_paired_dates"]
    if count < minimum or n < block * 6:
        result["reason"] = "insufficient_paired_dates_or_blocks"
        return result
    if (n - count) / n > PROTOCOL["statistics"]["max_missing_fraction"]:
        result["reason"] = "missing_data_threshold_exceeded"
        return result
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(replicates):
        starts = rng.integers(0, n - block + 1, size=math.ceil(n / block))
        sampled = np.concatenate([values[start:start + block] for start in starts])[:n]
        valid = sampled[np.isfinite(sampled)]
        if len(valid):
            means.append(float(valid.mean()))
    alpha = 0.05 / family
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    result.update(ci=[float(lo), float(hi)], confidence=1 - alpha, family_size=family,
                  reason="interval_is_diagnostic_not_automatic_strategy_promotion")
    return result


def study_statistics(summary, db):
    batches = summary["batches"]
    result = dict(primary_horizon=5, alpha=None, alpha_reason="PIT factor model and executable net account returns unavailable",
                  deflated_sharpe=None, deflated_sharpe_reason=PROTOCOL["statistics"]["deflated_sharpe"],
                  sharpe=None, sharpe_reason="no validated non-overlapping account return series",
                  conclusion="evidence_insufficient", automatic_strategy_promotion=False,
                  ai_coverage={}, phase_comparisons={}, regime_diagnostics={},
                  completed_cohort_count=0, frozen_batch_count=len(batches))
    finalized = [batch for batch in batches if batch["ai_deadline_passed"] and batch["candidate_count"] > 0]
    for mode in summary["study"]["enabled_modes"]:
        arms = [batch["arms"]["ai_" + mode] for batch in finalized]
        denominator = len(arms)
        fraction = lambda key: sum(bool(arm[key]) for arm in arms) / denominator if denominator else None
        result["ai_coverage"][mode] = dict(
            finalized_requested_batches=denominator, pending_batches=len(batches) - len(finalized),
            valid_submission_rate=fraction("valid_ai_submission"), effective_ai_batch_coverage=fraction("effective_ai_batch"),
            whole_batch_fallback_rate=fraction("fallback"), changed_selection_rate=fraction("changed_selection"),
            mean_decision_fallback_rate=float(np.mean([arm["decision_fallback_rate"] for arm in arms])) if arms else None,
            interpretation="fallback outcomes belong to the AI-enabled policy comparison, not to successful AI judgments",
            failure_reasons=[dict(batch_id=batch["batch_id"], reason=batch["arms"]["ai_" + mode].get("fallback_reason")) for batch in finalized if batch["arms"]["ai_" + mode].get("fallback_reason")])
    if not batches:
        result["reason"] = "no_v2_prospective_freezes; old batches never retroactively relabelled"
        return result
    lookup = {batch["as_of"]: batch for batch in batches}
    db_batch = db.execute("SELECT selection_json FROM batches ORDER BY as_of DESC LIMIT 1").fetchone()
    calendar = strict_loads(db_batch[0])["calendar_manifest"]
    anchor = _dt(summary["study"]["registered_at"], "registered_at").date().isoformat()
    today = _dt(summary["generated_at"], "generated_at").date().isoformat()
    end = min(today, calendar["covered_end"])
    grid_days = sessions(anchor, end, calendar) if anchor <= end else []
    # Only scheduled cohorts whose outcome could have matured enter missingness denominators.
    mature = []
    for index, day in enumerate(grid_days, 1):
        try:
            horizon_end = next_sessions(day, 5, calendar)[-1]
            if horizon_end < today or (horizon_end == today and _dt(summary["generated_at"], "generated_at").hour >= 16):
                mature.append((index, day))
        except ValueError:
            continue
    result["calendar_coverage_end"] = calendar["covered_end"]
    result["unavailable_calendar_beyond_coverage"] = today > calendar["covered_end"]
    comparisons = {"mechanical-minus-common": ("mechanical", "paired_minus_common")}
    comparisons.update({mode + "-minus-mechanical": ("ai_" + mode, "paired_minus_mechanical") for mode in summary["study"]["enabled_modes"]})
    result["completed_cohort_count"] = sum(batch["arms"]["mechanical"]["horizons"]["5"]["full_weight_price_return"] is not None for batch in batches)
    for phase in ("development", "validation", "final"):
        lo, hi = PROTOCOL["phases"][phase]
        days = [day for ordinal, day in mature if lo <= ordinal <= hi - 20]
        result["phase_comparisons"][phase] = {}
        for label, (name, metric) in comparisons.items():
            values = []
            data_bad = 0
            valid_ai = changed_ai = 0
            for day in days:
                batch = lookup.get(day)
                if batch is None:
                    values.append(None)
                    continue
                if batch["universe_data_failure_rate"] > PROTOCOL["statistics"]["max_missing_fraction"]:
                    data_bad += 1
                    values.append(None)
                    continue
                arm = batch["arms"][name]
                values.append(arm["horizons"]["5"].get(metric))
                valid_ai += int(arm.get("effective_ai_batch", False))
                changed_ai += int(arm.get("changed_selection", False))
            info = paired_block_interval(values)
            info.update(data_quality_excluded_dates=data_bad, phase=phase, primary_metric="D5_paired_price_difference",
                        selected_conditional_analysis="valid_AI_only is secondary and subject to coverage/selection bias",
                        actual_changed_selection_dates=changed_ai,
                        effective_ai_batch_coverage=valid_ai / len(days) if days and name.startswith("ai_") else None)
            if phase != "final":
                info["inferential_use"] = "development_or_validation_diagnostic_only"
            elif len(grid_days) < PROTOCOL["phases"]["final"][1]:
                info["inferential_use"] = "final_stage_not_closed; descriptive_only_no_early_stopping"
            elif summary["synthetic"]:
                info["inferential_use"] = "synthetic_demo_no_strategy_evidence"
            elif info["ci"] is None:
                info["inferential_use"] = "insufficient_evidence"
            elif info["ci"][1] <= 0:
                info["inferential_use"] = "no_positive_increment_detected_under_registered_observation_metric"
            elif info["ci"][0] <= PROTOCOL["statistics"]["economic_threshold"]:
                info["inferential_use"] = "uncertain_or_below_registered_economic_threshold"
            elif name.startswith("ai_") and info["effective_ai_batch_coverage"] < PROTOCOL["statistics"]["min_effective_ai_batch_coverage"]:
                info["inferential_use"] = "AI_coverage_insufficient_for_increment_claim"
            else:
                info["inferential_use"] = "requires_independent_cost_execution_and_risk_review_no_automatic_upgrade"
            result["phase_comparisons"][phase][label] = info
    # Secondary exposures/regimes diagnose outcomes without redefining the strategy or selecting the winner.
    for batch in batches:
        regime = batch["benchmark_diagnostics"].get("regime", "unknown")
        target = result["regime_diagnostics"].setdefault(regime, {})
        for name, arm in batch["arms"].items():
            value = arm["horizons"]["5"]["full_weight_price_return"]
            if value is not None:
                target.setdefault(name, []).append(value)
    for groups in result["regime_diagnostics"].values():
        for name, values in list(groups.items()):
            groups[name] = dict(cohort_dates=len(values), mean_D5_price_return=float(np.mean(values)),
                                worst_D5_cohort=float(min(values)), overlapping_cohorts=True,
                                not_account_drawdown=True, not_independent_samples=True)
    return result


def write_research_report(folder, summary):
    """Human entry and machine-readable audit report; no HTML from evidence is trusted."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    raw = canonical(summary)
    def atomic(name, text):
        path = folder / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    atomic("latest.json", raw)
    def esc(value):
        return html.escape(str(value))
    def pct(value):
        return "未知 / 尚未完成" if value is None else f"{value:+.2%}"
    rows = []
    for batch in summary["batches"]:
        for name, arm in batch["arms"].items():
            d5 = arm["horizons"]["5"]
            exposure = arm["exposures"]
            values = [batch["as_of"], name, ", ".join(arm["codes"][:K]) + (" …（全池等权）" if name == "common_equal_weight" and len(arm["codes"]) > K else ""),
                      f"{d5['known']}/{d5['samples']}", pct(d5["full_weight_price_return"]), pct(d5["paired_minus_common"]),
                      pct(d5["paired_minus_mechanical"]), arm.get("ai_status", "机械规则"), arm.get("fallback_reason") or "—",
                      f"{exposure['stock_HHI']:.3f}", pct(exposure["target_selection_turnover"]), pct(exposure["industry_known_weight"])]
            rows.append("<tr>" + "".join("<td>" + esc(value) + "</td>" for value in values) + "</tr>")
    header = ["冻结日", "研究组", "选择", "D5已知/全部", "完整权重价格变动", "减共同池", "减机械", "AI状态", "失败/回退原因", "股票HHI", "目标选择换手", "行业已知权重"]
    cards = ["研究工具：两层选择、冻结、受约束导入及报告已接线；软件测试不代表策略有效",
             f"前瞻价格观察：{len(summary['batches'])} 个本版本冻结批次；D1不是可执行当日买卖收益",
             "连续模拟账户：未接入新实验；需可信成交证据和独立账本适配验收",
             "人工条件计划：保留原实现；研究模块未读取/修改个人账户"]
    banner = "合成演示 · 不是任何真实前瞻或收益证据" if summary["synthetic"] else "真实研究命名空间 · 有效性结论仍需样本与执行验证"
    body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>两层研究验收与观察</title><style>body{{font:15px/1.7 system-ui,'Microsoft YaHei';margin:28px;color:#182c3a;background:#f5f7fa}}main{{max-width:1350px;margin:auto}}section{{background:white;padding:22px;border:1px solid #d6e1e9;border-radius:10px;margin:18px 0}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border:1px solid #d6e1e9;padding:8px;text-align:left;vertical-align:top}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.scroll{{overflow:auto}}h1{{font-size:29px}}</style><main>
<h1>机械策略价值与 AI 候选内增量</h1><p><strong>{esc(banner)}</strong></p><p>规则版本：{esc(summary['research_version'])} · 研究身份：{esc(summary['study']['study_hash'])}</p>
<section><h2>四条路径分别验收</h2>{''.join('<p>'+esc(line)+'</p>' for line in cards)}</section>
<section><h2>先看比较口径</h2><p>机械及候选实验固定5个权重槽；共同合格池基准为全池等权，广度不同是明确的基准例外。未分配槽位为零收益名义现金。缺失股票价格时完整权重指标为空，不把剩余股票重新加权。</p><p>1/5/20日均为独立候选价格观察，不拼接净值；实际成交费用、T+1、停牌、涨跌停及公司行动必须由独立撮合账本检验。差值不是风险调整 Alpha。</p></section>
<section><h2>冻结、选择、观察与归因</h2><div class="scroll"><table><thead><tr>{''.join('<th>'+x+'</th>' for x in header)}</tr></thead><tbody>{''.join(rows) or '<tr><td colspan="12">没有本版本前瞻冻结，旧批次未迁移为新样本。</td></tr>'}</tbody></table></div></section>
<section><h2>AI覆盖、回退与统计边界</h2><pre>{esc(canonical(summary['statistics']))}</pre></section>
<section><h2>完整不可混用的记录</h2><details><summary>展开运行、导入、缺失、证据及模式规则</summary><pre>{esc(raw)}</pre></details></section>
<p>结论允许没有改善或证据不足；本程序不会根据短期价格表现替换主策略。模式名称不保证回撤或收益。</p></main></html>'''
    atomic("latest.html", body)
    md = "# 两层研究报告\n\n" + banner + "\n\n" + "\n\n".join(cards)
    md += "\n\n## AI覆盖与统计边界\n\n```json\n" + canonical(summary["statistics"]) + "\n```\n\n完整记录：latest.json；可视入口：latest.html。价格观察不属于账户收益。\n"
    atomic("latest.md", md)
    return dict(status="report_written", html=str(folder / "latest.html"), json=str(folder / "latest.json"), markdown=str(folder / "latest.md"), synthetic=summary["synthetic"])
