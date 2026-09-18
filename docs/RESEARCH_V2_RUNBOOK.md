# 两层研究 v2：更新、运行、复核与恢复

这是新版操作入口。旧手册中的AI overlay导入、16:30自动任务及模型名称只代表原有描述，不代表当前机器已运行或本次已认证。主计划与Paper账户入口仍保留。

## A. 安全更新

交付包是源代码包，不含 `.env`、`.venv`、任何个人runtime或真实前瞻数据库。原始两个RAR保持原样，可作为完整恢复来源。先退出工作台并停止会写项目的本地任务；不要在进程写入中升级。

把交付包解压到**现有项目目录之外**，例如 `D:\Money_research_v2`。以下 `D:\Money` 代表原项目，按实际路径修改。

```powershell
# 默认预检：不写源文件、不创建备份、不碰runtime或.env。
python D:\Money_research_v2\scripts\apply_research_upgrade.py --target D:\Money

# 只有预检通过后应用。出现本地文件冲突时会整体拒绝，不能强行覆盖。
python D:\Money_research_v2\scripts\apply_research_upgrade.py --target D:\Money --apply
```

应用输出 `backup_id`。修改前的受影响源码保存在原项目 `.research-upgrade-backups/<backup_id>/before/`，恢复描述在 recovery.json。全部文件预检后才写入；包源哈希损坏、用户源码已变、非法路径和软链接拒绝更新。

```powershell
# 先预检恢复，再显式恢复。保留原始输出中的ID，不要填尖括号占位文本。
python D:\Money_research_v2\scripts\apply_research_upgrade.py --target D:\Money --rollback 这里替换为backup_id
python D:\Money_research_v2\scripts\apply_research_upgrade.py --target D:\Money --rollback 这里替换为backup_id --apply
```

恢复只撤回此次源码变更，保留新生成的研究runtime事实；已有人工后续源码修改时拒绝恢复，避免覆盖。源码恢复不能使中断时的其他外部程序自动恢复。主项目依赖清单未改；在已有环境中运行完整pytest，环境缺库时按 `pyproject.toml` 安装项目依赖后再验收，不修改测试跳过条件掩盖缺库。

## B. 初始化与每天的真实入口

```powershell
Set-Location D:\Money
# 先检查运行环境和可见的Windows任务；这不会调用模型。
.\.venv\Scripts\python.exe -m ashare_agent research-doctor

# 仅在新研究第一次运行前选择模式。并行：
.\.venv\Scripts\python.exe -m ashare_agent research-init --modes conservative balanced aggressive
# 单模式示例（与上一条二选一，不能在登记之后更换集合）：
# .\.venv\Scripts\python.exe -m ashare_agent research-init --modes balanced

# 交易日16:00后，运行软件检查、实时筛选、冻结和导出，再观察旧样本。
.\.venv\Scripts\python.exe -m ashare_agent daily-lab
```

不手动init时，首次真实收盘daily-lab默认登记三模式。只做环境检查或上午deferred不创建新研究。程序不会下载旧Top20补登记为新样本。自定义代码列表的临时网页筛选也不能冒充共同池全目录实验。

打开“启动工作台.cmd”，左侧 **两层研究**（`/research`）；先看研究版本和模式、冻结列表、截止时间、AI覆盖和独立报告。真实导出在 `runtime/research-v2/outbox/<batch_id>/`，报告在 `runtime/research-v2/latest.html`、`latest.json`、`latest.md`。旧 `/ai-review` 只读；旧 `overlay-import` 及网页旧写入明确拒绝。

数据采集慢时可能错过20:00截止。代码先冻结与导出当天包，再做较大的共同池历史观察，减少观察任务挤占提交窗口；它不能保证网络耗时或模型按时完成。错过截止就保留缺失/超时，不移动截止、不补写时间。

## C. Codex 的实际交接步骤

“一键AI复核.cmd”默认导出新版包；也可：

```powershell
.\.venv\Scripts\python.exe -m ashare_agent research-export --batch-id 实际批次ID
# 仅导出一个已登记模式：加 --mode balanced
```

