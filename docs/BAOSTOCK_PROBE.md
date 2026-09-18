# BaoStock 实际连接测试

日期：2026-09-10。测试环境为本机 Windows / Python 3.13，客户端 0.9.3
从官方 PyPI 安装到 `runtime/baostock-libs`，未添加为生产依赖。

## 实测结果

* 两次匿名 `bs.login()` 均返回 `10002007`（网络接收错误），底层出现连接超时。
* SDK 配置的服务地址为 `public-api.baostock.com:10030`。
* DNS 解析成功；独立 TCP 连接检查在 8 秒后超时。
* 因登录失败，交易日历、历史证券池、证券元数据、日线、ST/停牌及复权查询均未执行。
* 没有成功取得真实行情，不能宣布 BaoStock 已可替换 Tushare，也不能据此判断 BaoStock 全网不可用。

本次与 Tushare 的 `40203` 权限拒绝不同：失败发生在网络连接阶段。
具体是本机网络出口、路由还是供应方服务问题尚未确定；未修改代理、防火墙或系统安全设置。

两次原始结果：

* `runtime/baostock-probe/20260910-163602/summary.json`
* `runtime/baostock-probe/20260910-163656/summary.json`

## 可复用测试

`scripts/probe_baostock.py` 保存逐请求参数、获取时间、返回状态、字段、行数及内容哈希。
网络恢复后会尝试近期/历史股票池、证券资料、三只主板股票、不复权与前复权价格、
复权事件，以及可发现的 ST/停牌/退市样本；这些仍不是正式 PIT 认证。
脚本设置 socket 超时和总时间上限，避免客户端等待阻塞。

```powershell
.venv\Scripts\python.exe scripts/probe_baostock.py
```

目前只完成 SDK 安装、连接测试及诊断；数据质量和覆盖验收尚未开始。

## GitHub 候选复核

* [akfamily/akshare](https://github.com/akfamily/akshare)：可评估腾讯历史日线路径，
  避免与 BaoStock 的 TCP 连接问题混为一谈。当前 issues 中存在东财连接及 ST 列表超时报告，
  不能宣布整个库稳定可用；接口返回成功仍需核对成交量/成交额单位、复权和缺失状态。
* [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)：
  可参考日报界面和多源适配。其数据配置仍依赖 Tushare、BaoStock、腾讯等上游，
  不是新的数据供应商，也不能直接代替本项目的历史可见性和账本约束。
  [数据源配置](https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/docs/full-guide.md)。
* [mootdx/mootdx](https://github.com/mootdx/mootdx)：通达信数据读取封装。
  适合作为另一个候选，但线上协议连接仍须本机实测，当前未安装或访问其行情服务。
* [zxygithub/baostock](https://github.com/zxygithub/baostock)：第三方 BaoStock 下载/存储工程，
  可参考缓存组织；继续依赖 BaoStock 服务，不能解决本次网络超时。

当前决策：BaoStock 暂不接入生产选股；下一小样候选为 AKShare 腾讯日线，
并独立核验当前主板集合、ST/停牌状态和成交额。不把 GitHub 上的 AI 分析输出作为数据质量证明。
以上是只读项目/文档复核，未运行这些仓库的代码，尚未作完整源代码审计。
