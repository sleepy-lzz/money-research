"""SYNTHETIC counterexamples. No fixture here is real prospective evidence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest
from test_current_screen import _assess_fixture, _FakeCurrentClient, _history
from test_webapp import running_server  # noqa: F401

from ashare_agent.current_screen import assess, qualify, rank_rows, screen
from ashare_agent.research_demo import DemoClock, fixture_evidence, fixture_prices, fixture_result, fixture_selection, run_demo
from ashare_agent.research_lab import ResearchLab
from ashare_agent.research_protocol import (MODE_RULES, MODES, PROTOCOL, build_arms, canonical, select_ai,
                                            strict_loads, usable_evidence, validate_pool, validate_review)
from ashare_agent.research_statistics import paired_block_interval, summarize_exposures
from ashare_agent.session_calendar import sessions


@pytest.fixture
def study(tmp_path):
    clock=DemoClock()
    lab=ResearchLab(tmp_path,demo=True,clock=clock)
    selection=fixture_selection()
    codes=[r['ts_code'] for r in selection['candidates']]
    evidence=[fixture_evidence(codes[0],risk=True),fixture_evidence(codes[5],positive=True),
              fixture_evidence(codes[8],positive=True)]
    ident=lab.freeze(selection,evidence)['batch_id']
    clock.set('2026-09-10T17:15:00+08:00')
    try:
        yield lab,clock,selection,evidence,ident,lab.packet(ident)
    finally:
        lab.close()


def test_negative_momentum_and_below_ma_survive_qualification():
    days,raw,_,quote,cutoff=_assess_fixture()
    raw[['open','close','high','low']]=raw[['open','close','high','low']].iloc[::-1].to_numpy()
    for key in ('open','close','high','low','volume','amount'):
        quote[key]=float(raw.iloc[-1][key])
    common,reason=qualify('sh600000',quote,raw,raw.copy(),days,'Demo',100_000_000,cutoff)
    assert common and reason is None
    assert common['momentum60']<0 and common['momentum120']<0 and common['ma120_gap']<0
    assert assess('sh600000',quote,raw,raw.copy(),days,'Demo',100_000_000,cutoff)[0] is None


def test_real_screen_call_chain_keeps_nontrend_pool(tmp_path,monkeypatch):
    # Isolate Parquet I/O ONLY here; separate full suite records the missing dependency.
    # This is a synthetic selection-logic test, not Parquet or market-source acceptance.
    monkeypatch.setattr(pd.DataFrame,'to_parquet',lambda *a,**k:None)
    days=sessions('2025-01-01','2026-09-11')[-260:]
    raw=_history(days,offset=5)
    raw[['open','close','high','low']]=raw[['open','close','high','low']].iloc[::-1].to_numpy()
    fake=_FakeCurrentClient(raw,raw.copy())
    result=screen(tmp_path/'synthetic',symbols='000001.SZ',client=fake,
                  cutoff=DemoClock('2026-09-11T17:00:00+08:00')())
    assert result['status']=='complete'
    assert result['candidates']==[]
    assert result['research_universe']['eligible_count']==1
    assert result['research_universe']['rows'][0]['momentum60']<0
    assert not list(tmp_path.rglob('*.sqlite'))


def test_mechanical_formula_and_main_order_preserved(study):
    lab,_,selection,_,_,packet=study
    original=selection['candidates']
    top,arms=build_arms(selection)
    assert [r['ts_code'] for r in top]==[r['ts_code'] for r in original]
    for row in top:
        assert row['score']==next(x['score'] for x in original if x['ts_code']==row['ts_code'])
    assert len(arms['common_equal_weight']['codes'])==24
    assert '600023.SH' in arms['common_equal_weight']['codes']
    assert '600023.SH' not in arms['mechanical']['codes']
    assert arms['reversal20']['codes'][0]=='600023.SH'
    assert arms['without_ma120']['codes'][0]=='600022.SH'
    assert packet['input_hash']==lab.packet(packet['batch_id'])['input_hash']


@pytest.mark.parametrize('problem',['absent','prefilter','duplicates','negative_amount','short_history','nan_feature','mismatched_top20'])
def test_pool_and_baseline_binding_rejects_counterexamples(problem):
    selection=fixture_selection()
    pool=selection['research_universe']
    if problem=='absent': del selection['research_universe']
    elif problem=='prefilter': pool['trend_prefiltered']=True
    elif problem=='duplicates': pool['rows'][1]['ts_code']=pool['rows'][0]['ts_code']
    elif problem=='negative_amount': pool['rows'][0]['amount20']=-1
    elif problem=='short_history': pool['rows'][0]['observed_sessions']=250
    elif problem=='nan_feature': pool['rows'][0]['momentum60']=float('nan')
    else: selection['candidates'].reverse()
    with pytest.raises(ValueError): build_arms(selection)


def payloads(study):
    _,_,_,evidence,_,packet=study
    return [fixture_result(packet,'conservative',decisions=[(0,'exclude',[evidence[0]['evidence_id']])]),
            fixture_result(packet,'balanced',decisions=[(5,'promote',[evidence[1]['evidence_id']])]),
            fixture_result(packet,'aggressive',decisions=[(8,'promote',[evidence[2]['evidence_id']])])]


def test_three_modes_change_three_distinct_selection_sets(study):
    lab,_,_,_,ident,_=study
    lab.submit(ident,payloads(study))
    arms=lab.summary()['batches'][0]['arms']
    baseline=set(arms['mechanical']['codes'])
    selected=[]
    for mode in MODES:
        arm=arms['ai_'+mode]
        selected.append(frozenset(arm['codes']))
        assert set(arm['codes'])!=baseline
        assert arm['changed_selection'] and arm['effective_ai_batch']
        assert sum(arm['weights'].values())==pytest.approx(1)
        assert arm['trading_weight']==0
    assert len(set(selected))==3


@pytest.mark.parametrize('mode',['balanced','aggressive'])
def test_all_rows_respect_rank_corridor_for_random_action_patterns(study,mode):
    packet=study[-1]
    rng=np.random.default_rng(1)
    for _ in range(150):
        value=fixture_result(packet,mode)
        for item in value['decisions']:
            item['action']=rng.choice(['keep','promote','deprioritize','unknown'])
        ranking=select_ai(packet,value)['full_ranking']
        original={r['ts_code']:i for i,r in enumerate(packet['candidates'])}
        assert len(ranking)==len(set(ranking))==len(original)
        assert all(abs(i-original[c])<=MODE_RULES[mode]['max_rank_displacement'] for i,c in enumerate(ranking))
        assert ranking==select_ai(packet,value)['full_ranking']


@pytest.mark.parametrize('fault',['batch','input_hash','prompt_hash','omitted','duplicate','foreign_code','forged_evidence',
                                  'cross_evidence','extra_top','extra_decision','wrong_action','fake_sampling',
                                  'fake_identity','future_model_time','backdated_model_time','missing_reason','duplicate_evidence'])
def test_strict_import_rejects_fault_and_records_reason(study,fault):
    lab,_,_,evidence,ident,packet=study
    value=payloads(study)[0]
    d=value['decisions'][0]
    if fault=='batch': value['batch_id']='invented'
    elif fault in {'input_hash','prompt_hash'}: value[fault]='0'*64
    elif fault=='omitted': value['decisions'].pop()
    elif fault=='duplicate': value['decisions'][1]=deepcopy(d)
    elif fault=='foreign_code': d['ts_code']='600999.SH'
    elif fault=='forged_evidence': d['evidence_ids']=['invented']
    elif fault=='cross_evidence': d['evidence_ids']=[evidence[1]['evidence_id']]
    elif fault=='extra_top': value['orders']=[]
    elif fault=='extra_decision': d['quantity']=100
    elif fault=='wrong_action': d['action']='buy'
    elif fault=='fake_sampling': value['model_identity']['sampling_control']='temperature=0'
    elif fault=='fake_identity': value['model_identity']['exact_version']='unverifiable'
    elif fault=='future_model_time': value['generated_at']='2026-09-11T17:00:00+08:00'
    elif fault=='backdated_model_time': value['generated_at']='2026-09-10T16:59:59+08:00'
    elif fault=='missing_reason': d['reason']=''
    else: d['evidence_ids']*=2
    with pytest.raises(ValueError): lab.submit(ident,value)
    assert lab.db.execute('SELECT COUNT(*) FROM research_submissions').fetchone()[0]==0
    failure=lab.db.execute('SELECT status,reason FROM research_attempts ORDER BY id DESC LIMIT 1').fetchone()
    assert failure['status']=='rejected' and failure['reason']


def test_nonexistent_binding_and_invalid_json_logged(study):
    lab,_,_,_,ident,packet=study
    value=fixture_result(packet,'balanced')
    with pytest.raises(ValueError,match='frozen_batch_not_found'): lab.submit('made-up',value)
    for bad in ['{"x":1,"x":2}','{"x":NaN}','not-json']:
        with pytest.raises(ValueError,match='invalid_json'): lab.submit_text(ident,bad)
    assert lab.db.execute("SELECT COUNT(*) FROM research_attempts WHERE status='invalid_json'").fetchone()[0]==3


def test_first_valid_atomic_idempotent_conflict_and_local_receipt(study):
    lab,clock,_,_,ident,packet=study
    good=fixture_result(packet,'balanced')
    bad=fixture_result(packet,'aggressive');bad['orders']=[]
    with pytest.raises(ValueError): lab.submit(ident,[good,bad])
    assert lab.db.execute('SELECT COUNT(*) FROM research_submissions').fetchone()[0]==0
    assert lab.submit(ident,good,validate_only=True)['status']=='validated_not_submitted'
    assert lab.db.execute('SELECT COUNT(*) FROM research_submissions').fetchone()[0]==0
    first=lab.submit(ident,good)
    assert first['modes'][0]['received_at']==clock().isoformat()!=good['generated_at']
    clock.set('2026-09-11T17:30:00+08:00')
    retry=lab.submit(ident,good)
    assert retry['modes'][0]['idempotent']
    assert retry['modes'][0]['received_at']==first['modes'][0]['received_at']
    changed=deepcopy(good);changed['decisions'][0]['reason']='conflicting later response'
    with pytest.raises(ValueError,match='conflict_import'):lab.submit(ident,changed)
    assert lab.db.execute('SELECT COUNT(*) FROM research_submissions').fetchone()[0]==1


def test_late_and_backdated_claim_cannot_become_prospective(study):
    lab,clock,_,_,ident,packet=study
    value=fixture_result(packet,'balanced')
    clock.set('2026-09-10T20:00:00+08:00')
    with pytest.raises(ValueError,match='late_submission'):lab.submit(ident,value)
    assert lab.submit(ident,value,retrospective=True)['status']=='retrospective_only'
    assert lab.summary()['retrospective_count']==1
    assert lab.db.execute('SELECT COUNT(*) FROM research_submissions').fetchone()[0]==0
    arm=lab.summary()['batches'][0]['arms']['ai_balanced']
    assert arm['fallback'] and not arm['effective_ai_batch'] and not arm['changed_selection']


def test_claim_once_and_timeout_do_not_call_model(study):
    lab,clock,_,_,ident,_=study
    assert lab.claim(ident,'balanced')['status']=='claimed'
    assert lab.claim(ident,'balanced')['status'].startswith('already_claimed')
    clock.set('2026-09-10T20:00:00+08:00')
    with pytest.raises(ValueError,match='late_dispatch'):lab.claim(ident,'aggressive')


def test_ai_fallback_valid_coverage_and_actual_change_have_separate_denominators(study):
    lab,clock,_,_,ident,packet=study
    lab.submit(ident,fixture_result(packet,'conservative')) # valid syntax but all unknown
    lab.submit(ident,payloads(study)[1])
    with pytest.raises(ValueError):lab.submit_text(ident,'broken JSON')
    clock.set('2026-09-10T20:01:00+08:00')
    summary=lab.summary()
    coverage=summary['statistics']['ai_coverage']
    assert coverage['conservative']['valid_submission_rate']==1
    assert coverage['conservative']['effective_ai_batch_coverage']==0
    assert coverage['conservative']['whole_batch_fallback_rate']==1
    assert coverage['balanced']['changed_selection_rate']==1
    assert coverage['balanced']['mean_decision_fallback_rate']==pytest.approx(19/20)
    assert coverage['aggressive']['valid_submission_rate']==0
    assert coverage['aggressive']['whole_batch_fallback_rate']==1
    assert 'invalid_json' in coverage['aggressive']['failure_reasons'][0]['reason'] or 'Expecting' in coverage['aggressive']['failure_reasons'][0]['reason']
    assert summary['account_return_available'] is False


@pytest.mark.parametrize('fault',['future','hash','cross_code','community_primary','fake_primary_host','duplicate_id'])
def test_unusable_evidence_is_quarantined_not_silently_used(study,fault):
    _,_,_,evidence,_,packet=study
    item=deepcopy(evidence[0]);items=[item]
    if fault=='future':item['fetched_at']='2026-09-11T17:00:00+08:00'
    elif fault=='hash':item['content']='tampered'
    elif fault=='cross_code':item['ts_code']='600999.SH'
    elif fault=='community_primary':item['kind']='community'
    elif fault=='fake_primary_host':item['url']='https://sse.com.cn.attacker.example/x'
    else:items.append(deepcopy(item))
    accepted,quarantine,_=usable_evidence(items,{r['ts_code'] for r in packet['candidates']},packet['frozen_at'])
    assert accepted==[] and len(quarantine)==len(items)


def test_unknown_model_version_is_allowed_but_not_fabricated(study):
    packet=study[-1]
    value=fixture_result(packet,'balanced')
    assert validate_review(value,packet)['model_identity']['exact_version'] is None
    value['model_identity'].update(reported_name='UI label',identity_source='visible_interface')
    assert validate_review(value,packet)['model_identity']['identity_source']=='visible_interface'


def test_modes_have_distinct_evidence_thresholds(study):
    packet=deepcopy(study[-1])
    ev=fixture_evidence(packet['candidates'][5]['ts_code'],primary=False,positive=True)
    packet['evidence']=[ev]
    aggressive=fixture_result(packet,'aggressive',decisions=[(5,'promote',[ev['evidence_id']])])
    validate_review(aggressive,packet)
    balanced={**aggressive,'mode':'balanced'}
    with pytest.raises(ValueError,match='threshold'):validate_review(balanced,packet)
    conservative=fixture_result(packet,'conservative',decisions=[(5,'keep',[ev['evidence_id']])])
    with pytest.raises(ValueError,match='primary'):validate_review(conservative,packet)
    ev.update(kind='community',quality='community_timestamp')
    with pytest.raises(ValueError,match='community_only'):validate_review(aggressive,packet)


def test_parallel_conflict_only_one_transaction_wins(study,tmp_path):
    lab,clock,_,_,ident,packet=study
    a=fixture_result(packet,'balanced');b=deepcopy(a);b['decisions'][0]['reason']='different choice'
    def submit(value):
        other=ResearchLab(tmp_path,demo=True,clock=clock)
        try:
            return other.submit(ident,value)['status']
        except ValueError:
            return 'conflict'
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(submit,[a,b]))
    assert sorted(results)==['accepted','conflict']
    assert lab.db.execute('SELECT COUNT(*) FROM research_submissions').fetchone()[0]==1


def test_single_mode_is_supported_and_mode_set_frozen(tmp_path):
    clock=DemoClock();lab=ResearchLab(tmp_path,demo=True,clock=clock,modes=['balanced'])
    try:
        ident=lab.freeze(fixture_selection())['batch_id']
        assert list(lab._effective_arms(lab.batch(ident))).count('ai_balanced')==1
        assert 'ai_aggressive' not in lab._effective_arms(lab.batch(ident))
        with pytest.raises(ValueError,match='preregistered'):lab.export(ident,'aggressive')
    finally:lab.close()
    with pytest.raises(ValueError,match='preregistered'):ResearchLab(tmp_path,demo=True,clock=clock,modes=['aggressive'])


def test_freeze_conflict_hash_and_explicit_synthetic_isolation(study,tmp_path):
    lab,_,selection,evidence,ident,packet=study
    assert lab.freeze(selection,evidence)['status']=='already_frozen_identical'
    altered=deepcopy(selection);altered['research_universe']['rows'][-1]['momentum20']=-0.5
    with pytest.raises(ValueError,match='freeze_conflict'):lab.freeze(altered,evidence)
    assert lab.packet(ident)==packet
    with pytest.raises(ValueError,match='test_clock'):ResearchLab(tmp_path/'prod',clock=DemoClock())
    real=ResearchLab(tmp_path/'prod')
    try:
        with pytest.raises(ValueError,match='synthetic'):real.freeze(selection,evidence)
        assert real.summary()['statistics']['frozen_batch_count']==0
    finally:real.close()


def test_input_packet_export_is_immutable(study):
    lab,_,_,_,ident,packet=study
    first=lab.export(ident,'balanced');second=lab.export(ident,'balanced')
    assert first['folder']==second['folder']
    path=Path(first['folder'])/'packet.json'
    assert strict_loads(path.read_text())==packet
    path.write_text('{}')
    with pytest.raises(ValueError,match='outbox_file_conflict'):lab.export(ident)


def test_price_observation_never_becomes_nav_or_fills_and_missing_not_renormalized(study,tmp_path):
    lab,clock,selection,_,ident,_=study
    lab.submit(ident,payloads(study))
    benchmark,histories,days=fixture_prices(selection)
    clock.set(days[-1]+'T17:00:00+08:00')
    missing = dict(histories)
    del missing['600000.SH']
    lab.observe(days[-1],benchmark,missing)
    values=lab.summary()['batches'][0]['arms']['mechanical']['horizons']['5']
    assert values['avg_price_return'] is not None  # secondary known-only view, not full cohort
    assert values['full_weight_price_return'] is None
    assert values['unknown']==1
    lab.observe(days[-1],benchmark,histories)
    summary=lab.summary()
    arms=summary['batches'][0]['arms']
    assert len(arms)==8
    assert all(a['horizons']['5']['known']==len(a['codes']) for a in arms.values())
    assert all(a['horizons']['1']['is_executable_return'] is False for a in arms.values())
    assert summary['statistics']['sharpe'] is None and summary['statistics']['alpha'] is None
    assert not list(tmp_path.rglob('account.sqlite'))
    # A later failed fetch must not erase previously verified historical observations.
    lab.observe(days[-1],benchmark,missing)
    values=lab.summary()['batches'][0]['arms']['mechanical']['horizons']['5']
    assert values['full_weight_price_return'] is not None
    assert lab.report()['status']=='report_written'


def test_partial_and_all_cash_follow_preregistered_rule_and_maturity(tmp_path):
    clock=DemoClock();selection=fixture_selection()
    selection['research_universe']['rows']=selection['research_universe']['rows'][:2]
    selection['research_universe']['eligible_count']=2
    selection['candidates']=rank_rows(selection['research_universe']['rows'],20)
    evidence=[fixture_evidence(r['ts_code'],risk=True) for r in selection['candidates']]
    lab=ResearchLab(tmp_path,demo=True,clock=clock)
    try:
        ident=lab.freeze(selection,evidence)['batch_id'];packet=lab.packet(ident)
        clock.set('2026-09-10T17:15:00+08:00')
        value=fixture_result(packet,'conservative',decisions=[(i,'exclude',[ev['evidence_id']]) for i,ev in enumerate(evidence)])
        lab.submit(ident,value)
        arms=lab.summary()['batches'][0]['arms']
        assert arms['mechanical']['cash_weight']==pytest.approx(.6)
        assert arms['ai_conservative']['cash_weight']==1
        assert arms['ai_conservative']['horizons']['5']['full_weight_price_return'] is None
        benchmark,histories,days=fixture_prices(selection)
        clock.set(days[-1]+'T17:00:00+08:00');lab.observe(days[-1],benchmark,histories)
        arms=lab.summary()['batches'][0]['arms']
        assert arms['ai_conservative']['horizons']['5']['full_weight_price_return']==0
        expected=sum(arms['mechanical']['weights'][c]*float(histories[c]['raw'].iloc[4]['close']/histories[c]['raw'].iloc[0]['open']-1) for c in arms['mechanical']['codes'])
        assert arms['mechanical']['horizons']['5']['full_weight_price_return']==pytest.approx(expected)
    finally:lab.close()


def test_exposure_unknown_is_not_current_history_backfill(study):
    arms=build_arms(study[2])[1]
    result=summarize_exposures(arms['mechanical'])
    assert result['stock_HHI']==pytest.approx(.2)
    assert result['industry_known_weight']==0
    assert result['industry_weights']['unknown']==pytest.approx(1)
    assert result['weighted_log_market_cap'] is None
    assert result['weighted_beta120'] is not None
    rows=arms['mechanical']['rows']
    for row in rows:row.update(industry='Current only',industry_as_of='2026-10-01')
    assert summarize_exposures(arms['mechanical'])['industry_known_weight']==0


def test_statistics_use_dates_keep_missing_grid_and_do_not_invent_sharpe():
    small=paired_block_interval([.01]*119)
    assert small['paired_dates']==119 and small['ci'] is None
    missing=paired_block_interval([.01]*120+[None]*20)
    assert missing['date_grid_count']==140 and missing['paired_dates']==120
    assert missing['reason']=='missing_data_threshold_exceeded'
    ok=paired_block_interval([np.sin(i)*.01 for i in range(140)])
    assert ok['family_size']==4 and ok['block_sessions']==20
    assert ok['minimum_floor_is_not_sufficient_evidence']
    assert ok['ci']==paired_block_interval([np.sin(i)*.01 for i in range(140)])['ci']


def test_phase_purge_is_calendar_based_not_successful_batch_count(study):
    lab,_,selection,_,_,_=study
    calendar=selection['calendar_manifest']
    days=sessions('2026-09-10',calendar['covered_end'],calendar)
    assert lab.phase(days[39],calendar)['full_window_inside_stage'] is True
    assert lab.phase(days[40],calendar)['full_window_inside_stage'] is False
    assert lab.phase(days[60],calendar)['name']=='embargo1'


def test_demo_e2e_is_labeled_isolated_and_preserves_user_files(tmp_path):
    marker=tmp_path/'runtime/planner/plans.sqlite';marker.parent.mkdir(parents=True);marker.write_bytes(b'USER_ACCOUNT_SENTINEL')
    legacy=tmp_path/'runtime/daily-lab/observations/forward-lab.sqlite';legacy.parent.mkdir(parents=True);legacy.write_bytes(b'HISTORICAL_FREEZE_SENTINEL')
    before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (marker,legacy)}
    result=run_demo(tmp_path)
    assert result['synthetic'] and result['real_prospective_evidence'] is False and result['filled_orders']==0
    summary=json.loads(Path(result['json']).read_text())
    assert summary['synthetic'] and len(summary['batches'][0]['arms'])==8
    for p,h in before.items():assert hashlib.sha256(p.read_bytes()).hexdigest()==h
    assert not (tmp_path/'runtime/research-v2').exists()


def test_http_v2_entry_checks_csrf_and_preserves_raw_json(running_server,monkeypatch):  # noqa: F811
    server,jobs=running_server
    clock=DemoClock()
    import ashare_agent.research_lab as module
    real_cls=module.ResearchLab
    monkeypatch.setattr(module,'ResearchLab',lambda root,**kw:real_cls(root,demo=True,clock=clock,**kw))
    import ashare_agent.research_commands as commands
    monkeypatch.setattr(commands,'db_path',lambda root:Path(root)/'runtime/research-v2-demo/observations/forward-lab.sqlite')
    lab=real_cls(jobs.root,demo=True,clock=clock)
    ident=lab.freeze(fixture_selection())['batch_id'];packet=lab.packet(ident);lab.close()
    clock.set('2026-09-10T17:15:00+08:00')
    origin=f'http://127.0.0.1:{server.server_port}'
    with httpx.Client(base_url=origin,trust_env=False) as client:
        assert client.get('/research').status_code==200
        state=client.get('/api/research/state').json()
        assert client.get('/api/research/packet/'+ident).json()['input_hash']==packet['input_hash']
        headers={'Origin':origin,'X-CSRF-Token':state['csrf']}
        body={'batch_id':ident,'result_text':canonical(fixture_result(packet,'balanced'))}
        assert client.post('/api/research/import',json=body).status_code==403
        assert client.post('/api/research/validate',json=body,headers=headers).status_code==200
        assert client.post('/api/research/import',json=body,headers=headers).status_code==200
        bad={'batch_id':ident,'result_text':'{"schema_version":2,"schema_version":1}'}
        response=client.post('/api/research/import',json=bad,headers=headers)
        assert response.status_code==400 and 'duplicate_json_key' in response.text
        assert client.get('/api/research/report').status_code==200


def test_malformed_evidence_is_quarantined_instead_of_suppressing_mechanical(tmp_path):
    clock=DemoClock()
    with_book=ResearchLab(tmp_path,demo=True,clock=clock)
    try:
        batch=with_book.freeze(fixture_selection(),[{"evidence_id": []}, "SYNTHETIC_BAD_RECORD"])['batch_id']
        packet=with_book.packet(batch)
        assert len(packet['evidence_quarantine'])==2 and packet['evidence']==[]
        assert len(with_book.summary()['batches'][0]['arms']['mechanical']['codes'])==5
    finally:
        with_book.close()


def test_database_packet_tampering_is_detected(study):
    lab,_,_,_,batch,packet=study
    packet['candidates'][0]['name']='altered'
    with lab.db:
        lab.db.execute('UPDATE research_packets SET body=? WHERE batch_id=?',(canonical(packet),batch))
    with pytest.raises(ValueError,match='stored_packet_hash_mismatch'):
        lab.packet(batch)


def test_daily_v2_pipeline_survives_context_failure_and_observes_existing(tmp_path,monkeypatch):
    """SYNTHETIC orchestration E2E. Parquet serialization is explicitly not tested here."""
    from datetime import datetime
    from ashare_agent import context_feed,daily_lab,research_lab
    from ashare_agent.current_data import SHANGHAI
    clock=DemoClock()
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            value=clock()
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    selection=fixture_selection()
    benchmark,histories,days=fixture_prices(selection)
    def dated(frame):
        base=pd.DataFrame([dict(trade_date=selection['as_of'],open=100.,close=100.1,high=100.5,low=99.5,volume=100000.)])
        f=pd.concat([base,frame],ignore_index=True)
        f['amount']=f['close']*f['volume']
        f.index=f['trade_date']
        return f.loc[f.index<=clock().date().isoformat()]
    class Client:
        def __init__(self,*args): self.receipts=[]
        def close(self): pass
        def bars(self,code,adjust='',end=None):
            if code=='sh000300': return dated(benchmark),'SYNTHETIC'
            ts=code[2:]+'.SH'
            return dated(histories[ts]['qfq' if adjust else 'raw']),'SYNTHETIC'
        def quotes(self,codes):
            result={}
            for code in codes:
                f,_=self.bars(code);last=f.iloc[-1]
                result[code]=dict(name='SYNTHETIC',date=clock().date().isoformat(),observed_at=clock().date().isoformat()+'T15:30:00+08:00',**{k:float(last[k]) for k in ('open','close','high','low','volume','amount')})
            return result
    real=ResearchLab
    monkeypatch.setattr(research_lab,'ResearchLab',lambda root:real(root,demo=True,clock=clock))
    monkeypatch.setattr(daily_lab,'datetime',Clock)
    monkeypatch.setattr(daily_lab,'check_software',lambda *args:dict(passed=True,summary='SYNTHETIC fixture checks'))
    monkeypatch.setattr(daily_lab,'CurrentClient',Client)
    monkeypatch.setattr(daily_lab,'screen',lambda *a,**k:fixture_selection(clock().date().isoformat()))
    monkeypatch.setattr(pd.DataFrame,'to_parquet',lambda *a,**k:None)
    def failed_context(*args): raise RuntimeError('SYNTHETIC evidence provider outage')
    monkeypatch.setattr(context_feed,'fetch_context',failed_context)
    first=daily_lab.run(tmp_path)
    assert first['research_v2']['status']=='frozen_and_exported',first
    assert first['status']=='partial'  # legacy rejects synthetic input by design
    batch=first['research_v2']['freeze']['batch_id']
    book=real(tmp_path,demo=True,clock=clock)
    clock.set('2026-09-10T17:15:00+08:00')
    packet=book.packet(batch)
    assert packet['evidence']==[]
    book.submit(batch,[fixture_result(packet,m) for m in MODES])
    book.close()
    clock.set(days[-1]+'T17:00:00+08:00')
    second=daily_lab.run(tmp_path)
    assert second['research_v2']['status']=='frozen_and_exported',second
    book=real(tmp_path,demo=True,clock=clock)
    try:
        old=next(b for b in book.summary()['batches'] if b['batch_id']==batch)
        assert len(old['arms'])==8
        for name,arm in old['arms'].items():
            assert arm['horizons']['5']['full_weight_price_return'] is not None,(name,arm)
            assert arm['horizons']['1']['is_executable_return'] is False
        assert old['arms']['ai_balanced']['fallback'] is True
        assert Path(second['research_v2']['report']['html']).is_file()
        assert not (tmp_path/'runtime/research-v2').exists()
        assert not (tmp_path/'runtime/planner').exists()
    finally: book.close()


@pytest.mark.parametrize('field',['selection_json','arms_json'])
def test_stored_source_and_mechanical_arm_integrity(study,field):
    lab,_,_,_,batch,_=study
    row=lab.batch(batch)
    value=json.loads(row[field])
    if field=='selection_json': value['research_universe']['rows'][0]['momentum20']=0.99
    else: value['mechanical']['cash_weight']=0.7
    with lab.db: lab.db.execute('UPDATE batches SET '+field+'=? WHERE batch_id=?',(canonical(value),batch))
    with pytest.raises(ValueError,match='stored_'):
        lab.packet(batch)
