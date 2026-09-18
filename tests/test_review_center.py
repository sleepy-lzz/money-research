"""V1 is now historical-only. Strict prospective import coverage lives in test_research_v2."""
import json

import httpx
import pytest
from test_forward_lab import FREEZE_TIME, _market_data, _selection
from test_webapp import running_server  # noqa: F401

from ashare_agent.review_center import ReviewCenter


def setup(root):
    center=ReviewCenter(root)
    selection=_selection()
    ident=center.book.freeze(selection,FREEZE_TIME)['batch_id']
    return center,ident,selection


@pytest.mark.parametrize('values',[{},[],{'mode':'balanced'},[{'mode':'conservative'},{'mode':'aggressive'}]])
@pytest.mark.parametrize('save',[False,True])
def test_legacy_write_is_retired_for_all_shapes(tmp_path,values,save):
    center,ident,_=setup(tmp_path)
    try:
        with pytest.raises(ValueError,match='legacy_review_write_retired'):
            center.import_results(ident,values,save=save)
        assert center.db.execute('SELECT COUNT(*) FROM review_imports').fetchone()[0]==0
    finally:center.close()


def test_historical_freeze_and_observation_remain_readable(tmp_path):
    center,ident,selection=setup(tmp_path)
    try:
        before=tuple(center.db.execute('SELECT * FROM batches WHERE batch_id=?',(ident,)).fetchone())
        benchmark,histories,days=_market_data(selection)
        center.book.observe(days[-1],benchmark,histories,days[-1]+'T17:00:00+08:00')
        state=center.state()
        assert state['prospective_writes_retired']
        assert state['batches'][0]['arms']['mechanical']['horizons']['5']['known']==5
        assert tuple(center.db.execute('SELECT * FROM batches WHERE batch_id=?',(ident,)).fetchone())==before
    finally:center.close()


def test_legacy_http_view_and_import_retirement(running_server):  # noqa: F811
    server,jobs=running_server
    center,ident,_=setup(jobs.root);center.close()
    origin=f'http://127.0.0.1:{server.server_port}'
    with httpx.Client(base_url=origin,trust_env=False) as client:
        assert client.get('/ai-review').status_code==200
        state=client.get('/api/ai-review/state').json()
        assert client.get('/api/ai-review/packet/'+ident).json()['batch_id']==ident
        body=dict(batch_id=ident,results={})
        assert client.post('/api/ai-review/import',json=body).status_code==403
        headers={'Origin':origin,'X-CSRF-Token':state['csrf']}
        assert client.post('/api/ai-review/import',json=body,headers=headers).status_code==400
