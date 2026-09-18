"""Scheduled forward research. No broker, personal account mutation or historical backfill."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from html import escape
from pathlib import Path
from uuid import uuid4

from .current_data import SHANGHAI, CurrentClient, mainboard_code
from .current_screen import screen, verify_current


@contextmanager
def exclusive_run(folder):
    """OS-held lock is released on exit/crash; never remove another process's lock."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "run.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("每日检验已在运行，本次不重复启动") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temp.write_text(value, encoding="utf-8")
    temp.replace(path)


@contextmanager
def run_journal(folder, output):
    """Persist lifecycle independently of reports; the OS lock owns liveness."""
    with exclusive_run(folder):
        output.mkdir(parents=True, exist_ok=True)
        pointer = folder / 'latest-run.json'
        previous = None
        if pointer.exists():
            try:
                old = json.loads(pointer.read_text(encoding='utf-8'))
                if old.get('status') == 'running':
                    previous = {'output': old.get('output'), 'last_stage': old.get('stage'),
                                'status': 'interrupted', 'reason': '前次未记录结束，当前已取得独占锁'}
            except (ValueError, OSError):
                previous = {'status': 'unreadable_journal'}
        state = dict(status='running', stage='starting', pid=os.getpid(),
                     output=str(output.resolve()), started_at=datetime.now(SHANGHAI).isoformat(),
                     python=sys.executable, runner_path=str(Path(__file__).resolve()),
                     runner_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                     previous_unfinished=previous)

        def checkpoint(stage):
            state.update(stage=stage, updated_at=datetime.now(SHANGHAI).isoformat())
            encoded = json.dumps(state, ensure_ascii=False, indent=2)
            atomic_text(output / 'run-state.json', encoded)
            atomic_text(pointer, encoded)

        checkpoint('starting')
        try:
            yield checkpoint
        except BaseException as exc:
            state.update(status='failed', error=type(exc).__name__)
            checkpoint('interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed')
            raise
        else:
            report = output / 'result.json'
            if report.exists():
                result = json.loads(report.read_text(encoding='utf-8'))
                state.update(status='finished', outcome=result.get('status'))
            else:
                state.update(status='failed', error='missing_result')
            checkpoint('finished')


