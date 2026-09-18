> **当前为兼容合并候选源码，未部署到原项目，完整回归尚未通过。** 先阅读 [本次兼容核对报告](docs/COMPATIBILITY_REVIEW.md)。请用交付包的“只验证不部署”入口，不要整包覆盖生产目录，也不要把历史交付日志当成本次验收。

# A 股日频研究与模拟交易系统

**当前研究入口：两层研究 v2.0（2026-09-16）**。工作台左侧「两层研究」进入 `/research`。机械共同池、有限对照、三模式AI、冻结/本地提交/价格观察/报告已接线；旧AI复核中心改为只读历史，旧overlay-import停止写入。新研究账本独立，不重标旧样本、不接管主策略和个人账户。

[研究协议与文献适用条件](docs/RESEARCH_V2.md) · [更新/运行/恢复手册](docs/RESEARCH_V2_RUNBOOK.md) · [实际问题与取舍](docs/RESEARCH_V2_AUDIT.md) · [本次测试与边界](docs/RESEARCH_V2_ACCEPTANCE.md)。完整交易资格、真实AI效果和新实验连续账户仍未验证；软件通过不代表策略有效。

**首次使用或交接：** 优先阅读新版运行手册。原项目的24页手册属于旧界面资料，原有output目录存在时可继续查看主账户等旧功能；本源码交付不包含旧生成手册、依赖环境及个人runtime。

