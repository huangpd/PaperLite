# 项目结构

```
src/paperlite/
  # 根目录：共享内核，两种模式都用；这里的模块不 import 下面的子包（cli 除外）
  cli.py              命令行：参数解析、读配置、打开任务队列，再调下面各模块；只放命令行本身的事
  errors.py           UserError：给人看的错误，库代码抛它，命令行打印后退出（库代码不直接 SystemExit）
  config.py           配置加载与校验：[pg] [proxy] [paths] [limits] [run]（含 ProxySettings）
  config.example.toml paperlite init 生成的配置模板
  tasks.py            任务的公共定义：状态、错误分类、Task、URL 规范化与校验、DOI 推导、文件路径（不碰数据库）
  page.py             页面判定：Cloudflare 挑战 / 封禁、WAF 识别、non-PDF 分类（not_found、paywall、landing_page、blocked）
  pdf.py              PDF 文件头规则（两种模式共用）
  proxy.py            代理提取与分层体检（TCP / TLS / TTFB / 总耗时）
  logutil.py          日志格式（text / json）

  taskq/              任务队列（PostgreSQL，按块领取）
    queue.py          PgTaskQueue：全部 SQL —— 建表、导入、切块、领块 / 续租 / 回写、重试、导出、跳过规则
    source.py         PgTaskSource + LeaseKeeper：下载 worker 这一侧（本地队列、攒批回写、后台续租）
    title_dup.py      巡检：同标题重复 → 归纳 doi/url 规则、跳过疑似失效的任务
    importer.py       导入：读 txt / CSV（DuckDB 按 DOI 排序）、写进队列、坏链接写 .bad_url.csv

  browser/            浏览器模式（默认）
    session.py        单个浏览器：经代理启动 CloakBrowser、过盾、render 打开页面（要 PDF 时开拦截）、地理库
    pool.py           worker 池：每个 worker 一个浏览器 + 一个 IP，首篇探测、换 IP、结果回写
    pdf_capture.py    PDF 下载：CDP Fetch 拦截 PDF 响应、流式写盘、校验
    turnstile.py      Turnstile 定位、点击与 token 服务
    env.py            浏览器进程的环境变量（session 和 turnstile 共用）
    runner.py         跑一次浏览器模式：开浏览器池、打进度、挂监控心跳、收尾汇总

  curl/               curl 模式
    producer.py       生产者：按消费生产身份，控制过盾并发与熔断（过盾借用 browser.session）
    downloader.py     下载线程：流式下载、身份健康判定
    identity.py       身份池（PostgreSQL UNLOGGED 表，SKIP LOCKED 领用、租约、生产者锁），只有 curl 模式用
    http.py           curl_cffi 会话（按浏览器版本选 TLS 指纹）、Retry-After 解析；生产者和下载线程共用
    cookies.py        浏览器 / 身份池 cookie 记录与 CookieJar 互转
    runner.py         跑 curl 模式：生产者线程 + 下载线程、打进度、挂监控心跳（run --curl / produce / download）

  monitor/            监控：上报端（reporter / events / schema，未启用时为空操作）+ paperlite-monitor 面板
                      （ingest 取走汇总、db 查询、dashboard 页面）
tests/                pytest 测试（unittest 风格）
tools/bench_pg_chunks.py  PG 队列调度压测：按块领取 vs 逐条领取
deploy/pg/            PostgreSQL docker-compose
docs/                 文档
```

## 模块之间的关系

- **依赖方向**：`cli` → `browser` / `curl` / `taskq` → 根目录的共享模块 → `monitor.reporter`。反方向不允许；
  `curl` 对 `browser` 的依赖只有过盾和一次性渲染（`browser.session`、`browser.env`）。`tests/test_layering.py` 扫描全部 import 检查这条规则。
- **命令行**：`cli` 读配置、`_open_db` 打开任务队列（`taskq.PgTaskSource`），导入交给 `taskq.importer`，跑下载交给 `browser.runner.run_browser` / `curl.runner.run_curl`（db 交给 runner，由它关闭）。库代码出错抛 `errors.UserError`，`cli.main` 统一打印后退出。
- **任务队列**：worker 都通过 `taskq.PgTaskSource` 领任务、回写结果。
- **浏览器模式**：`cli` → `browser.pool` → `browser.session`（过盾、渲染）+ 任务队列（领任务、回写）+ `taskq.title_dup`（巡检）。
- **curl 模式**：`curl.producer`（`browser.session` 过盾 → `curl.identity` 入池）与 `curl.downloader`（`curl.identity` 领身份 + 任务队列领任务）只通过身份池交换数据。
- **监控**：业务代码只调用 `monitor.reporter` 的 `page` / `browser_start` / `proxy_check`，心跳由 `cli` 挂上。

设计取舍见[架构与设计说明](architecture.md)。
