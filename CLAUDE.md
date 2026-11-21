# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

PaperLite 是从 `paper_downloader/cookie_pool/` 拆出的独立项目：大批量 PDF/网页下载（过 Cloudflare）。**一份 config = 一个批次**（`[pg] batch`），各种主机的 URL 混进一个 PostgreSQL 任务队列，默认用**浏览器模式**统一跑（每浏览器+IP 过盾后连续下、跟跳转，网页存 `.html`、PDF 流式截获）。另有 **curl 模式**（单主机 cookie 池：浏览器过盾存 cookie、curl_cffi 领用批量下，身份池也在 PG），日常少用。**任务只存 PostgreSQL**（单机多机同一套实现，不要再加第二种存储）：`taskq.PgTaskQueue`（`taskq/queue.py`）放全部 SQL（导入、切块、领块、续租、回写、reset、rows、全批次巡检），`taskq.PgTaskSource`（`taskq/source.py`）是给 worker 的任务来源（按块领取 → 本地队列 → 攒批回写）；`cli._open_db` 读 `[pg]` 建它们。一台和一百台机器是同一套机制。`tasks.py` 只剩公共定义（Status、ERROR_KINDS、Task、url_key、derive_doi、pdf_path、`url_problem`），不碰数据库；`url_problem` 在导入时剔除明显坏的链接（内网 IP、非法主机名），只拦明显坏的，公网 IP 放行。输出在 `[paths] storage`（可挂载）。监控面板数据也在 PG（`[monitor] dsn`，留空用 `[pg] dsn`；schema `paperlite_monitor`）。

文档：`README.md` / `README.zh-CN.md` 只是概览（中英同步）；详细文档在 `docs/`（索引 `docs/README.md`）——`docs/reference/`（配置、命令、任务库表结构）、`docs/guide/`（运行模式、部署 PostgreSQL、多机部署、运维、监控、Turnstile、故障排查）、`docs/development/pg-chunk-lease.md`（按块领取设计）、`docs/development/architecture.md`（设计取舍）、`docs/development/postmortems.md`（踩过的坑）。`docs/getting-started.md` 中英两份同步，其余 docs 只写中文。改动涉及过盾/熔断/巡检/按块领取的行为语义时，先读 architecture.md 对应小节——那里写明了"为什么这样设计"，别无意中改回被否决的方案。改了命令、参数或配置字段，同步更新 `docs/reference/` 与 `config.example.toml`，并在 `CHANGELOG.md` 的 Unreleased 下记一笔。

## 常用命令

```bash
uv sync                                   # 装依赖（开发）
docker run -d --name paperlite-pg -e POSTGRES_PASSWORD=pl -e POSTGRES_DB=paperlite -p 5439:5432 postgres:16-alpine
PAPERLITE_TEST_PG=postgresql://postgres:pl@127.0.0.1:5439/paperlite uv run pytest   # 全量（不设则依赖 PG 的用例跳过）
uv run pytest tests/test_config.py::ConfigTest::test_from_dict   # 单个用例（unittest 风格，pytest 直接跑）
uv run paperlite <命令>                   # 运行；子命令见 docs/reference/cli.md
```

测试是 `unittest.TestCase` 子类，用 pytest 跑。依赖 PG 的用例用 `tests/pgutil.needs_pg` 门控（`PAPERLITE_TEST_PG`），身份池（`curl/identity.py`）的测试也在其中；没设时它们跳过，不能当全量通过。测试文件按包对应（`test_curl` / `test_browser` / `test_taskq` / `test_title_dup` / `test_cli` …），共用的替身（`FakeStore`、`Resp`、`ident`、`PROXY`）在 `tests/fakes.py`，测试文件之间不互相 import。

**包结构与依赖方向**（见 docs/development/project-layout.md）：根目录是共享内核（`tasks` `config` `page` `pdf` `proxy` `logutil`；PDF 文件头规则只在 `pdf.py`，页面/挑战判定只在 `page.py`，两种模式共用，别再各写一份），`taskq/` 任务队列、`browser/` 浏览器模式、`curl/` curl 模式、`monitor/` 监控。依赖只能 `cli` → `browser`/`curl`/`taskq` → 根目录 → `monitor.reporter`；`curl` 只借用 `browser.session` / `browser.env`（过盾、渲染）。这条由 `tests/test_layering.py` 检查。`cli.py` 只做命令行本身（参数、读配置、打开队列）：导入在 `taskq/importer.py`，跑下载在 `browser/runner.py` / `curl/runner.py`；库代码给人看的错误抛 `errors.UserError`（由 `cli.main` 打印退出），别在库里 `SystemExit`。PG 里的 advisory lock 名沿用旧模块名（`paperlite.pgqueue.*`、`paperlite.store.schema`），别跟着改名。

## 架构要点（跨文件才看得清的部分）

**两种模式**（`run` 默认 browser，`--curl` 切 curl）：

- **浏览器模式**（主，`browser/pool.py`）：N 个 worker 各独占一个浏览器+IP，过盾后串行领任务下载。**不用身份池**。各种主机混在一个库里，每页各自过盾、跟跳转。
- **curl 模式**（`curl/` 包：`downloader.py` + `producer.py` + `identity.py`，经身份池握手，无直接调用）：`producer` 按"健康身份数 vs 在线下载线程数 vs 可领取任务数"开浏览器过盾攒身份进身份池；`downloader` 下载线程从任务队列领任务、从 `curl/identity.py` 领身份。身份池在 PostgreSQL（与任务队列同一个 `[pg]`，UNLOGGED 表 `pl_identities` / `pl_id_hosts` / `pl_id_workers` / `pl_id_pending`；`FOR UPDATE SKIP LOCKED` 领取、`holder` + `lease_until` 租约、续租和归还都校验 `holder`），不要再引入别的中间件。身份池不存代理密码，只存 `ip:port`，消费者用本地 `config.toml` 补鉴权。须单主机。

