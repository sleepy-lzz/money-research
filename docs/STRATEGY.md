# Frozen baseline strategy

`ashare_agent.strategy` is a deterministic, point-in-time worker. It consumes
the duck-typed `Snapshot` described in `docs/CONTRACTS.md`; it does not import
the data, ledger, broker, or execution modules.

For `as_of`, every market and benchmark row after that ISO date is discarded.
No future row is used to fill a missing observation. A security is considered
supported only on SSE or SZSE and on MAIN, using the shared execution eligibility rule. The
security table supplies listing and exclusive delisting dates. Listing age is
the count of known calendar sessions from `list_date` through `as_of`. If the
calendar is absent, the observed history is used conservatively and a caller
should treat the result as limited research evidence.

The baseline uses adjusted close (`close * adj_factor`) for the two momentum
returns and the trend average:

* `momentum60 = P[t] / P[t-60] - 1` and `momentum120 = P[t] / P[t-120] - 1`;
  each requires exactly the requested number of prior valid observations plus
  the current observation.
* `ma120` is the mean of the last 120 valid adjusted closes. The trend gate is
  strict: the latest valid adjusted close must be above that average.
* `score` is the equal-weight mean of the pandas average-rank percentiles of
  the two momentum columns. Ties receive the same average percentile; code is
  the final ascending tie-break. Only eligible rows get a positive integer
  `rank`.
* `volatility20` is the population standard deviation (`ddof=0`) of the last
  20 adjusted-close returns. `atr14` is the mean of the last 14 raw true
  ranges. `adv20` and `amount20` use the latest 20 calendar-session rows;
  explicit suspensions contribute zero liquidity instead of disappearing from the denominator.

Rows with insufficient valid history, unknown status, known ST status,
suspension, stale as-of data, low liquidity, or a failed trend gate remain in
the output with a specific `reason`. Missing data is never silently filled or
reweighted. A current suspended row may still expose its last valid close and
factors so a held position can be inspected, but it is not eligible for a new
entry.

`select_targets` retains eligible held symbols through rank 20 and adds new
symbols only on a rebalance day, from rank 10 or better. A held symbol with a
known suspension or stale/missing current observation is preserved for the
risk/execution layer; known ST, delisting, unknown status, failed trend, and
other explicit invalidations are omitted. The selector returns target weights
only, capped by `max_positions`, `max_gross`, and `max_industry`; it never
creates quantities or orders. With the default 8% target and 25% industry cap,
at most three full-weight names in one industry are selected.

`is_rebalance_day` compares `as_of` with the next known calendar session. It is
true when that next session belongs to another ISO week and false when the
next session is unavailable. `market_regime` is a report-only description of
the selected benchmark: bull requires close above MA200 and a positive 20
session return slope, bear requires both negative conditions, and mixed signals
are neutral. Its score is `+1/-1` per signal, never a probability. Fewer than
200 valid benchmark observations produce `unknown`. Missing decision-day data,
missing sessions in the required window, or not-yet-available observations also
produce `unknown`; yesterday's regime is not relabeled as today's result.

`research_candidates` is a side path. With no explicitly enabled provider it
returns strict, cached `ResearchRecord` objects marked `unavailable` and
labels them as mechanical-only. The optional HTTP provider reads its URL/key
from explicit configuration or `ASHARE_RESEARCH_API_URL` and
`ASHARE_RESEARCH_API_KEY`, applies a timeout and call budget, filters evidence
published after `as_of`, and validates every response with an extra-fields
forbidden Pydantic schema. Provider failures produce unavailable records.
Research records contain no order, quantity, weight, or execution fields and
never affect ranking or target selection. Cache keys include symbol, as-of,
model version, and prompt version.

## 不同模式的规则边界（2026-09-11）

下表是用途对照，不代表三个模式是同一个已验证赚钱的策略。精确的运行参数和源码身份以每次产物保存的 manifest 为准。

| 路径 | 数据和股票范围 | 主要规则 | 结果口径 |
| --- | --- | --- | --- |
| 历史 Snapshot / strategy | 沪深主板；需要历史库存、状态、公司行动、可见时间与日历证明 | 60/120 动量等权排序、MA120趋势；配置的选入/持有排名与风险上限；行业信息缺失不得用今日行业补历史 | 历史引擎在数据门禁通过时才可模拟；MA200市场描述仅报告用途 |
| CURRENT_RESEARCH | 新浪当前主板目录或显式代码，尚未认证交易所完整全集；腾讯/新浪交叉核验 | 独立交易日历251日连续观测、近20日成交额、MA120、60/120动量均正；Top N | 当日候选研究，非历史PIT与成交；参数和日历随结果归档 |
| PLANNER | 当日通过身份核验的选股 + 用户全部已录入持仓 | 指数MA120约束新仓；确定性资金/风险预算、条件价格、止盈止损、持有期；旧规则或账户修订计划失效 | 人工条件计划；不知券商实际成交；缺账户资金时数量为0；行业暴露限制未启用 |
| FORWARD_RESEARCH | 每日机械Top20内部的四组Top5对照，冻结后不回写 | 相同D+1开盘起点，独立日历固定D+1/5/20收盘；缺数据记unknown；规则/参数/新闻处理分版本 | 价格观察，无模拟fills、资金净值或Alpha证明 |

新闻和社区的解释、风险线索、对照实验，与默认机械排名和仓位数学分别留痕。历史回放中的证据必须受 ingestion 时间门禁约束；不能让 provider 自报时间或在线模型利用后来得知的信息补写历史判断。