一个本地运行的 Python 系统：结构化数据 → 固定规则选股 → 风控 → 次日模拟订单 → 持久化账本 → 离线日报。参考 [FriesTrader](https://github.com/YizhiSong/FriesTrader) 的研究/执行分离和可审计设计。

当前版本是 **0.1.1 Data Correctness / Point-in-Time Hardening**。支持沪深主板、现金账户、只做多；不支持真实下单，不连接财通账户。没有可开启实盘的配置开关。默认演示使用合成数据，不能用其收益评价策略有效性。

本项目分为四层：研究工具、真实收盘后的前瞻价格观察、独立 Paper 账本和人工确认的持续计划。当前选股是 60/120 日动量与 MA120 的研究基线，不是成熟策略或自动荐股；A 股反转、行业和市场状态对照仍需独立前瞻实验。当前屏幕股票池也不是经过完整历史 PIT 证明的全市场股票池，不能据此宣称严格无幸存者偏差回测。新版 `research-import` 已接入独立D+1/D+5/D+20观察；旧CLI overlay只是历史存储路径，现已停写。Codex模型调用需要实际对话/本地目录权限；本次没有新增收费模型API、自动下单或主策略替换。

本版验收输出为 `runtime/acceptance-v0.1.1-final/`。运行 `./scripts/demo.ps1` 可另行生成演示报告。见 [本版验收](docs/ACCEPTANCE_v0.1.1.md) 和 [18 项复核与剩余边界](docs/HARDENING_REVIEW.md)。

真实历史数据必须有独立 inventory/session 档案及可见时间证据。今天成功下载历史行情不代表历史 PIT；材料不足会保存为研究重建快照并拒绝正式模拟。哈希验证不证明来源真实；尚未完成真实 Tushare 历史数据验收。

## 快速运行（PowerShell）

双击 **启动工作台.cmd** 打开本地网页。交易日 16:00 后点“一键真实选股”，检查数据源返回的主板目录，也可输入少量代码试用。当前研究采用腾讯日线与新浪当日报价交叉校验，无需 Tushare Token。
网页提供候选搜索/排序、CSV 导出、独立报告、取消、历史记录、网络诊断和合成演示。默认国内直连仅作用于行情客户端，不修改系统代理。
每次结果保存在独立的 `runtime/web-runs/时间戳-编号/`。见 [网页使用与架构](docs/CURRENT_RESEARCH.md)。旧快照、回测、滚动 OOS 和 Tushare 构建仍通过下述 CLI 使用。

新增 **持续交易计划** 网页：录入资金与全部持仓、明确核对收盘账户快照后，生成入场区间、风险预算内股数、止盈止损与最长持有期；余额与当日持仓市值核对不通过时，新仓数量为零。每次复核覆盖已有持仓，保留计划变化及新闻/社区证据。详见 [持续计划使用与边界](docs/CONTINUOUS_PLANS.md)。支持网页打开期间每 30 分钟复核，不提供盘中自动止损或真实下单。

新增 **模拟账户** 网页：创建单独的虚拟资金账户，检查严格 Snapshot 后推进今天，查看连续持仓、条件委托、成交及拒绝原因。现有免费日线不足以证明可执行时，账户等待数据，不生成假成交或假收益。它与手工持仓、合成演示、前瞻价格观察分别保存。详见 [模拟账户](docs/PAPER_ACCOUNTS.md)。

新增 **盘中监控** 网页：明确核对账户后，在连续竞价期间约每30秒读取新浪/腾讯双源报价，监测买入区间、止盈止损及到期条件，在原计划上限内重算股数。支持本地持久提醒和用户授权的浏览器通知；行情陈旧、源冲突、T+1或账户变化时阻断相应数量。它是有延迟的条件辅助，不是自动下单或实时新闻决策。见 [盘中使用说明](docs/INTRADAY.md)。

新增 **国内外事件与财报参考**：收盘计划中展示国内政策/经济、海外利率/贸易/能源事件和候选及持仓的财务摘要，保留来源、日期精度及真实获取时间，区分正反影响情景与已核实事实。新数据暂不参与交易权重。见 [使用与可信度说明](docs/NEWS_AND_FINANCIAL_CONTEXT.md) 和 [独立因子研究](docs/FACTOR_RESEARCH_2026-09-11.md)。

新增 **自动前瞻检验**：`python -m ashare_agent daily-lab` 自动测试、冻结当日因子对照并观察后续真实价格；`--check-only` 只检查软件。双击“查看自动复盘.cmd”打开报告。见[自动复盘说明](docs/AUTO_REVIEW.md)。价格观察不是模拟成交收益，因子实验不自动替换主策略。

旧文档曾记录工作日16:30任务及特定模型名称；本次runtime不能证明当前任务配置、真实模型版本或当天执行成功。使用 `research-doctor` 和实际Codex任务界面核对。社交资料只提供假设，不能成为策略效果证据。旧[完成计划](docs/PROJECT_COMPLETION_PLAN.md)保留历史时间语境，当前状态以新版审计及验收为准。

在本项目目录执行。已有机器可使用原 `.venv`；本次交付不含环境，不能据旧文档认定当前依赖完整。新机器先按前两行安装，再运行测试。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m ashare_agent init
.\.venv\Scripts\python.exe -m ashare_agent demo-data --sessions 320
.\.venv\Scripts\python.exe -m ashare_agent backtest --snapshot runtime/snapshots/demo-v2-320-42 --start 2025-09-25 --end 2025-12-31 --output runtime/my-backtest
```

打开 `runtime/my-backtest/report.html`。同目录有 Markdown、完整 JSON 结果和 SQLite 账本。合成数据使用工作日历，**不代表真实交易所节假日日历**；股票代码只是测试标识，价格也不是对应股票的真实价格。

演示结束日固定为 2025-12-31，生成 320 个合成交易日及一个用于次日委托的后续日期。默认初始资金 10 万元只是模拟值，不是用户实际资产。

## 已实现的命令

`--config` 是全局参数，放在子命令之前。示例：

```powershell
.\.venv\Scripts\python.exe -m ashare_agent --config config/settings.yaml select --snapshot runtime/snapshots/demo-v2-320-42 --date 2025-12-31
```

| 命令 | 用途 |
| --- | --- |
| `init` | 创建运行目录，保存配置和环境信息 |
| `demo-data` | 生成明确标记的合成数据快照 |
| `doctor --snapshot <目录>` | 检查快照结构、数据状态、文件哈希和模拟可用性 |
| `doctor` | 检查 Token 是否配置及原始缓存能力；不把离线检查称为权限验证 |
| `download` / `update` | 按请求日期下载/更新 Tushare 原始缓存 |
| `build-data` | 合并原始缓存及历史时点证据，产生不可变快照 |
| `select` | 因子、候选排名和市场环境；不下单 |
| `backtest` | 固定版本、固定快照的历史模拟 |
| `rolling-oos` / `walk-forward` | 固定规则滚动样本外评估，不拟合参数；后者保留兼容 |
| `robustness` | 预定义成本加倍、容量减半对照 |
| `paper` / `daily` | 结算已冻结订单、更新风险、产生次日意图、生成日报 |
| `paper-account` | 独立虚拟账户的创建、列表、只读状态、数据预检与前瞻推进 |
| `report` | 从已保存的 `run.json` 重新生成报告 |

运行 `python -m ashare_agent <命令> --help` 查看参数。

```powershell
# 每日筛选
.\.venv\Scripts\python.exe -m ashare_agent select --snapshot runtime/snapshots/demo-v2-320-42 --date 2025-12-31 --output runtime/selection.json

# 固定基线：252 个历史日作为初始研究/预热窗口，后续每 21 日独立评估
.\.venv\Scripts\python.exe -m ashare_agent walk-forward --snapshot runtime/snapshots/demo-v2-320-42 --start 2024-10-10 --end 2025-12-31 --train-sessions 252 --test-sessions 21 --output runtime/my-walk-forward

# 成本/容量压力测试
.\.venv\Scripts\python.exe -m ashare_agent robustness --snapshot runtime/snapshots/demo-v2-320-42 --start 2025-09-25 --end 2025-12-31 --output runtime/my-robustness

# 合成数据只能显式重放；第一天形成意图，后续交易日才可能成交
.\.venv\Scripts\python.exe -m ashare_agent paper --snapshot runtime/snapshots/demo-v2-320-42 --date 2025-09-26 --replay --ledger runtime/my-paper/account.sqlite
.\.venv\Scripts\python.exe -m ashare_agent paper --snapshot runtime/snapshots/demo-v2-320-42 --date 2025-09-29 --replay --ledger runtime/my-paper/account.sqlite

# 重新渲染保存的研究结果
.\.venv\Scripts\python.exe -m ashare_agent report --input runtime/my-backtest/run.json --output runtime/report-copy
```

历史回测使用同一执行器和账本；逐日模拟必须按交易日连续推进。重复执行最近已处理日期不会再次成交；旧日期请读取当时保存的报告。交易源码、配置或已处理的数据发生变化会报错，必须分开保存新的研究结果，不能覆盖原研究。`run.json` 保存源码、配置、历史输入哈希及记录时间；回测、历史 Paper、前瞻 Paper 分开使用账本。

`walk-forward` 不会自动寻找赚钱参数。各测试窗口独立初始化资金，不能拼成一条连续可交易曲线。成本压力对照不选择最优情景。当前没有训练模型，也没有完成未见真实历史上的 Alpha 验证。

## 接入 Tushare

创建项目根目录 `.env`，填入自己的 Token，不要把 Token 发到聊天或提交 Git：

```dotenv
TUSHARE_TOKEN=你的Token
```

`.env` 和整个 `runtime/` 已在 `.gitignore` 中排除。

```powershell
.\.venv\Scripts\python.exe -m ashare_agent doctor
.\.venv\Scripts\python.exe -m ashare_agent download --start 2024-01-01 --end 2025-12-31
.\.venv\Scripts\python.exe -m ashare_agent update --start 2026-01-01 --end 2026-01-09
.\.venv\Scripts\python.exe -m ashare_agent build-data --start 2024-01-01 --end 2025-12-31 --evidence-dir runtime/evidence
```

以 `build-data` 输出的快照路径替换演示路径。历史 ST、行业、停牌和公司行为必须有可核对的时间范围及来源证据，详见 [数据文档](docs/DATA.md)。缺失关键证据时，系统拒绝把结果当作可交易的正式模拟；不能用今天的状态填满历史。不要为让程序通过而把未知状态或公司行为完整性直接标为真。

若未配置 Token，可完整运行本地合成数据测试，但真实接口权限、历史覆盖、数据修订和供应方返回行为仍属于未验证。`doctor` 的 `network_checked:false` 必须按未联网理解。

## 前瞻 Paper 与每日运行

只有真实、完整快照才能不带 `--replay` 运行 `daily`。当前日行情必须有带时区的 `available_at` 可用时间证据，且不能晚于实际决策时刻。它必须在所选日期收盘之后、下一交易日 09:15 之前冻结委托；历史时间调用会拒绝，避免把事后重放包装成前瞻结果。

每日先更新数据和证据、发布快照，再执行：

```powershell
.\.venv\Scripts\python.exe -m ashare_agent daily --snapshot <当天已验证的快照目录> --date <YYYY-MM-DD> --ledger runtime/forward/account.sqlite
```

可以由 Windows 任务计划程序调用此命令或上层数据更新脚本。当前交付不在这台电脑自动注册后台定时任务；运行机需保持在线，数据未齐时不应继续当日交易决策。SQLite 事务保证单写者和整日原子处理；任务调度器也应设置“不启动新实例”。

第一天从空账户开始，先冻结下一交易日订单；之后逐日结算上一日意图。已有卖出委托的资金不预先用于买入。停牌、跌停和容量限制可能导致退出持续无法完成，会留在持仓中。实际退市处置数据缺失时停止并要求补充，不能将股票从账上删除。

## 规则和风险口径

详见 [策略规格](docs/STRATEGY.md)、[模块契约](docs/CONTRACTS.md)、[指标口径](docs/METRICS.md)。主配置为 `config/settings.yaml`。

- 主板、上市至少 250 日、20 日成交额门槛；60/120 动量等权百分位，MA120 趋势过滤。
- 每周最后交易日调仓，前 10 名新入、已有持仓可保留至前 20 名；单股 8%、总仓 80%、行业 25%，最多 10 只，不强制凑满。
- 当天风险/趋势退出、超限减仓按规则生成次日意图。10bp 滑点、历史日均成交量 0.1% 容量是假设，不代表实际开盘可成交量。
- T+1 根据批次与真实交易日管理；只支持主板普通整数股买卖及清仓零股，不支持其他板块的委托单位。
- 最大回撤 15% 触发后本账本永久禁止再开仓并尝试退出；本版本没有自动重置高水位。日/周亏损触发后至少经历 5 个无再次触发的处理交易日才恢复；所有参数是研究默认值。
- 止损和回撤均是触发阈值，不保证损失封顶。主基线不启用盘中 ATR 止损、分批止盈或 AI 动态调仓。
- 模拟佣金万三、最低 5 元，假设佣金包含券商经手/监管费用；过户费和卖出印花税另计。券商实际佣金未确认。
- 公司行为支持明确记录日期、除权日期、到账日期、红股可卖日期的现金红利与整数送转；输入税后现金，暂不自动计算个人持股期限差别税。配股认购、复杂合并换股、碎股补偿不支持，缺数据时应停止。

默认费用分段从 2015-08-01 开始；更早时期明确拒绝。印花税 2023-08-28 调整依据见 [税务总局公告](https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html)，2022-04-29 过户费调整可参考 [券商执行通知](https://www.xcsc.com/main/a/20220429/1022871784.shtml)。研究前仍应核对目标时期、交易品种和账户费用。

## AI 研究

默认提供机械说明并标记“未调用模型”。`ResearchProvider` 与交易路径隔离。可选 HTTP 接口接受带来源的研究输入并返回严格 `ResearchRecord`；它是通用适配协议，不宣称已直接适配每一家模型 API。

启用需设置 `research.enabled: true`、配置 `RESEARCH_API_URL/RESEARCH_API_KEY/RESEARCH_MODEL`，并通过 `--evidence` 提供真实且带时间的研究材料。未提供证据或供应商不可用时返回 unavailable。外部返回的订单/仓位字段不被接受，引用须可追溯到输入资料；研究结果不改变订单。

历史回测和 `--replay` 都禁用外部 LLM。模型可能知道历史结局，不能把新闻回放结果当作严格历史选股验证。

## 工程与验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe scripts/verify_offline.py --output runtime/acceptance
```

核心测试包含人工资金账本、手续费日期、T+1 新旧仓、重复成交、分笔成交、事务回滚、分红送股、限价/停牌拒单、未来数据隔离、持久化恢复和回测/Paper 重放一致性。验证状态及交付结果见 [验收记录](docs/ACCEPTANCE.md)。

`verify_offline.py` 实际调用公开命令，保存每一步日志及 `summary.json`；不使用 Token，不发起数据或模型网络请求。若修改过交易源码，使用新的 `--output` 目录以保留旧验收账本。

数据、策略、风控、Broker 和报告已经分离。未来财通 QMT 接入需要实际权限、行情和回报适配、状态核对与专项验收；本项目没有伪造 QMT API，也没有把实盘适配视为已完成。
# 一键 AI 复核

新版入口为网页 `/research`，旧 `/ai-review` 只保留只读历史。研究未登记、没有冻结批次时不能导出；初始化会锚定真实研究阶段，不能当作界面检查执行。

在项目根目录双击 `一键AI复核.cmd` 导出已有最新冻结批次；指定批次可运行 `powershell -ExecutionPolicy Bypass -File scripts/ai_review.ps1 -Action prepare -BatchId 实际批次ID`。输出位于 `runtime/research-v2/outbox/<batch_id>/`，包括 `packet.json`、逐模式提示词、结果 schema 和 `results/`。

将对应提示词交给有本项目本地访问权限的 Codex 复核对话，按提示先 claim，再保存对应结果到该批次的 `results/`。导入必须明确批次和结果文件，例如 `powershell -ExecutionPolicy Bypass -File scripts/ai_review.ps1 -Action import -BatchId 实际批次ID -Result runtime/research-v2/outbox/实际批次ID/results/balanced.json`。也可在网页选择明确批次上传原始 JSON；不得向旧 daily-lab overlay 路径导入。

实际接收时间必须早于信号日 Asia/Shanghai 20:00，迟到结果只能明确作为回顾性存档；AI 不负责资金、股数、仓位、止损或下单。详见 [新版运行手册](docs/RESEARCH_V2_RUNBOOK.md)。
