# 更新日志

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- PostgreSQL 任务队列后端：`[queue] backend = "pg"` + `[pg]` 段，多台机器共享同一个批次。导入时按 (主机, DOI) 排序切块，机器按块领取、带租约，结果批量写回；正常退出交还未做完的块，异常退出由租约到期回收。需要 `pip install 'paperlite[pg]'`。
- 所有命令按 `[queue] backend` 选库；`--pg` 可对单条命令临时改用 PostgreSQL。
- PostgreSQL 后端下巡检只作用于本机领到的块；`status` 显示块统计；`inspect --list` / `--reset` 查看和放回巡检跳过的任务。
- `tools/bench_pg_chunks.py`：PG 队列调度压测。

### 说明

- PostgreSQL 后端暂不支持 `export` / `retry`（会报错，用 SQL），不做卷期停止。

### 文档

- README 精简为概览，详细内容拆到 `docs/`：使用指南、配置参考、命令参考、任务库、架构与设计说明。
- 新增 `CONTRIBUTING.md`、`CHANGELOG.md`、快速上手（中英双语）、故障排查、多机部署、PG 按块领取设计。
- `docs/MONITOR.md` 移至 `docs/guide/monitoring.md`，`POSTMORTEMS.md` 移至 `docs/development/postmortems.md`。

## [0.2.5] - 2026-11-17

### 变更

- curl 模式按消费生产身份：生产目标 = min(`max_identities`, 在线下载线程数, 可领取任务数)，没有下载线程或任务领完时停止过盾。
- 删除 `produce --keep`；`min_identities` 仍可读取但不再生效，非 0 时打警告。

### 修复

- curl 模式的进度日志和文档不再把 HTML 任务说成 PDF。

## [0.2.4] - 2026-11-17

### 新增

- CSV 导入前用 DuckDB 按 DOI 排序，同一期刊的任务连号。
- 新增 `error_kind = rate_limited`（HTTP 429 等待后仍未恢复）。

### 修复

- Cloudflare 拒绝的 429 直接丢弃身份，不再等待。
- 429 判定顺序、测试收集错误；`.gitignore` 补上凭据文件。

## [0.2.3] - 2026-11-10

### 文档

- README 改写为英文，新增中文版 `README.zh-CN.md`。

## [0.2.2] - 2026-11-10

### 新增

- IP 在租期内失效时提前更换 IP。

### 变更

- `pika` 改为默认依赖；监控去掉证书 / TLS 配置。

## [0.2.1] - 2026-11-10

### 修复

- 发布流程的测试装上监控依赖。

## [0.2.0] - 2026-11-10

### 新增

- 监控模块：终端经 RabbitMQ 上报指标，`paperlite-monitor` 提供面板。
- 面板：小时汇总存储、登录密码、域名表服务端分页 / 排序 / 筛选 / 主域名合并，公网部署文档。
- 代理提取接口支持纯文本返回。

## [0.1.10] - 2026-11-07

### 新增

- 浏览器反检测：`geoip`（按出口 IP 对齐时区和语言）与 `webrtc_spoof`，默认开启。

### 修复

- 首次下载卡死。

## [0.1.8] - 2026-11-07

### 变更

- 配置重构为 `[limits]` / `[paths]`；默认浏览器模式。
- 代理分层体检（TCP / TLS / TTFB / 总耗时）；巡检覆盖失效页。

## [0.1.4] - 2026-11-07

### 新增

- `paperlite run config.toml` 按配置文件启动；日志分终端与文件输出。

## [0.1.3] - 2026-11-07

### 新增

- 多主机共用一个任务库；`--browser` / `--curl` 模式开关；浏览器直连 `--no-proxy`。
- 浏览器下载模式：常驻浏览器 + 独立 IP 批量下载网页。
- 网页任务：`url_type`、整页保存、跨主机跳转时浏览器兜底。
- 重复标题巡检：归纳 DOI/URL 规则，跳过疑似封面页。

### 变更

- 表结构增加 `url` / `doi` / `title` 列，`error_message` 只存错误，文件路径改为绝对路径。

### 修复

- 浏览器模式保存的标题被截断到 60 字符。

## [0.1.2] - 2026-11-04

### 新增

- 过盾时自动点击 Turnstile；新增 `paperlite turnstile` 按 sitekey 获取 token。

## [0.1.1] - 2026-10-26

- 补上新版命令帮助。

## [0.1.0] - 2026-10-26

- 首个版本。

[Unreleased]: https://github.com/huangpd/PaperLite/compare/v0.2.5...HEAD
[0.2.5]: https://github.com/huangpd/PaperLite/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/huangpd/PaperLite/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/huangpd/PaperLite/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/huangpd/PaperLite/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/huangpd/PaperLite/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/huangpd/PaperLite/compare/v0.1.10...v0.2.0
[0.1.10]: https://github.com/huangpd/PaperLite/compare/v0.1.8...v0.1.10
[0.1.8]: https://github.com/huangpd/PaperLite/compare/v0.1.4...v0.1.8
[0.1.4]: https://github.com/huangpd/PaperLite/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/huangpd/PaperLite/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/huangpd/PaperLite/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/huangpd/PaperLite/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/huangpd/PaperLite/releases/tag/v0.1.0
