# 现状审计、修改决定与未解决事项

审计日期：2026-09-16。依据本次上传Money.rar的源码、文档，以及后补runtime.rar中成功解出的运行元数据。SQL及最小结果见 `delivery_evidence/runtime_audit.json`，保护前后哈希见 `preservation.json`。本次未把历史文档自述当作数据库事实。

## 1. 修改前恢复点与读取范围

完整上传RAR保持原样；源码复制为干净基线并建立本地git提交，再在独立工作副本修改。升级包的安全安装器在用户机器上另做实际修改前备份，默认只预检。既没有将真实runtime复制进交付源代码，也没有输出`.env`。

这里的libarchive对runtimeRAR解码到 `runtime/current-acceptance-full/history/sh603392-raw.parquet` 时遇到“不支持的压缩块头”错误，因此**不能声称完整运行压缩包已全部解压验收**。随后按元数据类型单独完整解出593个SQLite/JSON/报告/日志等文件；不使用前一轮中断产生的数值缓存。两个RAR的字节数及SHA256、593文件再次核对结果均在preservation.json。Parquet/CSV行情缓存未据此认证。

数据库使用SQLite URI `mode=ro` 打开；有WAL时此方式读取现有WAL，不把活跃库当immutable。核心查询为：

```sql
SELECT COUNT(*) FROM batches;
SELECT COUNT(*) FROM observations;
SELECT COUNT(*) FROM overlay_results;
SELECT COUNT(*) FROM review_imports;
SELECT batch_id, as_of, created_at, input_hash, rules_hash FROM batches;
SELECT batch_id, mode, created_at, payload_json FROM overlay_results;
```

个人planner仅核对表计数与完整性，不导出余额、持仓金额等内容。完整性quick_check在本次元数据副本通过，不等于原机器当下数据库已停止写入或具备同样状态。

## 2. 实际运行事实

| 核对项 | 本次库中事实 | 解释 |
|---|---|---|
| 原前瞻batches | 1 | 仅2026-09-11 |
| 原observations | 0 | 不能据此计算任何真实D1/5/20样本表现 |
| 原overlay_results | 3 | conservative / balanced / aggressive的旧存储结果 |
| 网页review_imports | 0 | 新旧入口不能混称已经有效接入运行 |
| 旧组别 | mechanical / momentum60 / momentum120 / news_guard | 均来自同一个已过趋势过滤的Top20，不是共同池实验 |
| 旧证据 | 785条，verified_primary为0 | 新闻/社区聚合材料不能自动升级为一手事实 |
| 最近可见daily日志 | 2026-09-15上午，deferred，观察/选股均not_run | 表示等待，不证明当天收盘任务成功 |

唯一旧批次ID `bdcbf327c489accb28d66f2124fc1663`，实际冻结时间 `2026-09-11T17:15:26.517764+08:00`。三条overlay自报时间均为 `2026-09-11T17:15:26.498344+08:00`，比冻结还早约19毫秒；实际数据库created_at分别是9月13日13:10:58、13:11:00、13:11:02（上海时区）。旧结果不是新协议样本，本身也缺少及时前瞻提交的支持证据。保留为历史审阅，**不推断有人故意造假，也不将其计入有效前瞻AI**。

原文档出现多个历史测试通过数与任务ACTIVE陈述；全部按历史记录保留，不当作本次软件结果或当前主机调度认证。本次没有用户Windows调度器或Codex应用的实时控制连接。

## 3. 逐项接受、拒绝及实施理由

