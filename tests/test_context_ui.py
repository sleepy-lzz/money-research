import shutil
import subprocess
from pathlib import Path

import pytest


def test_actual_context_renderers_show_unknowns_and_safe_source_links():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for page renderer validation")
    html = (Path(__file__).parents[1] / "src/ashare_agent/web/planner.html").read_text(encoding="utf-8")
    funcs = html[html.index("function renderFinancialContext("):html.index("function renderLatest()")]
    safe = html[html.index("function safeHttpUrl("):html.index("function activeJobId(")]
    script = r'''
const assert = require('node:assert/strict');
const panels = {};
const node = (tag, cls='', content='') => ({tag,textContent:content,children:[],append(...items){this.children.push(...items);}});
const $ = id => panels[id] || (panels[id] = node('div'));
const clear = item => {item.children=[];};
const text = (value, fallback='—') => value === null || value === undefined || value === '' ? fallback : String(value);
const fmt = value => String(value);
const escDate = text;
const notice = content => node('p','',content);
const window = {location:{origin:'http://127.0.0.1'}};
''' + safe + funcs + r'''
const flatten = item => [item,...item.children.flatMap(flatten)];
renderFinancialContext(null); assert(flatten($('financial-context')).some(n=>n.textContent.includes('尚无')));
renderFinancialContext({schema_version:1,coverage:{A:{status:'missing'}},evidence:[{metrics:{cash:null},url:'javascript:alert(1)'}],metric_definitions:{cash:{label:'每股经营现金流',unit:'元/股'}}});
assert(flatten($('financial-context')).some(n=>n.textContent === '每股经营现金流：未知'));
assert(!flatten($('financial-context')).some(n=>n.tag==='a'));
renderMarketContext({schema_version:1,coverage:{},scenarios:{},evidence:[{title:'<img onerror=alert(1)>',topics:[],url:'https://example.invalid/news'}]});
assert(flatten($('market-context')).some(n=>n.tag==='summary' && n.textContent==='<img onerror=alert(1)>'));
const link=flatten($('market-context')).find(n=>n.tag==='a');
assert.equal(link.href,'https://example.invalid/news'); assert.equal(link.rel,'noopener noreferrer');
'''
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
