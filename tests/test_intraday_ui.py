import shutil
import subprocess
from pathlib import Path

import pytest


def test_actual_browser_alert_helpers_handle_null_plan_identity_and_expiry():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser helper validation")
    path = Path(__file__).parents[1] / "src/ashare_agent/web/intraday.html"
    html = path.read_text(encoding="utf-8")
    helpers = html[html.index("const alertTime ="):html.index("const setMessage =")]
    script = r"""
const assert = require('node:assert/strict');
const list = value => Array.isArray(value) ? value : [];
const state = {};
""" + helpers + r"""
const at = new Date().toISOString();
const latest = {id:'tick',plan_version:null,status:'monitoring',checked_at:at,
 valid_until:new Date(Date.now()+30000).toISOString(),entries:[],holdings:[{ts_code:'600000.SH',trigger:'止损触发',quantity:0}]};
const value={running:true,latest,confirmation:{id:'ack'}};
const alert={id:'alert',tick_id:'tick',confirmation_id:'ack',plan_version:null,
 ts_code:'600000.SH',kind:'止损触发',quantity:0,created_at:at};
assert.equal(currentAlert(alert,value),true,'null-plan holding alerts must work');
assert.equal(currentAlert({...alert,confirmation_id:'old'},value),false);
assert.equal(currentAlert({...alert,tick_id:'old'},value),false);
assert.equal(currentAlert({...alert,quantity:100},value),false);
assert.equal(currentAlert(alert,{...value,stopping:true}),false);
const expired={...latest,valid_until:new Date(Date.now()-1000).toISOString(),holdings:[{quantity:100}]};
assert.equal(staleLatest(expired).holdings[0].quantity,0);
console.log('browser identity and expiry helpers passed');
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
