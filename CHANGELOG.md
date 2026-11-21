# 更新日志

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.3.0] - 2026-11-21

### 不兼容变更

- **任务队列只用 PostgreSQL**，移除 SQLite 任务库：配置文件必须有 `[pg]`（`dsn`、`batch`）。删除 `[paths] db`、`--db`、`[queue]`、`--pg`、`[run] pg`；旧配置里写了这些会报错并提示怎么改。没有迁移工具：已有的 `tasks.db` 批次需要把原始 CSV 重新导入。
- **监控面板数据改存 PostgreSQL**：`[monitor] db` 换成 `[monitor] dsn`（留空用 `[pg] dsn`）和 `[monitor] schema`（默认 `paperlite_monitor`）；`paperlite-monitor --db` 换成 `--dsn` / `--schema`。
- **移除卷期停止**：`[limits] volume_stop_after`、`retry --stopped-volumes` 删除（只适用 ACM 式 DOI，不通用）。
- `psycopg` 成为默认依赖；`paperlite[pg]` 保留为空的兼容写法。
- **监控只用 PostgreSQL**：下载机按批（每 2 秒或 1000 条）把事件写进 `paperlite_monitor.events`（UNLOGGED 中转表），面板在一个事务里取走、汇总、删掉，不丢不重；面板停机时事件在表里排队（上限 `backlog_max`）。旧的上报连接配置项删除（写了会报错并提示删掉），新增 `flush_s`、`backlog_max`；`dsn` / `schema` 两端共用；`paperlite-monitor test` 改为验证能写进监控库、面板是否在消费。原始事件默认保留从 48 小时改为 6 小时（大屏只读小时汇总）。删除对应的依赖和 `deploy/monitor` 部署文件。至此任务队列、身份池、监控全部在 PostgreSQL 里，不需要其他中间件。
- **curl 模式的身份池改存 PostgreSQL**：和任务队列同一个 `[pg]`（UNLOGGED 表，`SKIP LOCKED` 领取，租约、生产者锁、熔断、上报都在表里）。旧的身份池连接配置项删除（写了会报错并提示删掉），对应依赖一并去掉。续租、在线记录限频（≤ 每 30 / 20 秒）。身份池测试改为跑在 PostgreSQL 上，CI 每次都跑。
- **去掉热身和 `[landing]`**：写了 `[landing]` 会报错并提示删掉。
  - 浏览器模式：新浏览器不再先打开落地页，领到的第一篇就是探针。首篇打不开归咎于 IP：任务原样放回、不计次数，换 IP；同一站点连续 3 个新浏览器首篇都失败，就认定是站点问题，之后正常计次，不会无限换 IP。不走代理时规则相同（重开浏览器）。
  - curl 模式：生产者的浏览器在站点首页过盾拿 cookie；入池前用真实链接验收（最近下载成功的链接 > 队列里一条待下载链接 > 首页）；下载被挑战时用最近成功的链接复验身份。最近成功链接经身份池在各机器间共享。
- **导入时剔除明显下载不到的链接**，不入库：内网 / 本机 IP（`10.x`、`172.16–31.x`、`192.168.x`、`127.x` 等）、主机名不合法（以点开头如 `https://.asm.org/…`、没有点如 `http://0`、`http://null/…`）、不是 http/https。明细写到输入文件旁边的 `<文件名>.bad_url.csv`。以前一条没有主机名的链接会让整个导入中止，现在只跳过这一条。这些链接来自出版商在 DOI 系统里登记的错误地址，换成 `https://doi.org/<DOI>` 也会跳到同一个坏地址（hw1 上 244 条实测）。公网 IP 形式的主机放行。

### 新增

- 任务队列按块领取：导入时按 (主机, DOI) 排序切块（默认 1000 条），进程按块领取、带租约，结果批量写回；正常退出交还未做完的块，异常退出由租约到期回收。一台或多台机器用同一份配置即可共享一个批次。
- 浏览器模式能下 PDF：`url_type=pdf` 的任务用 CDP 拦截浏览器收到的 PDF 响应、流式写盘（校验 `%PDF-`、大小、长度，同 curl 模式），走浏览器自己的网络栈，过盾后的身份直接可用。实测 ACM `dl.acm.org/doi/pdf/10.1145/100348.100354`：过 Cloudflare 挑战（点击 2 次）后 20 秒拿到完整 6 页 PDF。
- `status` 显示块统计（queued / leased / done）和巡检跳过数。
- 监控面板的 WAF 统计只算页面采集；浏览器启动成功率改为"新浏览器首篇打开的比例"。
- `inspect` 对全批次巡检；下载时浏览器模式在本机块内实时巡检；两者的规则都记入 `pl_skip_rules`，`inspect --list` 可查。
- `export` 流式导出，`retry` 放回后自动切进下一代块；导入保留 `source`、`batch_no`。
- `deploy/pg/docker-compose.yml`；`tools/bench_pg_chunks.py`（PG 队列调度压测）。
- CI：每次推送连 PostgreSQL 跑全量测试（`ci.yml`）；发布前同样跑。

### 修复

- `paperlite import` 不写 `--type` 时取配置文件 `[run] url_type`，不再固定为 `pdf`（配置写着 `html`、单独导入却全部记成 `pdf`）。
- 浏览器模式不再把"要 PDF 却打开的是网页"的任务记成成功：截不到 PDF、停在网页上时记 `non_pdf`（按页面分类），网页存到 `non_pdf/`，与 curl 模式一致。
- 浏览器热身用批次里任务最多的主机，不再用排序第一的主机（数据里一条 `https://.asm.org/…` 坏链接曾让整个批次热身失败）。
- 某一块的任务全被巡检跳过时，curl 模式的下载线程不再误判为还有可领任务。

### 文档

- README 精简为概览，详细内容拆到 `docs/`：使用指南、配置参考、命令参考、任务库、架构与设计说明。
- 新增 `CONTRIBUTING.md`、`CHANGELOG.md`、快速上手（中英双语）、故障排查、部署 PostgreSQL、多机部署、PG 按块领取设计。
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

- 监控上报的依赖改为默认安装；监控去掉证书 / TLS 配置。

## [0.2.1] - 2026-11-10

### 修复

- 发布流程的测试装上监控依赖。

## [0.2.0] - 2026-11-10

### 新增

- 监控模块：终端上报运行指标，`paperlite-monitor` 汇总并提供面板。
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

[Unreleased]: https://github.com/huangpd/PaperLite/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/huangpd/PaperLite/compare/v0.2.5...v0.3.0
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
