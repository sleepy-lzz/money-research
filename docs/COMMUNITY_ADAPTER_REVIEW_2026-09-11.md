# 社区适配复核与修复

本次读取 `runtime/daily-lab/20260911-164734-8c520e8f/receipts.json`，按请求 URL/params 计算缓存键、按 revision 找到原始 response.bin，并逐个验证 SHA-256。未联网重抓或改写当天冻结数据。

## 原因与更正

20 个页面各检查前 40 行，共 800 行。686 行代码属于请求股票，55 行来自其他证券、55 行来自非个股栏目（cfhpl 或指数栏目），4 行缺少代码。个股页面确实混有其他内容，旧规则拒绝这些行是正确的。686 只表示代码归属通过，不表示全部满足时间、内容和去重规则。

上一轮将主要原因推断为市场前缀差异，没有原始响应依据，应撤回此解释。上一轮任意提取六位数字还可能把指数栏目或错误市场当成股票，因此本轮收紧为全字符串匹配：纯六位、SH/SZ 前缀或 .SH/.SZ 后缀；市场冲突、别名冲突、结构异常均拒绝，不使用标题猜测或名称模糊匹配。

## 已实现

- 社区映射区分 missing_code、invalid_code、conflicting_code、other_security、other_channel。
- warnings 按相同股票与原因合并，保留次数；warning_counts 保留完整计数。
- context.json 新增 community_diagnostics：逐股票记录 examined、accepted、rejected、outside_window、duplicate、rejection_reasons、request_status。原 coverage 的接口及至少10条有效社区证据要求保持不变。
- 社区保持无风险标记；时间证据、仓位数学及策略未修改。新解析仅影响后续请求，旧冻结不重算。

## 验证与边界

context_feed、daily_lab、planner 相关测试共 **85 passed / 4.19s**；上述源码与测试 ruff 通过。新增反例覆盖指数代码、任意文本夹码、错误交易所、别名冲突、缺码、不同股票以及警告计数与行数守恒。

原始20页仅完成代码映射审计，未将修复后解析结果回灌当日实验，未宣称全市场社区覆盖或来源内容真实性已验证。网页现有警告区会显示合并文本，详细分类在新 context.json 内；当前没有新增专用图表。历史报告仍展示旧警告，不修改其原始记录。
