# v0.1.1 离线验收记录

日期：2026-09-10。Windows / Python 3.13.3 / 项目 `.venv`。已安装包元数据版本为 0.1.1。

## 已执行并通过

* `.venv\Scripts\python.exe -m pytest -q --maxfail=2`：**73 passed in 127.41s**。
* `ruff check src tests scripts`：通过；`ruff format --check src tests scripts`：28 个文件符合格式。
* `python -m ashare_agent rolling-oos --help`：别名入口可用。
* `python scripts/verify_offline.py --output runtime/acceptance-v0.1.1-final`：12 个命令步骤全部通过。

最终命令验收运行于日历完整性修复之后，source hash：

`ed442be2a6e0e5ef0f0876df374c379dcef61b6c17abb0ecda5a9fc1c7b85c94`

| 项目 | 结果 |
| --- | --- |
| 合成快照生成、快照 doctor、离线 doctor、select | 通过 |
| 回测 | 70 个合成交易日、65 笔 fill |
| 重复回测 | 完整结果相同 |
| 两日 paper + 重复第二日 | 10 笔 fill，幂等检查通过 |
| 保存结果再生成报告 | 通过 |
| 滚动 OOS（兼容 walk-forward 命令） | 3 个完整窗口，剔除 5 日尾窗 |
| 成本/容量对照 | baseline、cost_x2、capacity_half 全部完成 |

原始日志位于 `runtime/acceptance-v0.1.1-final/logs/`，摘要为同目录上一层的
`summary.json`，报告为 `backtest/report.html`。之前的 acceptance 目录保留为历史记录。

## 新增反例覆盖

测试覆盖 session spine 与空 OHLC、没有全天停牌证据、停牌成交额窗口、
除权日缺报价估值、持仓证券缺失、非主板排名、L/D/P 单项失败、元数据刷新失败、
分页不前进、交易日历漏日、原始文件破坏、重叠版本冲突、历史 inventory 缺项、
session 内容被改动、实际获取时间晚于历史决策、同日晚到行情、开盘证据缺失/过晚，
以及研究证据哈希/时间/URL/内容篡改和 provider 修改输入对象。

## 未验证与保留限制

* 没有真实 Tushare 下载、权限或历史资料对账；`network_checked=false`。
* inventory/session 档案来源真实性与完整性需要外部审计；合成 receipt 仅测试校验逻辑。
* 日频撮合与开盘量约束不证明队列成交、完整盘中 PIT 或实际券商费用。
* 未实现 position episode 统计、完整退市结算、完整财报修订/新闻档案服务。
* 历史 replay 禁用外部 AI provider，AI 仍是解释 sidecar；没有历史 AI Alpha 结论。
* 合成收益仅验证软件链路，不评价真实策略收益；没有真实交易。

本版修复判断和下一版优先级见 [逐项复核](HARDENING_REVIEW.md)。
