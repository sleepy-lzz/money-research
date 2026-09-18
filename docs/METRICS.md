# 指标口径

`ashare_agent.analytics.compute_metrics` 使用每日权益快照计算回测指标。金额单位为 CNY，收益按小数保存（例如 `0.1` 表示 10%），未定义的指标为 JSON `null`。

## 权益与收益

`initial_cash` 是首个 `equity` 行之前的现金基线。首个权益日的收益为 `equity[0] / initial_cash - 1`，之后按相邻权益日计算。CAGR（单独标为年化）使用样本交易日数除以 252 的年化期数。`annual_return`/`annual_returns` 是观测样本覆盖到的年度区间收益；`period_coverage` 提供样本起止、交易日数及覆盖标签。没有权威交易日历证明，即使具备 1 月 1 日和 12 月 31 日端点也只标为 `full_year_coverage_unverified`；其他情况为 `partial_year`，`full_year_verified` 保持 false。

最大回撤以 `initial_cash` 和权益序列共同形成的历史高点计算；回撤值为负数，另提供绝对值字段。持续时间以连续低于历史高点的权益交易日计数。Sharpe 使用零无风险利率、日收益样本标准差和 `sqrt(252)`；Sortino 使用日下行平方均值；零方差、无下行收益或样本不足时返回 `null`。Calmar 为 CAGR 除以最大回撤绝对值，无回撤时为 `null`。

## 成本、换手与交易

交易成本是 fills 的 `fee` 之和。成交金额是 `abs(quantity) * price` 之和，换手率为成交金额除以 `initial_cash`。`realized_pnl` 非空的成交行被视为 `closed-fill`；按引擎约定，它是含买入和卖出手续费的 FIFO 平仓净额，买入行通常为 `null`，不会被当作亏损。`closed_fill_win_rate` 是盈利 closed-fill 数除以所有非空平仓数，`closed_fill_profit_factor` 是盈利总额除以亏损总额的绝对值。没有平仓成交时，两项均为 `null`。`trade_count`、`win_rate`、`profit_factor` 等旧字段仍保留作兼容，但已标为 deprecated，语义仍是 closed-fill 而非交易级 round-trip；position/round-trip episode 统计尚未实现。现金分红和送转股属于独立账本事件；送转产生的新股按零新增取得成本处理，不从平仓字段推导。

## 基准与阶段归因

基准优先使用首个权益日之前最近的基准交易日作为锚点，因此首日基准收益包含这一天到首个权益日的变化。若没有前一日但有同日基准，锚点使用同日值，首日基准日收益为 `null`；若两者均不存在，基准指标为 `null`。多标的基准输入按字典序选择第一个 `ts_code`，以保持结果确定。

阶段归因把从昨日到今日的实现收益标记为该日权益行的 `regime`。引擎在该行保存前一日已经确定的 regime，避免用今日收盘标签解释今日已经发生的收益。首个权益日没有已知前一日状态，因此不进入阶段归因。

## Rolling out-of-sample 窗口

`walk_forward_windows` 使用固定规则滚动布局：先取 `train_sessions` 个历史交易日预热，再取 `test_sessions` 个交易日 OOS 测试；不执行训练或参数拟合。CLI 名称为 `rolling-oos`，旧 `walk-forward` 名称保留兼容。每次前进一个完整测试块，测试块互不重叠；末尾不足 `test_sessions` 的窗口直接剔除。下一窗口的历史部分只使用当时已经结束的日期，窗口之间的 OOS 权益不得拼接成一条可交易净值曲线。

报告还会明确标注合成数据、日频近似成交，并声明不宣称历史 AI Alpha。HTML 报告只包含内联 CSS 和 SVG，不依赖远程资源。

报告显示时，总收益、CAGR、基准/超额收益、回撤、换手率、暴露和胜率按百分比展示；Sharpe、Sortino、Calmar 和盈亏比保持倍数显示，避免把 `profit_factor=0.4448` 误读成 44.48%。订单、成交、持仓和权益主表提供可读字段，完整原始记录放在可展开的 JSON 区块中。
