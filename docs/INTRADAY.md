# 盘中观察页

盘中观察页位于工作台导航“盘中监控”，帮助监测已有买卖条件，不连接券商、不提交订单，也不把提醒当作成交记录。

## 怎么使用

1. 再次双击“启动工作台.cmd”打开新版后台。先在“持续交易计划”录入实际资金和全部持仓。
2. 买入提醒需要上一交易日形成的新版有效收盘计划；原计划为 0 股的候选不会在盘中变成可买。更新后旧计划规则哈希失效，需要重新做收盘复核。周五形成的计划对应下一个实际交易日。
3. 在“盘中监控”填冻结资金、其他资产（没有填 0）和每只股票的实际可卖数量，明确核对持仓与成本/除权情况，点击开始。缺少收盘计划时仍可开始观察持仓，但旧持仓缺少经过核验的价格基准时不会给出卖出股数。
4. 连续竞价时后台约每 30 秒检查一次；实际间隔包含接口请求耗时。监控时间为交易日 09:30–11:30、13:00–14:57，不包含集合竞价、午休和休市。网页显示行情时间、触发条件、数量、止盈止损及数据缺口。
5. 点击“授权浏览器提醒”可接收该页面新收到的当前条件提醒。网页标签页在后台也可接收，但浏览器可能节流或拦截；关闭网页后后台仍保存检查与提醒，浏览器通知停止。退出后台进程后监控停止，重新启动不会自行恢复。
6. 实际成交后到持续计划页同步现金和持仓，停止监控、重新核对可卖数量再开始。界面不会知道券商的实际成交。

同一次账户确认的首批正买入提醒固定候选分配上限；后续价格变化不会把已用预算再分给新候选。股数重新计算时同时受现金、当前持仓市值、组合风险和原计划上限约束。T+1 或可卖数为 0 时仍可显示止损触发，但不会给出可卖股数。

程序保留双源原始回执、实际获取时间、每轮检查和去重提醒，存在本地 `runtime/current-cache` 与 `runtime/planner/plans.sqlite`。这些记录仅证明本工具当时收到的公共报价，不证明可成交、完整盘口或历史 PIT 数据。

## 接口契约

开发接口遵循同源 Origin 和 CSRF 检查，写请求带 `X-CSRF-Token`。

页面读取 `GET /api/intraday/state`。响应至少包含：

```json
{
  "intraday_api_version": 1,
  "csrf": "…",
  "running": false,
  "interval_seconds": 30,
  "market_status": "连续竞价",
  "account": {"profile": {}, "positions": [], "revision": 1},
  "confirmation": {"id": "…"},
  "latest": {
    "id": "…",
    "status": "waiting",
    "checked_at": null,
    "valid_until": null,
    "entries": [],
    "holdings": [],
    "reasons": [],
    "plan_version": null,
    "quote_receipts": []
  },
  "alerts": [],
  "last_error": null
}
```

`entries` 和 `holdings` 的每条记录使用 `ts_code/name/price/observed_at/status/quantity/entry_low/entry_high/stop_price/take_profit_price/reasons/trigger/available_quantity` 字段。页面会把缺失字段显示为空值；所有用户输入都使用 DOM `textContent` 或属性赋值渲染。

开始观察使用 `POST /api/intraday/start`，请求体为：

```json
{
  "expected_account_revision": 1,
  "positions_complete": true,
  "corporate_actions_checked": true,
  "frozen_cash": "0.00",
  "other_assets": "0.00",
  "sellable_quantities": {"600000.SH": 0}
}
```

停止观察使用 `POST /api/intraday/stop`，请求体为空对象。页面每 5 秒读取状态；后端观察间隔为 30 秒。关闭页面不会停止后台观察，直到服务退出或收到停止请求。

## 使用边界

冻结现金、其他资产和每个持仓代码的实际可卖数量必须显式填写；没有其他资产时填写 `0`。页面只校验这些输入、账户 revision、持仓数量上限和两个确认框，不推断收盘计划字段；后台负责决定计划是否有效以及买入数量。没有可用收盘计划时仍可观察已有持仓。持仓完整性、成本及除权资料两个确认框默认不勾选。账户 revision 变化后页面会取消确认，必须先停止，再重新核对资料并启动。

浏览器通知只有在用户点击授权按钮、页面仍打开、观察仍处于 `running` 且最新检查为 `monitoring` 时才会触发；新增提醒还必须在 90 秒内且不来自未来，并同时匹配当前检查 `tick_id == latest.id`、当前确认 `confirmation_id == confirmation.id`、`plan_version`、代码、触发种类和 JSON 整数数量。买入、卖出和数量为 0 的提醒都可出现。停止请求一提交，当前数量立即失效；服务返回 `stopping:true,running:true` 时页面会显示“停止中”，直到后续 GET 明确返回 `running:false`。首载的历史提醒、停止期间的提醒和过期数量只标为“历史条件（非当前执行数量）”，不会弹出；后台标签页仍可接收已授权的浏览器通知。没有微信、邮件或其他系统推送。

页面只接受 `intraday_api_version == 1` 的状态。状态请求单飞并在约 10 秒超时；连接失败会清空最新条件、将数量显示为 `0/stale` 或空状态，并标记连接未知，恢复成功后才重新允许启动。

免费行情轮询可能延迟、缺失或受接口限制，不保证成交。页面不下单；止盈止损和入场区间只是观察条件。新闻沿用收盘证据，不是实时新闻，不能替代人工核验。
