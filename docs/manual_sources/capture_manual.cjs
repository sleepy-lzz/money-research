// Render the shipped HTML with isolated, explicitly illustrative API fixtures.
// No broker calls, account writes, source refreshes, or live notifications.
const fs = require('fs');
const path = require('path');
const {chromium} = require('C:/Users/31227/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = process.cwd();
const out = path.join(root,'tmp/pdfs/manual/screens');
fs.mkdirSync(out,{recursive:true});
const stamp = '2026-09-11T16:35:00+08:00';
const profile = {capital:'100000',cash:'90000',risk_pct:'.005',max_position_pct:'.1',max_gross_pct:'.6',portfolio_risk_pct:'.02',stop_pct:'.06',reward_r:'2',max_hold_sessions:20,entry_band_pct:'.01',weights:{mechanical:'.7',market:'.2',news:'.1',community:'0'}};
const positions = [{ts_code:'600000.SH',quantity:1000,cost_price:'10.00',buy_date:'2026-09-10',stop_price:'9.40',take_profit_price:'11.20'}];
const macro = {schema_version:1,as_of:stamp,scope:'近7日有限来源样本，不代表全市场新闻覆盖',decision_mode:'参考，不参与排名、股数、止损或历史回测',warnings:[],coverage:{a:{topic:'energy',source:'eastmoney',status:'sample_available',accepted:1,rejected:0}},scenarios:{energy:{label:'能源与地缘风险',channels:'上游收入与下游成本',positive:'若能源价格下降，部分下游成本可能改善。',negative:'若能源成本上涨，部分下游利润可能承压。'}},evidence:[{title:'操作示例 能源价格变化与产业影响',source_label:'东方财富聚合检索',verification:'聚合报道；事实待核实',published_at:'2026-09-11T15:10:00+08:00',fetched_at:stamp,available_at:stamp,content:'此条为手册操作示例，不对应实际新闻。请分别核对事件、来源、预期及公司业务暴露。',topics:['energy'],url:'https://example.invalid/illustration'}]};
const financial = {schema_version:1,as_of:stamp,scope:'每股最多展示4期可校验财务摘要，非完整三大报表',decision_mode:'仅参考；未核对原始公告，不参与选股权重',warnings:['利润为报告期累计，非单季、非TTM；示例数值不代表真实公司。'],coverage:{'600000.SH':{status:'sample_available',accepted:1}},metric_definitions:{TOTALOPERATEREVE:{label:'营业总收入',unit:'元'},PARENTNETPROFIT:{label:'归母净利润',unit:'元'},MGJYXJJE:{label:'每股经营现金流',unit:'元/股'},ROEJQ:{label:'加权净资产收益率',unit:'%'}},evidence:[{ts_code:'600000.SH',name:'演示公司',report_period:'2026-06-30',report_type:'中报',org_type:'演示口径',basis:'按报告期累计，非单季、非TTM',source_announced_date:'2026-08-15',source_updated_date:'2026-08-15',fetched_at:stamp,available_at:stamp,metrics:{TOTALOPERATEREVE:'100000000',PARENTNETPROFIT:'5000000',MGJYXJJE:null,ROEJQ:'6.5'}}]};
const latest = {version_id:'manual-example-only',created_at:stamp,as_of:'2026-09-11',status:'partial',profile,warnings:['操作示例：以下为虚构账户与结果，不用于投资。'],entries:[{ts_code:'000001.SZ',name:'演示候选',status:'观察',entry_low:'9.90',entry_high:'10.10',quantity:0,score_low:.5,score_high:.6,max_hold_sessions:20,stop_price:'9.49',take_profit_price:'11.32',risk_amount:'0',reasons:['示例数据，不允许执行']}],holdings:[{ts_code:'600000.SH',quantity:1000,sell_quantity:0,close:'10.00',stop_price:'9.40',take_profit_price:'11.20',held_sessions:1,action:'持有',reason:'操作示例，复核价格与持仓'}],evidence:[],market_context:macro,financial_context:financial,factors:{market:1,market_label:'示例市场状态',weights:profile.weights},changes:[],account_check:{status:'unconfirmed',new_positions_allowed:false,reasons:['示例尚未确认'],confirmation:null}};
const planner = {planner_api_version:3,csrf:'manual-fixture',profile,positions,latest,history:[],account_revision:1,account_check:{status:'unconfirmed',reasons:['尚未确认账户快照'],confirmation:null},active_job:null};
const intraday = {intraday_api_version:1,csrf:'manual-fixture',running:false,stopping:false,interval_seconds:30,market_status:'连续竞价',account:{profile,positions,revision:1},confirmation:null,latest:null,alerts:[],last_error:null};
const paper = {paper_api_version:1,csrf:'manual-fixture',active_job:null,accounts:[{account_id:'manual-demo',name:'手册虚拟账户'}],snapshots:[{snapshot_id:'demo-v2-320-42',synthetic:true}]};
const paperAccount={name:'手册虚拟账户',status:'waiting_data',cash:'100000',sessions_count:0,last_session:null,metrics:{total_return:null},positions:[],orders:[],fills:[],reasons:['演示截图：合成数据不能用于真实前瞻模拟。等待可信Snapshot。']};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const context=await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:1.5,locale:'zh-CN'});
 const page=await context.newPage(); const errors=[]; page.on('pageerror',e=>errors.push(String(e)));
 await page.route('**/*',async route=>{
   const url=new URL(route.request().url()); const pathname=url.pathname;
   if(pathname.startsWith('/api/')){
     let data = pathname==='/api/planner/state'?planner:pathname==='/api/intraday/state'?intraday:pathname==='/api/paper/state'?paper:pathname.startsWith('/api/paper/accounts/')?paperAccount:{app:'ashare-workbench',backend_version:6,csrf:'manual-fixture',active_job:null,last_job:null,jobs:[]};
     return route.fulfill({json:data});
   }
   const filename = {'/':'index.html','/planner':'planner.html','/intraday':'intraday.html','/paper':'paper.html'}[pathname];
   if(filename)return route.fulfill({contentType:'text/html; charset=utf-8',body:fs.readFileSync(path.join(root,'src/ashare_agent/web',filename))});
   return route.fulfill({status:404,body:'manual fixture'});
 });
 async function go(url){await page.goto('http://manual.local'+url);await page.waitForTimeout(350);}
 async function shot(name,locator){await locator.screenshot({path:path.join(out,name+'.png')});}
 await go('/'); await shot('01_home',page.locator('body'));
 await page.locator('[data-tab="doctor"]').click(); await shot('02_doctor',page.locator('#panel-doctor'));
 await page.locator('[data-tab="demo"]').click(); await shot('03_demo',page.locator('#panel-demo'));
 await go('/planner');
 await shot('04_profile',page.locator('#profile-form').locator('..'));
 await shot('05_positions',page.locator('#position-form').locator('..'));
 await shot('06_confirmation',page.locator('#account-confirmation-form').locator('..'));
 await shot('07_plan',page.locator('#entries-table').locator('..'));
 await page.locator('#market-context details').first().evaluate(e=>e.open=true);
 await shot('08_macro',page.locator('#market-context').locator('..'));
 await page.locator('#financial-context details').first().evaluate(e=>e.open=true);
 await shot('09_financial',page.locator('#financial-context').locator('..'));
 await shot('10_evidence',page.locator('#evidence-form').locator('..'));
 await go('/intraday'); await shot('11_intraday',page.locator('#start-form').locator('..'));
 await go('/paper'); await shot('12_paper',page.locator('main'));
 await shot('12a_paper_setup',page.locator('.grid'));
 await shot('12b_paper_state',page.locator('#account-panel'));
 fs.writeFileSync(path.join(out,'capture.json'),JSON.stringify({source:'shipped HTML + isolated illustrative responses',private_data_used:false,errors},null,2));
 await browser.close(); if(errors.length)throw Error(errors.join('\n')); console.log('Captured 14 production UI illustrations; 12 selected for the manual');
})().catch(e=>{console.error(e);process.exit(1)});
