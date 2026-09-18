> 版本说明（2026-09-16）：本文保留旧版历史设计/运行描述。当前研究流程、AI导入与完成状态以 [RESEARCH_V2_RUNBOOK.md](RESEARCH_V2_RUNBOOK.md) 和 [RESEARCH_V2_ACCEPTANCE.md](RESEARCH_V2_ACCEPTANCE.md) 为准。旧AI写入已停用，旧批次不转为新前瞻样本。

# AI overlay 实验接口

当前生产决策仍是机械基线。网页复核中心接受绑定同一机械 Top20 的结构化 JSON，并行记录 conservative、balanced、aggressive 三种模式；按时提交的结果接入统一 D+1/D+5/D+20 价格观察。没有模拟成交或扣费收益，trading_weight=0。

这里的 Codex 是半自动研究输入，不是稳定的后台模型服务：需要人工提交 JSON，保留模型/提示词/输入哈希，失败或缺证据时回退机械结果。未接入前不得把 overlay 结果写成 daily-lab 已完成的 AI 前瞻收益对照。

每条结果必须包含 `batch_id`、`as_of`、`available_at`、`input_hash`、`model_version`、`prompt_version` 和结构化 `decisions`。AI 只能给 `keep`、`deprioritize`、`unknown`，不能输出金额、股数、止损或买卖动作。缺时间、证据或模型输出异常时保留 unknown 并回退机械结果。

网页入口 `/ai-review`，新服务 `review_center.py` 使用 review_imports 表完成事务校验、接收时间审计和冻结观察组。详见 [网页操作及边界](WEB_AI_REVIEW.md)。旧版 OverlayLedger / overlay-import 仅记录结果，不自动升级为前瞻组；建议使用网页重新校验原始 JSON。迟到结果仍不进入历史前瞻观察。

不得按短期结果自动选择三种模式。晋级前需预先登记样本量、成本后净增量、回撤、换手、成交率、未知率和失败条件，并在同一 OOS 时间段比较。
# Codex 对话导入流程

`overlay-export` 导出冻结批次的不可变候选与证据包；本对话依据包内 instructions 生成 JSON，再用 `overlay-import` 导入同一前瞻账本。导入重新核对 batch、候选代码、证据 ID 和 input_hash，冲突拒绝覆盖。结果固定 `trading_weight=0`，不改变订单或机械策略。

命令：`python -m ashare_agent.cli overlay-export --batch-id <ID> --output packet.json`；`python -m ashare_agent.cli overlay-import --batch-id <ID> --input result.json`。没有可信时间戳或证据时返回 unknown；不能补写已过决策截止时间的历史 AI 结论。