**配置结构**（`config.py`）：`[pg]`(dataclass `PgConfig`，必填)=任务队列；`[proxy]`=代理；`[limits]`(dataclass `Limits`)=并发与行为；`[paths]`(dataclass `Paths`)=`storage`/`log_dir`；`[run]`=启动参数（mode 默认 browser）。命令行 `--storage` 覆盖。没有 `--db`/`--pg`/`--out`/`--host`/站点概念；旧字段（`[paths] db`、`[queue]`、`[run] pg`、`volume_stop_after`、`[monitor] db`）会报错提示。

**身份坏不坏的判定是核心陷阱**（见 docs/development/postmortems.md 与 architecture.md）：带有效 cookie 仍被挑战时，**先用参照链接验身份**（最近下载成功的链接，没有时用首页；不再有配置的落地页）——参照正常说明只是这条 URL 被站点单独盯上（`challenge_count+1` 排后面），参照也打不开才算身份坏、丢弃。不要用"下一条也被挑战"来判身份，被盯上的论文成批相邻会误杀好身份。

**浏览器模式下 PDF**：`url_type=pdf` 走 `browser.pdf_capture.PdfCapture`（CDP Fetch 拦截 PDF 响应、流式写盘），别改回 goto 后读页面或点下载按钮；开拦截时必须应答 `Fetch.authRequired`（代理鉴权）。

**浏览器模式不热身**：新浏览器领到的第一篇就是探针（`browser.pool` 的 `_do_task(probe=True)`），首篇失败任务放回不计次数、换 IP；同一主机连续 `_PROBE_STREAK` 个新浏览器首篇失败就按站点问题正常计次，别改成无限换 IP。

**易改错的行为语义**，动之前先对齐 architecture.md / postmortems.md：

- 熔断：连续 `max_fresh_rejects` 个新身份一用就被拒 → 生产者暂停 `breaker_pause_s`；网络错误不计入。
- 按块领取（`taskq/`：SQL 在 `queue.py`，worker 侧在 `source.py`）：领块不改任务状态（库里没有 running），回写只改仍是 pending 的行（至少一次、重复无害）；要重试的留在 pending 写 `next_attempt_at`，块做完由 `requeue_leftovers` 切进下一代；租约丢了放弃本地剩余任务。详见 pg-chunk-lease.md。
- 巡检（`taskq/title_dup.py` + `taskq/queue.py` 的 `skip_reason`/`pl_skip_rules`）：浏览器模式下，任何**真加载出来**的页（成功页 + 404 等失效页，`browser.pool` 的 `_observe` 在成功和 404 分支都喂、不喂 `r.ok=False` 的过盾超时页）标题重复到 `title_dup_trigger` 次就归纳规则。候选取 doi/url 最长公共前缀及其 `_coarsen`（限 `_MAX_COARSEN` 位，防退化成 `/pii/S`），选规则**偏最具体**（最长前缀）而非命中最多，用已采数据验 precision/support 达标才标 pending 跳过。只看内存滑动窗口，按 `(doi_prefix, url_prefix)` 去重，只跳本机领到的块（`PgTaskSource.flag_suspects`）；`paperlite inspect` 另对全批次做（`PgTaskQueue.flag_suspects`）——详见 docs/development/postmortems.md。

**监控（`monitor/`）是旁路**：业务代码只调 `monitor.reporter` 的 `page`/`browser_start`/`proxy_check`（未启用时 no-op、不连库；启用后按批写进 PG 的 `paperlite_monitor.events`，面板 `MonitorDB.consume` 一个事务里取走、汇总、删掉；不要再引入别的中间件），心跳由 `cli._attach_monitor` 挂上。改监控绝不能让下载阻塞或抛错；新增埋点只加一行调用，别把业务逻辑搬进 monitor。指标口径（按尝试计、耗时只算已加载页、WAF = 站点前面是哪家）见 `docs/guide/monitoring.md`「指标口径」，部署见同一文档。面板查询只读小时汇总表（`*_h`），别改回扫原始事件表——那是 30 天 52 秒的旧方案。

## 约束

- **src 布局**：测试跑的是安装好的包，不会误用源码目录。
- **测试要 PG**：`PAPERLITE_TEST_PG=postgresql://...`，辅助在 `tests/pgutil.py`（`make_source` 每个用例一个临时 schema）；没设时依赖 PG 的用例跳过，不能当作全量通过。
- `[paths].storage` 可放对象存储挂载（顺序写）。
- **打包排除敏感文件**：`pyproject.toml` 的 `sdist only-include` 白名单确保 `config.toml`（含代理和数据库密码）、`data/`、`db/`、`pdfs/` 绝不进包。新增需打包的路径时改这里。
- 代理体检在 `proxy.py`：用 curl_cffi **低层 `Curl`** 拿 TCP/TLS/TTFB/总耗时四段，判 2xx + 核对回显出口 IP == proxy_ip + 延迟闸；两个判定目标任一通过即合格，每 IP 记一条 `proxy_check` 结构化日志。
