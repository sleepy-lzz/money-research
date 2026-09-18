# 0.1 本地研究版验收记录

历史记录；当前版本见 [v0.1.1 验收](ACCEPTANCE_v0.1.1.md)。

日期：2026-09-10。环境：本机 Windows、Python 3.13.3、项目 `.venv`；依赖版本保存在 `requirements-lock.txt` 和 `runtime/environment.json`。

## 已交付

- 固定规则选股：主板过滤、历史状态证据、60/120 日动量、MA120、周调仓、候选及退出原因。
- 风控与模拟执行：仓位/行业/现金限制、T+1 批次、费用日期分段、停牌/涨跌停限制、次日开盘近似成交。
- SQLite 资金与股份账本：订单预留、分笔回报、重复回报保护、整日事务、重启恢复、分红应收与支付、红股可卖日期。
- 数据快照及 Tushare 适配：Parquet、哈希、增量缓存、历史时点证据、未知关键数据拒绝正式模拟。
- 研究侧接口：机械说明默认可用；可选带来源的 HTTP 研究协议、缓存和预算，研究内容与订单隔离。
- 回测、逐日 Paper 重放、滚动窗口、成本/容量情景、HTML/Markdown/JSON 报告、命令行及复现脚本。

## 已验证

完整测试命令 `.venv\Scripts\python.exe -m pytest -q`：**48 passed in 136.06s**。

离线验收脚本已完整退出，退出码为 0；12 个命令步骤及一致性断言全部通过，`runtime/acceptance/summary.json` 记录 `status: passed`。

`ruff check src tests scripts`、Python 编译检查通过。HTML 报告已在本机浏览器检查：权益图、表格、折叠 JSON 正常；页面宽度与视口一致，宽表使用局部滚动；Sharpe、利润因子和百分比单位已分别核验。

| 验证项 | 实际结果 |
| --- | --- |
| 快照校验 | 16 个合成证券、320 个合成交易日；结构和哈希校验通过 |
| 固定回测 | 2025-09-25 至 2025-12-31，70 日、65 笔模拟成交 |
| 回测重复运行 | 完整 `run.json` 一致，未重复成交 |
| 逐日 Paper 重放 | 2025-09-26 冻结意图，2025-09-29 产生 10 笔成交 |
| Paper 重复运行 | 订单、成交、余额及返回结果保持一致 |
| 滚动窗口 | 252 日历史窗口、21 日评估窗口，得到 3 个完整窗口；末尾 5 日不单独成窗 |
| 压力情景 | baseline、cost_x2、capacity_half 均运行并保存独立结果 |
| 报告重建 | 从已保存 `run.json` 重新生成 HTML、Markdown、JSON |
| 真实数据能力检查 | `token_configured:false`、`network_checked:false` |

账本测试包含手工核算：10,000 元账户买入 100 股、单价 10 元，买入费 5.01 元；次日按 11 元卖出、卖出费 5.56 元，最终现金 10,089.43 元，净平仓盈亏 89.43 元。另验证新旧仓 T+1、最低佣金不按分笔重复收取、分红送股权益连续性、事务回滚和冲突回报拒绝。

引擎验证包含：未来价格变化不改变过去订单；回测与分段重启的逐日模拟产生相同成交和权益；拒绝跳过交易日、改写已处理历史、混用回测/前瞻/重放账本，以及用较新账户生成旧时点报告。

## 复现与结果路径

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe scripts/verify_offline.py --output runtime/acceptance
```

脚本实际调用公开 CLI，保留每一步标准输出及错误日志。每轮验收创建新的 Paper 测试账本，并在本轮内重复执行末日以验证幂等。修改过交易源码时需换一个新的输出目录，不能延续旧版本账本。

- 主报告：`runtime/acceptance/backtest/report.html`
- 完整回测结果：`runtime/acceptance/backtest/run.json`
- 回测账本：`runtime/acceptance/backtest/account.sqlite`
- 滚动评估：`runtime/acceptance/walk-forward/walk-forward.json`
- 情景对照：`runtime/acceptance/robustness/robustness.json`
- 命令日志：`runtime/acceptance/logs/`
- 汇总与本轮 Paper 路径：`runtime/acceptance/summary.json`

本次交易源码指纹：`8b62795026a6e11c3f55b4b4e2e6406f45bccd9611971a0299fd2d3d53bf3218`。配置和历史输入指纹随 `run.json` 保存。

## 仍待验证及后续范围

1. **真实历史数据和策略有效性未验证。** 本次全部收益、成交和压力结果来自合成数据，仅验证软件流程。合成日历按工作日生成，不是真实交易所节假日历。
2. **真实 Tushare 端到端未验证。** 尚未配置 Token；接口权限、全市场历史覆盖、停牌缺行、供应商修订、公司行为证据须用真实数据核对。字段缺失不会自动补成可交易状态。
3. **真实模型调用未验证。** 当前提供通用 HTTP 研究适配协议；具体模型服务仍需实际地址、凭据和带时间来源材料。历史重放禁用外部模型，不宣称历史 AI Alpha。
4. **生产运行与实盘不在此版本内。** 没有注册后台任务，没有连接财通/QMT。仅支持已说明的主板日频近似撮合和现金/整数送转，公司行为税补扣、配股/合并换股/退市处置仍需专门支持。
5. **全市场规模性能、长期前瞻运行及跨平台 CI 未验收。** GitHub Actions 配置已提供，但尚未在远端实际运行。此版本不构成可直接实盘部署的验收。
