# 配图手册维护

手册对应 2026-09-11 的 v0.1.1 / 后台版本 6。交付文件在 `output/pdf/`；根目录 `查看使用手册.cmd` 打开离线 HTML。

- `manual_content.py`：24 页正文，共用一份内容生成 PDF 与 HTML。
- `capture_manual.cjs`：渲染实际交付网页，拦截全部接口并返回明确标记的虚构资料。不启动后台，不操作真实账户。截图写入 `tmp/pdfs/manual/screens/`。
- `build_manual.py`：使用 ReportLab、Pillow 与 Windows 微软雅黑生成手册；图片嵌入 HTML，离线可读。
- `verify_manual.cjs`：用无头 Edge 检查章节、图片、目录、放大、手机宽度与无外部网络请求。
- `package_manual.py`：仅打包 PDF、HTML 和阅读说明，白名单排除账户、Token 和运行数据。

从项目根目录按 capture → build → verify → package 执行。JS 当前使用本机 Codex bundled Playwright 路径，Python 使用 bundled artifact runtime，换机器需调整依赖路径。每次重建 PDF 后用 Poppler 渲染全部页面并逐页检查；截图与 QA 文件放在 `tmp/pdfs/manual/`，不加入分享包。

更新时尤其核对：账户确认时间、实际可卖数量、原计划数量上限、严格 Snapshot 门禁，以及网页定时复核、盘中监控、Codex 自动任务三者的区别。当前宏观和财报只供参考；daily-lab 尚不采集这两层数据；不要在手册中写成已参与仓位或已自动采集。自动任务属于本机 Codex 配置，不随项目复制。