| 要求/建议 | 处理 | 代码落点或理由 |
|---|---|---|
| 核实四类路径是否真正接线 | 接受并发现入口分叉 | 旧网页代码具备部分观察连接，但实际review_imports为0；旧一键CLI只写overlay。现在统一新/research与research-import，旧写口停用 |
| 保留历史和账户，先恢复点 | 接受 | 原RAR/基线提交/源代码安装前备份；新库独立；593运行文件及14核心源文件哈希一致 |
| 共同池不含趋势预筛 | 接受并重构 | current_screen.qualify生成共同池；assess保持原趋势兼容接口；负动量、低于MA反例进入共同池 |
| “完整交易资格合格池已经具备” | 不作此断言 | 现有源缺交易所时点状态/成交档案，研究池标记execution_eligibility未认证；执行实验不启动 |
| 少量预先登记机械对照 | 接受 | 全池等权、原机械、两项联合门槛消融、20日反转；不扩大参数搜索 |
| 因文献而默认反转胜出 | 拒绝 | 20日公式只登记假设；本项目组合、时期、数据和交易成本与原文不同 |
| 现有60/120等权必须直接删除 | 未照做 | 保留为待检验的现有基线，不宣称权重最优；直接删除会改变研究对象和主策略 |
| 三模式只是名字不算完成 | 接受并替换 | conservative只排除；balanced位移2；aggressive位移5；证据门槛/未知/补位不同，合成测试实际改变选择 |
| 三模式名意味着收益或回撤保证 | 拒绝 | 报告显示实际暴露和覆盖，不按名称赋予风险等级 |
| Codex对话研究入口 | 接受 | 固定包、schema、逐模式提示词、claim、单/多模式提交、CLI/网页/PS脚本；无新增模型API |
| 一概宣称完全自动或完全不能自动 | 拒绝 | 本地文件与命令交接已实现；实际对话权限和调度仍需本机验证；doctor输出当前可检查范围 |
| 真实本地截止、首次有效锁定 | 接受 | freeze/import事务、解析前本地收据、重复幂等、冲突拒绝、迟到仅显式回顾性库 |
| 证据不存在、跨股引用、结构越权 | 接受严格拒绝 | exact_keys、重复JSON键/NaN拒绝、全候选逐项覆盖、证据归属、包/源/机械组哈希复核 |
| AI失败/unknown不隐藏 | 接受 | 全批回退、有效格式提交、有效判断批次、逐股unknown、改变选择率分别报告 |
| 缺价时只对剩余股票重新加权 | 拒绝 | 完整权重指标置空，不用已知子集冒充整组收益 |
| 价格观察当净值、D1当股票可执行往返 | 拒绝 | is_executable_return=False；不产生订单/成交、Sharpe/Alpha/账户回撤为空 |
| 复用现有撮合立即生成新账户收益 | 暂缓实施新适配器 | 保留引擎正确风控；可信交易数据及新适配验收不足，八组实验连续账户未开展 |
| 行业、市值、市场敏感度诊断 | 部分可运行 | beta/个股波动历史代理可算；行业/市值时点字段缺失公开unknown，不回填历史 |
| 默认中性化一定更好 | 拒绝 | 先诊断，再独立登记中性化实验；本次不改因子/持仓/止损共同作用 |
| 时间划分、重叠和多重试验控制 | 接受 | 交易日阶段固定、20日边界隔离、日期级块重采样、四重校正、缺失网格保留 |
| 120样本或软件通过足以证明有效 | 拒绝 | 最低运行门槛不是证据充分；阶段未关闭不能推断；不自动晋级 |
| ETF/CTA/高频或几十因子扩展 | 拒绝本次扩展 | 可另立后续方案，不加入当前研究比较 |

## 4. 修改的功能入口

新模块：research_protocol.py、research_lab.py、research_statistics.py、research_commands.py、research_demo.py。核心重构：current_screen.py资格池分离、ForwardLab只增加可替换研究组钩子、daily_lab.py接线与独立状态、cli.py入口、review_center.py停旧写入、webapp.py与research页面、ai_review.ps1。精确修改文件清单及哈希见包根 `upgrade-manifest.json`。

14个保持逐字节不变的核心模块为config、models、data、hardening、universe、strategy、risk、execution、ledger、broker、engine、planner、intraday、paper_workspace；engine.source_fingerprint只绑定原执行名单，所以研究新增文件不会改变该执行指纹。test_intraday中修正的是旧夹具重复previous_close关键字的TypeError，不是放宽交易约束。

旧review_center写口的测试更新为“拒绝旧写入且保留可读历史”，更严格的输入校验/事务/时间反例迁移到新版测试，不以删除反例制造通过率。旧daily测试明确旧fixture不能认证v2，证据采集失败也不再无故阻断机械冻结。

## 5. 仍未完成，不以实现接口代替真实验收

新版本尚无真实市场前瞻批次、按时真实Codex三模式结果或到期观察。原样本不能迁移填数。本次有合成端到端证据，但没有模型效果证据。当前本地不能完成Parquet相关全套测试，且缺少完整可解析数值缓存；完整交易资格、真实撮合、连续实验账本、净成本表现、时点行业/市值与风险调整Alpha仍未验证。

本地数据库哈希与claim不能证明外部未反复调用模型，也不能抵抗拥有系统全部写权限的恶意重写；身份记录不等于模型版本认证。内容哈希与两域名规则不等于新闻真实性/独立性证明。

下一优先事项：在原机器依赖齐备的隔离项目副本里完成全量回归及真实收盘单日闭环，保留输入包、实际接收收据与次日观察，先解决可重复数据质量；随后再接可信时点交易状态和独立账户适配。不能从合成结果或一两个真实日自动更换主策略。
