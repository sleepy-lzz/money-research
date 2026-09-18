"""Synthetic, isolated integration exercise. NEVER produces prospective evidence."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd

from .current_screen import rank_rows, screen_rule_hash
from .research_protocol import canonical, digest
from .session_calendar import calendar_manifest, next_sessions


class DemoClock:
    def __init__(self, value="2026-09-10T17:00:00+08:00"):
        self.value = datetime.fromisoformat(value)

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = datetime.fromisoformat(value)


def fixture_selection(day="2026-09-10"):
    calendar = deepcopy(calendar_manifest())
    for source in calendar["sources"]:
        source["fetched_at"] = day + "T15:00:00+08:00"
    rows = []
    for i in range(24):
        rows.append(dict(ts_code=f"{600000+i:06d}.SH", name=f"SYNTHETIC-{i}", close=100.0,
                         momentum20=0.10-i*0.005, momentum60=0.50-i*0.01, momentum120=0.60-i*0.01,
                         ma120_gap=0.1, amount20=200_000_000, observed_sessions=251,
                         eligibility_status="synthetic_fixture", regulatory_status="synthetic_not_real",
                         exposures_as_of=day, industry=None, industry_as_of=None, market_cap=None,
                         market_cap_as_of=None, market_beta120=0.9+i*0.01, daily_volatility60=0.02,
                         synthetic=True))
    # This stock must survive the common pool but not trend screening.
    rows[-1].update(momentum20=-0.4, momentum60=-0.3, momentum120=-0.2, ma120_gap=-0.2)
    # High momentum below MA: test the MA-only ablation separately.
    rows[-2].update(momentum60=0.9, momentum120=0.8, ma120_gap=-0.1)
    top = rank_rows([r for r in rows if r["ma120_gap"] > 0 and r["momentum60"] > 0 and r["momentum120"] > 0], 20)
    return dict(mode="CURRENT_RESEARCH", status="complete", synthetic=True, as_of=day,
                started_at=day+"T16:00:00+08:00", completed_at=day+"T16:30:00+08:00",
                source_hash=screen_rule_hash(calendar), calendar_manifest=calendar,
                parameters=dict(top=20, min_amount=100_000_000, momentum=[60,120], trend=120),
                receipts=[dict(available_at=day+"T16:20:00+08:00", fetched_at=day+"T16:20:00+08:00",
                               source="SYNTHETIC_DO_NOT_USE_AS_MARKET_EVIDENCE")], candidates=top,
                benchmark_diagnostics=dict(regime="above_ma120", as_of=day, synthetic=True),
                research_universe=dict(schema="common-eligibility-v2", trend_prefiltered=False, rows=rows,
                                       eligible_count=len(rows), data_failure_count=0, attempted_count=len(rows),
                                       source_universe_count=len(rows), execution_eligibility="synthetic_only"))


def fixture_evidence(code, *, primary=True, positive=False, risk=False, suffix="1", day="2026-09-10"):
    import hashlib
    title = "SYNTHETIC test evidence " + suffix
    content = "Synthetic text; never a real source or investment fact. " + suffix
    return dict(evidence_id=f"synthetic-{code}-{suffix}", ts_code=code, kind="news",
                quality="verified_primary" if primary else "aggregator_timestamp",
                url=("https://www.sse.com.cn/" if primary else "https://synthetic.example/")+"synthetic-only-"+suffix,
                published_at=day+"T14:00:00+08:00", available_at=day+"T16:20:00+08:00",
                fetched_at=day+"T16:20:00+08:00", title=title, content=content,
                content_hash=hashlib.sha256((title+"\n"+content).encode()).hexdigest(),
                risk_flags=["SYNTHETIC_RISK"] if risk else [], sentiment=1 if positive else 0)


def fixture_result(packet, mode, *, decisions=None, generated_at=None):
    result = {k: packet[k] for k in ("schema_version", "batch_id", "as_of", "input_hash", "prompt_hash")}
    result.update(mode=mode, generated_at=generated_at or packet["as_of"]+"T17:10:00+08:00",
                  model_identity=dict(reported_name=None, exact_version=None, identity_source="unavailable", sampling_control="unavailable"),
                  decisions=[dict(ts_code=r["ts_code"], action="unknown", reason="SYNTHETIC: insufficient evidence", evidence_ids=[]) for r in packet["candidates"]])
    for index, action, ids in decisions or []:
        result["decisions"][index].update(action=action, evidence_ids=ids, reason="SYNTHETIC deterministic branch test")
    return result


def fixture_prices(selection):
    days = next_sessions(selection["as_of"], 20, selection["calendar_manifest"])
    def frame(start, slope):
        prices=[start+slope*i for i in range(len(days))]
        return pd.DataFrame(dict(trade_date=days, open=prices, close=[v+0.1 for v in prices],
                                 high=[v+0.5 for v in prices], low=[v-0.5 for v in prices], volume=[100000]*len(days)))
    benchmark=frame(200, 0.1)
    histories={r["ts_code"]: {"raw": frame(40+i, 0.1+i*0.003), "qfq": frame(40+i, 0.1+i*0.003)}
               for i,r in enumerate(selection["research_universe"]["rows"])}
    return benchmark, histories, days


def run_demo(project_root):
    from .research_lab import ResearchLab
    # Each exercise has its own root and demo-marked database, never the real runtime.
    root=Path(project_root).resolve()/"runtime"/"research-demos"/uuid4().hex[:12]
    root.mkdir(parents=True)
    clock=DemoClock()
    book=ResearchLab(root, demo=True, clock=clock)
    try:
        selection=fixture_selection()
        codes=[r["ts_code"] for r in selection["candidates"]]
        evidence=[fixture_evidence(codes[0], risk=True), fixture_evidence(codes[5], positive=True), fixture_evidence(codes[8], positive=True)]
        batch=book.freeze(selection,evidence)["batch_id"]
        packet=book.packet(batch)
        outbox=book.export(batch)
        clock.set("2026-09-10T17:15:00+08:00")
        payloads=[fixture_result(packet,"conservative",decisions=[(0,"exclude",[evidence[0]["evidence_id"]])]),
                  fixture_result(packet,"balanced",decisions=[(5,"promote",[evidence[1]["evidence_id"]])]),
                  fixture_result(packet,"aggressive",decisions=[(8,"promote",[evidence[2]["evidence_id"]])])]
        for value in payloads:
            (Path(outbox["folder"])/"results"/(value["mode"]+".json")).write_text(canonical(value),encoding="utf-8")
        book.submit(batch,payloads)
        benchmark,histories,days=fixture_prices(selection)
        clock.set(days[-1]+"T17:00:00+08:00")
        book.observe(days[-1],benchmark,histories)
        report=book.report()
        report.update(status="synthetic_e2e_complete", synthetic=True, root=str(root), batch_id=batch,
                      real_prospective_evidence=False, filled_orders=0, source_fixture_hash=digest(selection))
        return report
    finally:
        book.close()