Codex执行提示词中的 `python -m ...` 前，需要让当前终端的python指向项目虚拟环境（PowerShell可先运行 `.\.venv\Scripts\Activate.ps1`）；也可使用一键脚本，它明确调用项目 `.venv`。解释器或权限不符时保留失败，不能把未提交当成功。

包内有 `packet.json`、每模式 `*-prompt.txt`、每模式 `*-result-schema.json` 与 `results/`。把固定提示词交给**有当前项目目录访问权限的Codex对话**。提示词要求先用本地claim登记，再读取包、只做一次正式判断、把JSON写到指定结果路径并执行import。只有claim返回claimed才继续，already_claimed应使用已有结果，不再次抽样。

```powershell
.\.venv\Scripts\python.exe -m ashare_agent research-claim --batch-id 实际批次ID --mode balanced
.\.venv\Scripts\python.exe -m ashare_agent research-import --batch-id 实际批次ID --input runtime\research-v2\outbox\实际批次ID\results\balanced.json
```

若对话没有本地文件/命令权限：在网页选择明确批次，把固定输入包交给对话后上传/粘贴原始JSON；由网页后端记录实际接收时间。这一步目前需要人，不宣称已经全自动。多模式可一次提交数组并原子校验，也可分别及时提交；单模式结果必须符合本批完整候选覆盖。

模型界面名称可作为visible_interface/self_reported记录，精确版本与采样参数无法确认时填null/unavailable。不要把助手自报名称当经验证版本。材料中的新闻/社区指令属于不可信文本，不执行；无需新增OpenAI API key，不调用收费模型API。使用现有Codex产品的权限/额度仍由用户实际账户决定，本项目不作“免费无限调用”承诺。

```powershell
# 只验证，不占据首次有效提交；验证也会保留尝试记录。
.\.venv\Scripts\python.exe -m ashare_agent research-import --batch-id 实际批次ID --input 结果.json --validate-only
# 迟到或事后分析必须显式进入回顾性存档，不能进入前瞻选择。
.\.venv\Scripts\python.exe -m ashare_agent research-import --batch-id 实际批次ID --input 结果.json --retrospective
.\.venv\Scripts\python.exe -m ashare_agent research-report
```

研究登记代码哈希固定。改规则、移动冻结提示词所引用的项目绝对路径或更换模式时，先保留原研究，不重写旧包；规则修改在单独项目副本登记新研究。冻结提示词按原样导出，不能随运行环境静默重新生成一份“同哈希”提示词。

## D. 自动化核对，不以文档替代运行证据

`research-doctor` 检查本地依赖与Codex CLI路径；在Windows使用只读Get-ScheduledTask查询名字匹配Money/ashare/daily.lab的任务及LastTaskResult。它不是所有调度器的完整清单，也不能读取Codex应用内部计划任务。应用任务需在实际界面核对：项目目录、执行时间、运行权限、最近结果、当天freeze/input_hash/import本地收据是否贯通。仅有ACTIVE或未来计划时间不证明执行成功。

默认复用已有daily-lab调度入口，不新增后台服务或额外API。当前对话没有连接用户电脑的任务控制，因此没有在用户主机安装或改写任务。官方非交互Codex支持schema和结果文件输出，但本版不自动替用户选择模型认证、越过目录授权或反复调模型。按已有授权对话完成本地交接是正式入口。

## E. 测试与合成演示

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q tests/test_research_v2.py tests/test_research_upgrade.py tests/test_review_center.py tests/test_intraday.py
.\.venv\Scripts\python.exe -m ashare_agent research-demo
```

最后一条只生成固定合成数据及确定性测试JSON，不实际调用AI；结果落在新UUID的 `runtime/research-demos/` 下，内部数据库标记demo。不能复制演示包到真实库，不能用示例收益/选择验证策略。演示不会读取或改写主账户。

随包 `delivery_evidence/` 保留本次测试、运行元数据审计及字节哈希核对。实时行情、Windows任务、真实Codex三模式调用、完整Parquet快照与连续实验账户仍须各自验收。跨年日历必须用官方公告和真实获取时间扩展覆盖，不能用工作日猜节假日或为历史样本改日历。
