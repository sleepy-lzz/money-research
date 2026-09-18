# Money 本次源码核对与兼容合并报告

## 当前结论

**状态：已完成上传包核验和独立副本中的兼容合并；尚未部署到用户 Windows 原项目；完整软件回归尚未通过。**

本次依据最新 `Money_可交接源码.zip`、`receipt.json`、`整理报告.json` 及 ZIP 内实际日志、冲突补丁、源码开展。用本会话原 `Money.rar` 中的两个基线文件和原 `Money_research_v2_交付包.zip` 作三方比较。没有修改这些原始上传文件，没有连接 GitHub，没有调用行情、付费模型或真实账户操作。

## 1. 包完整性已实际核验

最新 ZIP SHA-256：

`dd63074b496e53cffe8f3910790bfaa0847e0131b11f5f2ffb9594a13e31c277`

它与 receipt 的 `archive_sha256` 相同。ZIP 中 139 个实际文件与 receipt 的 139 个条目逐一核对：路径、字节数和 SHA-256 全部匹配；缺失 0、多余 0、内容不符 0。123 是导出工具的源码计数，不是整个 ZIP 文件总数，二者不能混用。

原 v2 清单的 47 个交付文件也重新验证了 after 哈希，均与原交付清单一致。原交付压缩包及清单未修改。

依据：`receipt_verification.json`、原 ZIP、receipt；清单验证为本次计算，不是仅转述旧报告。

## 2. Codex 暂停前的实际状态

交接中的原始记录有以下内容，路径均相对于 ZIP 内 `Money/docs/handoff/oneclick-export/evidence/`：

| 证据 | 已支持的结论 | 不能据此声称 |
|---|---|---|
| `logs/isolated_updater_dry_run.log.txt` | 更新器返回 `rejected`，错误是 `local_source_conflict:src/ashare_agent/webapp.py` | 更新已完成 |
| `logs/isolated_updater_exit.json` | 预检退出码为 1 | 已执行正式 --apply |
| `logs/conflicts.json` | 原项目冲突清单有 webapp.py 与 test_intraday.py 两项 | 两个文件均应被旧包覆盖 |
| `logs/isolated_dependency_retry.exit.txt` | 依赖安装重试退出码为 0 | 项目测试已通过 |
| `logs/isolated_dependency_retry.log.txt` | 末行是 Successfully installed，其中包含 pyarrow、duckdb、tushare、pytest 等 | 最新 Windows 环境仍与当时完全相同 |
| `logs/backup_verification.json` | 当时脚本记录复制了 75427 个文件、32 个 SQLite 备份，哈希/完整性检查成功 | 本次独立核验了用户电脑现在的全部备份和账户 |

本次 ZIP 仍没有 `logs/test_results.json`、`logs/baseline_full.log`、`logs/requested_targeted.log`。因此不能为那次 Codex 任务补写测试通过次数。日志缺失也不等价于证明任何测试从未在别处运行。

**阻断主因是源码版本不兼容；不是交接包缺失，也不是已提供的依赖安装重试失败。**

## 3. 两项冲突的真实差异及处理

### 3.1 webapp.py

从原 `Money.rar` 取出的文件 SHA-256 与旧交付清单的 before 哈希完全一致。原上传版本到最新上传版本仅有这一项差异：

```diff
-BACKEND_VERSION = 7
+BACKEND_VERSION = 8
```

v2 交付文件新增 `/research` 及相关研究接口，却仍写着版本 7。直接使用交付文件会丢掉本地版本变化；只保留 8 又会让启动器把已运行的旧版 8 与新接口代码视为同一版本。该启动器实际按 `backend_version` 是否相等决定是否复用现有服务。

本次独立候选副本使用 **版本 9**，保留 v2 研究接口和原进程恢复逻辑。新增反例测试验证：遇到版本 7/8 不直接复用，遇到当前版本 9 才复用。测试使用合成服务，不启动生产任务、不结束任何用户进程。版本号区分不能代替部署时停止旧后台，也不替代完整代码身份认证。

### 3.2 tests/test_intraday.py

原基线到最新上传版本的唯一差异，是测试调用去掉冗余 `previous_close="10.00"`。原辅助函数已设置同名默认参数，再显式通过 `**changes` 传入会引起重复关键字错误。

本次同时保留：

1. 本地调用点不再重复传入 previous_close 的改动；
2. v2 中测试辅助函数使用字典解包合并默认值和覆盖值的修复。

没有删除任何已有断言，也没有更改 `intraday.py` 的业务数学。新增测试覆盖默认值覆盖和本地调用修复保留。

### 3.3 .gitignore 是导出差异，不是第三项生产冲突