def check_software(root, output):
    tests = [
        "test_forward_lab.py", "test_daily_lab.py", "test_planner.py", "test_context_feed.py", "test_market_context.py", "test_financial_context.py",
        "test_session_calendar.py", "test_current_screen.py", "test_paper_workspace.py",
        "test_intraday.py", "test_live_quotes.py", "test_ai_overlay.py", "test_review_center.py", "test_research_v2.py",
    ]
    command = [sys.executable, "-m", "pytest", "-q", *[str(root / "tests" / t) for t in tests]]
    try:
        completed = subprocess.run(command, cwd=root, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        captured = (exc.stdout or b"") + (exc.stderr or b"")
        text = captured.decode("utf-8", errors="replace") + "\n软件检查超时（180 秒），未通过。\n"
        (output / "checks.log").write_text(text, encoding="utf-8")
        return {"passed": False, "summary": "软件检查超时（180 秒）"}
    text = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
    (output / "checks.log").write_text(text, encoding="utf-8")
    return {
        "passed": completed.returncode == 0,
        "summary": text.strip().splitlines()[-1] if text.strip() else "无测试输出",
    }


def observe_existing(book, client, benchmark, as_of, output):
    """Persist old signals' observations independently of today's new selection."""
    watch = book.watch_codes()
    histories, errors = {}, {}
    try:
        quotes = client.quotes([mainboard_code(c) for c in watch]) if watch else {}
    except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
        quotes = {}
        errors["quotes"] = str(exc)
    history_folder = output / "observed-prices"
    history_folder.mkdir(exist_ok=True)
    for code in watch:
        try:
            raw, name = client.bars(mainboard_code(code), end=as_of)
            qfq, _ = client.bars(mainboard_code(code), adjust="qfq", end=as_of)
            verify_current(quotes.get(mainboard_code(code)), raw, as_of, name, datetime.now(SHANGHAI))
            raw.to_parquet(history_folder / f"{code}-raw.parquet", index=False)
            qfq.to_parquet(history_folder / f"{code}-qfq.parquet", index=False)
            histories[code] = {"raw": raw, "qfq": qfq}
        except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
            errors[code] = str(exc)
    atomic_text(output / "quotes.json", json.dumps(quotes, ensure_ascii=False))
    observations = book.observe(as_of, benchmark, histories, datetime.now(SHANGHAI).isoformat())
    atomic_text(output / "observations.json", json.dumps(observations, ensure_ascii=False, indent=2))
    issues = [
        {k: row.get(k) for k in ("signal_date", "arm", "ts_code", "horizon", "reason")}
        for batch in observations.get("batches", [])
        for row in batch.get("observations", [])
        if row.get("status") == "unknown"
    ]
    return dict(
        watch_count=len(watch), data_errors=errors,
        observation_issues=issues[:50], observation_issue_count=len(issues),
    )


def observation_table(summary):
    names = {
        "mechanical": "固定机械基线",
        "momentum60": "60 日动量对照",
        "momentum120": "120 日动量对照",
        "news_guard": "新闻风险过滤对照",
        "ai_conservative": "AI 保守（价格观察）",
        "ai_balanced": "AI 平衡（价格观察）",
        "ai_aggressive": "AI 积极（价格观察）",
    }
    batches = summary.get("batches", [])
    if not batches:
        return "<p>还没有前瞻样本。首次真实收盘采集后开始冻结，随后逐日验证。</p>"
    rows = []
    for batch in reversed(batches):
        for arm, data in batch.get("arms", {}).items():
            for horizon, values in data.get("horizons", {}).items():
                price = values.get("avg_price_return")
                excess = values.get("avg_excess_return")
                cells = [
                    batch["as_of"],
                    names.get(arm, arm),
                    horizon,
                    f"{values.get('known', 0)} / {values.get('samples', 0)}",
                    str(values.get("unknown", 0)),
                    str(values.get("pending", 0)),
                    "—" if price is None else f"{price:+.2%}",
                    "—" if excess is None else f"{excess:+.2%}",
                ]
                rows.append("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in cells) + "</tr>")
    return (
        "<p>每个冻结日期独立展示。已知样本均值不包含未知样本，不能把它当成整体策略收益。</p><div style='overflow:auto'><table><thead><tr>"
        + "".join(
            f"<th>{s}</th>"
            for s in [
                "冻结日期",
                "对照",
                "观察日数",
                "已知/全部",
                "未知",
                "等待",
                "已知均价涨幅",
                "与指数差值",
            ]
        )
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def write_report(folder, output, result):
    encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    atomic_text(output / "result.json", encoded)
    labels = {"deferred": "等待收盘", "blocked": "数据待核验", "complete": "采集完成", "partial": "部分完成"}
    status = labels.get(result["status"], result["status"])
    notes = "\n".join(f"- {s}" for s in result["notes"])
    md = f"# 每日前瞻检验 · {result['as_of']}\n\n状态：{status}\n\n{notes}\n\n"
    md += "价格观察不是成交收益；没有现金账本、费用收益或实盘买卖。实验基线不会自动替换。\n\n"
    md += (
        "## 样本与对照\n\n```json\n"
        + json.dumps(result.get("lab", {}), ensure_ascii=False, indent=2)
        + "\n```\n"
    )
    atomic_text(output / "report.md", md)
    checks = result.get("checks", {})
    badges = [
        ("运行状态", status),
        (
            "软件检查",
            "通过" if checks.get("passed") else "未通过" if checks.get("passed") is False else "未运行",
        ),
        ("当前候选", str(result.get("candidate_count", "—"))),
        ("决策状态", "保留固定基线"),
    ]
    cards = "".join(
        f"<div class='card'><small>{escape(k)}</small><strong>{escape(v)}</strong></div>" for k, v in badges
    )
    bullets = "".join(f"<li>{escape(s)}</li>" for s in result["notes"])
    problems = "".join(
        f"<li>{escape(str(row.get('signal_date')))} · {escape(str(row.get('ts_code')))} · "
        f"{row.get('horizon')} 日：{escape(str(row.get('reason')))}</li>"
        for row in result.get("observation_issues", [])
    )
    coverage = result.get("source_coverage", {})
    coverage_text = (
        "尚未采集当日新闻与社区。"
        if not coverage
        else (
            f"当日 {len(coverage)} 个代码：新闻可用 {sum(v.get('news') == 'available' for v in coverage.values())} 个，"
            f"社区样本足够 {sum(v.get('community') == 'available' for v in coverage.values())} 个。缺失仍为未知。"
        )
    )
    report = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日自动复盘</title><style>body{{margin:0;background:#eef3f7;color:#193047;font:15px/1.7 system-ui,'Microsoft Yahei'}}main{{max-width:1120px;margin:auto;padding:36px 24px}}header{{border-left:5px solid #169c92;padding-left:22px;margin-bottom:28px}}h1{{margin:5px 0;font-size:32px}}small{{color:#637a8d}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}.card,section{{background:white;border:1px solid #dde6ef;border-radius:14px;padding:22px;margin-bottom:18px}}strong{{display:block;font-size:23px;margin-top:9px}}h2{{font-size:18px;margin-top:0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}.note{{color:#657484}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:10px;border-bottom:1px solid #e4edf3;text-align:left;white-space:nowrap}}th{{background:#eef5f8}}@media(max-width:700px){{.grid{{grid-template-columns:1fr 1fr}}}}</style>
<main><header><small>FORWARD RESEARCH · 自动复盘</small><h1>每日前瞻检验</h1><div>{escape(result["as_of"])} · 记录于 {escape(result["created_at"])}</div></header><div class="grid">{cards}</div>
<section><h2>今天发生了什么</h2><ul>{bullets}</ul></section>
<section><h2>数据覆盖与待核验事项</h2><p>{escape(coverage_text)}</p><ul>{problems}</ul><p class="note">无缺失记录不代表已证明数据完整。完整来源和观察记录保存在本次运行目录。</p></section>
<section><h2>第二版两层研究</h2><p>新版共同池与 AI 对照在独立账本运行。状态和完整报告位于 runtime/research-v2/latest.html；网页入口 /research。下面是保留的第一版历史视图，不混入新版统计。</p><pre>{escape(json.dumps(result.get("research_v2", {"status": "not_started"}), ensure_ascii=False, indent=2))}</pre></section><section><h2>第一版历史候选观察（不可用于共同池检验）</h2><p>在原机械候选中比较：原排名、60 日动量、120 日动量、新闻风险过滤。冻结后观察 1 / 5 / 20 个交易日，不回写历史选择。</p>{observation_table(result.get("lab", {}))}<details><summary>数据版本与完整记录</summary><pre>{escape(json.dumps(result.get("lab", {}), ensure_ascii=False, indent=2))}</pre></details></section>
<section><h2>接下来怎么判断</h2><p>每天积累样本，每周检查数据缺失、价格表现与对照差异。样本不足继续观察；因子改动进入新实验版本，不根据短期涨跌自动替换主策略。</p><p class="note">这里的价格涨幅不是模拟成交收益，也不证明策略有 Alpha。当前没有无人值守实盘下单。关闭电脑或 Codex 后，本地定时任务无法按时运行。</p></section></main></html>"""
    atomic_text(output / "report.html", report)
    # Latest is a pointer/view; immutable dated reports remain available.
    atomic_text(folder / "latest.json", encoded)
    atomic_text(folder / "latest.html", report)
    return result


def run(root, output=None, check_only=False, progress=lambda _: None):
    from .context_feed import fetch_context
    from .forward_lab import ForwardLab

    root = Path(root).resolve()
    folder = root / "runtime/daily-lab"
    now = datetime.now(SHANGHAI)
    output = Path(output) if output else folder / (now.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
    with run_journal(folder, output) as checkpoint:
        output.mkdir(parents=True, exist_ok=True)
        result = dict(
            mode="FORWARD_RESEARCH",
            status="blocked",
            as_of=now.date().isoformat(),
            created_at=now.isoformat(),
            runner_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            runner_path=str(Path(__file__).resolve()),
            notes=[],
            checks={},
            phases={"observation": "not_run", "selection": "not_run"},
            output=str(output),
        )
        book = ForwardLab(folder / "observations")
        client = None
        research = None
        try:
            progress("运行前瞻检验相关软件测试")
            checkpoint('software_checks')
            result["checks"] = check_software(root, output)
            if not result["checks"]["passed"]:
                raise ValueError("软件检查未通过，本次停止采集与样本登记；查看 checks.log")
            result["lab"] = book.summary()
            if check_only or now.hour < 16:
                result["status"] = "deferred"
                result["notes"].append("软件检查完成；等待交易日 16:00 后真实收盘数据，不回填此前日期。")
                return write_report(folder, output, result)
            from .research_lab import ResearchLab
            try:
                research = ResearchLab(root)
                result["research_v2"] = {"status": "registered", "study_hash": research.registry["study_hash"]}
            except (ValueError, OSError) as exc:
                result["research_v2"] = {"status": "blocked", "reason": str(exc)}
            client = CurrentClient(root / "runtime/current-cache")
            checkpoint('benchmark')
            benchmark, _ = client.bars("sh000300", end=result["as_of"])
            if benchmark.index[-1] != result["as_of"]:
                result["notes"].append("基准不是今天：可能休市或数据未更新；本次不登记样本、不伪造持平收益。")
                return write_report(folder, output, result)
            benchmark.to_parquet(output / "benchmark.parquet", index=False)
            # Observing old signals must survive a broken universe/news/selection provider.
            progress("对此前已冻结候选进行后续行情观察")
            checkpoint('observation')
            try:
                result.update(observe_existing(book, client, benchmark, result["as_of"], output))
                result["phases"]["observation"] = (
                    "partial" if result["data_errors"] or result["observation_issue_count"] else "complete"
                )
                result["notes"].append(f"已独立观察 {result['watch_count']} 个此前冻结候选代码。")
            except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
                result["phases"]["observation"] = "failed"
                result["notes"].append("既有样本观察失败：" + str(exc))
                atomic_text(output / "observations.json", json.dumps(
                    {"as_of": result["as_of"], "status": "failed", "reason": str(exc), "batches": []},
                    ensure_ascii=False, indent=2,
                ))

            # Both paths have independent status; no observation failure can fabricate a freeze.
            try:
                progress("采集并核验当日机械候选；完整目录首次可能需要数十分钟")
                checkpoint('selection')
                # No injected client: retain screen's production clock checks.
                selection = screen(output / "screen", progress=progress)
                if selection["status"] not in {"complete", "partial"}:
                    raise ValueError("今日机械候选未就绪：" + "；".join(selection.get("warnings", [])[-2:]))
                codes = [r["ts_code"] for r in selection["candidates"]]
                checkpoint('context')
                try:
                    context = fetch_context(codes, client, datetime.now(SHANGHAI))
                except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
                    context = {"evidence": [], "coverage": {}, "warnings": ["证据采集失败，AI 应为 unknown：" + str(exc)]}
                atomic_text(output / "context.json", json.dumps(context, ensure_ascii=False, indent=2))
                if research:
                    try:
                        v2 = research.freeze({**selection, "coverage": context["coverage"]}, context["evidence"])
                        handoff = research.export(v2["batch_id"])
                        result["research_v2"].update(status="frozen_and_exported", freeze=v2, handoff=handoff)
                    except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
                        result["research_v2"].update(status="blocked", reason=str(exc))
                        result["notes"].append("第二版研究冻结未完成：" + str(exc))
                registered = book.freeze(
                    {**selection, "coverage": context["coverage"]},
                    datetime.now(SHANGHAI).isoformat(), context["evidence"],
                )
                result.update(candidate_count=len(codes), freeze=registered, source_coverage=context["coverage"])
                result["phases"]["selection"] = selection["status"]
                result["notes"].append(f"已核验 {len(codes)} 个当日候选；冻结状态：{registered['status']}。")
                result["notes"] += context["warnings"][:8]
            except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
                result["phases"]["selection"] = "failed"
                result["notes"].append("当日新增候选/证据冻结失败：" + str(exc))
            # Freeze today's packet before potentially expensive common-pool observations.
            # Failure of today's screen does not suppress observation of existing v2 batches.
            if research:
                try:
                    checkpoint("research_v2_observation")
                    research_output = output / "research-v2-observation"
                    research_output.mkdir(exist_ok=True)
                    observation = observe_existing(research, client, benchmark, result["as_of"], research_output)
                    atomic_text(research_output / "receipts.json", json.dumps(client.receipts, ensure_ascii=False))
                    research.event("source_acquisition", "partial" if observation["data_errors"] else "complete",
                                   {"as_of": result["as_of"], "receipts": client.receipts, "data_errors": observation["data_errors"]})
                    result["research_v2"]["observation"] = observation
                except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
                    result["research_v2"]["observation"] = {"status": "failed", "reason": str(exc)}
                    research.event("observation", "failed", {"reason": str(exc)})
            result["status"] = "complete" if all(v == "complete" for v in result["phases"].values()) else (
                "partial" if "freeze" in result or result.get("watch_count", 0) or result.get("research_v2", {}).get("freeze") else "blocked"
            )
            if result.get("research_v2", {}).get("status") == "blocked" and result["status"] == "complete":
                result["status"] = "partial"
                result["notes"].append("旧版路径有结果，但第二版研究被阻断；两者不得混称完成。")
            v2_observation = result.get("research_v2", {}).get("observation", {})
            if result["status"] == "complete" and (v2_observation.get("status") == "failed" or v2_observation.get("data_errors") or v2_observation.get("observation_issue_count")):
                result["status"] = "partial"
                result["notes"].append("第二版历史观察不完整，价格缺失仍为未知。")
            if result.get("observation_issue_count"):
                result["notes"].append(
                    f"{result['observation_issue_count']} 个对照观察因数据无法判定；完整原因见本次 observations.json。"
                )
            result["notes"] += [
                "既有冻结事实已保留；没有修改个人资金、持仓或主策略参数。",
                "1/5/20 日价格观察尚未到期时记为等待；缺失数据保留未知样本。",
            ]
        except (ValueError, RuntimeError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
            result["status"] = "blocked"
            result["notes"].append(str(exc))
        finally:
            result["lab"] = book.summary()
            if research:
                try:
                    result.setdefault("research_v2", {})["report"] = research.report()
                finally:
                    research.close()
            if client:
                atomic_text(output / "receipts.json", json.dumps(client.receipts, ensure_ascii=False))
                client.close()
            book.close()
        result["created_at"] = datetime.now(SHANGHAI).isoformat()
        result["runner_hash"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        return write_report(folder, output, result)