对“整理后的源码”运行原清单比较，还会看到 `.gitignore` 不同。整理报告已明确指出它由 GitHub 源码准备步骤修改。不能将这个差异误报成用户原项目的第三项冲突。

本候选副本保留最新导出中更严格的 `.gitignore`，不恢复旧的较窄排除规则。没有修改原清单哈希来跳过检查。

### 3.4 README 补丁及换行格式

Codex 准备的 README 补丁只能应用到 v2 README，不能应用到旧基线 README。补丁传输文件为 CRLF，而 v2 README 为 LF，直接 git apply 在本环境失败。仅在临时副本规范化补丁换行后，补丁精确应用成功，应用后的中间文件哈希与原 patch-manifest 的 after 哈希一致。

随后在候选 README 顶部追加“只验证、未部署”的状态提示。旧补丁原件保持不变；中间哈希及最终哈希分开记录。新版最后一节使用 `runtime/research-v2/outbox/<batch_id>/`，不再指导用户向旧 overlay 目录导入。

## 4. 本次实际测试及边界

全部本次测试均在 Linux 独立目录运行，使用合成数据。测试进程不继承 Token/API 密钥，并限制外连，只允许本地回环 HTTP；没有真实研究登记或生产账户推进。

| 本次检查 | 结果 |
|---|---|
| 最新基线的 intraday / review_center / webapp 三模块针对性测试 | 51 passed，退出码 0 |
| 兼容候选的 research_v2 / research_upgrade / review_center / intraday / webapp / compatibility_merge | 129 passed，退出码 0；包含 7 个新增兼容用例 |
| 本次附带“只验证”工具的合成单元测试 | 11 passed，退出码 0；不计为项目策略测试 |
| 候选全量范围、遇到首个错误停止的诊断运行 | 12 passed、1 error，退出码 1；错误为当前 Linux 环境缺少 Parquet 引擎 |
| 首次尝试完整基线测试 | 被执行工具超时中断；没有完整结果，不推算最终通过数 |
| 候选 Python 静态语法解析 | src / tests / scripts 共 74 个文件通过 |

当前 Linux 环境还缺少 pyarrow、duckdb、tushare；尝试在新虚拟环境安装声明依赖时遇到网络 DNS 错误，未安装成功。这是本次审阅环境的限制，**不能据此断定用户的 Windows test-venv 缺少依赖**。用户此前安装日志实际显示这些依赖已安装，但仍需本机确认。

因此：129 项通过只是针对性软件证据，不是完整项目回归通过。未做 Windows 双击入口实测、真实数据源验收、生产数据库核验、收益验证或 AI 效果验证。

## 5. 保留范围

与最新上传源码逐字节比较，以下 16 个核心模块保持不变：analytics、broker、config、current_data、data、engine、execution、intraday、ledger、live_quotes、models、paper_workspace、planner、risk、strategy、session_calendar。

`config/settings.yaml`、`pyproject.toml`、`requirements-lock.txt`、`.env.example`、导出版本 `.gitignore` 也保持不变。保留的历史交接材料与旧审计记录不能被当成本次新测试结果；本次结果放在独立证据目录。

用户实际运行目录、`.env`、账户、冻结历史和恢复备份不在此次代码写入范围内。本次源码包不含真实账户，不能用源码哈希代替数据库完整性证明。

## 6. 交付和下一步

交付目录包括 `Money_candidate/` 兼容候选源码、核对报告、逐文件合并哈希、原始证据摘录、本次测试日志、源文件清单及一个 **只验证不部署** 的入口。

**不是新的自动安装包。候选中故意没有重写过的旧 upgrade-manifest.json。不要运行旧更新器来套用此候选，也不要把候选整个复制覆盖生产 Money。**

把整个交付文件夹放在原验收目录 `Money_v2_acceptance_20260917` 下，双击 `01_只验证不部署.cmd`。它优先使用旁边已有的 `test-venv`，校验候选文件后再复制到新的隔离验证目录；不安装依赖、不修改虚拟环境，不读取或覆盖原 Money，不运行 research-init、daily-lab 或账户 advance。

它执行完整 pytest（不设置 --maxfail、不新增 skip）以及针对性测试，输出 `verification-results.zip`。若依赖不齐或源文件哈希不符，明确阻断。ZIP 仅打包本次报告及测试日志，不收集测试数据库、原 runtime 或备份。

**下一优先事项是取得本机完整回归结果。** 只有再完成生产版本兼容检查、可恢复备份和部署验收，才能谈本地升级完成。真实研究注册、每日运行和 GitHub 上传均是后续独立操作。
