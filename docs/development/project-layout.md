# 项目结构

```
src/paperlite/
  cli.py              命令行入口：子命令解析、run / import / status / retry / export / inspect
  config.py           配置加载与校验：[pg] [proxy] [paths] [limits] [run]
  config.example.toml paperlite init 生成的配置模板
  tasks.py            任务的公共定义：状态、错误分类、Task、URL 规范化、DOI 推导、文件路径（不碰数据库）
  pgqueue.py          任务队列（PostgreSQL）：导入、切块、按块领取、租约、批量回写、重试、导出、巡检；
                      PgTaskSource 是给下载 worker 的任务来源
  browser_pool.py     浏览器模式：每个 worker 一个浏览器 + 一个 IP，过盾后连续下载
  browser.py          单次过盾：经代理启动独立的 CloakBrowser 进程；BrowserSession.render 打开页面，要 PDF 时开拦截
  pdf_capture.py      浏览器模式下载 PDF：CDP Fetch 拦截 PDF 响应、流式写盘、校验
  turnstile.py        Turnstile 定位、点击与 token 服务
  producer.py         curl 模式：按消费生产身份，控制过盾并发与熔断
  store.py            curl 模式：身份池（PostgreSQL UNLOGGED 表，SKIP LOCKED 领用、租约、生产者锁）
  consumer.py         curl 模式：下载线程、流式下载、身份健康判定
  cookies.py          浏览器 / 身份池 cookie 记录与 CookieJar 互转
  inspect.py          重复标题规则归纳
  classify.py         non-PDF 分类：not_found、paywall、landing_page、blocked
  proxy.py            代理提取与分层体检（TCP / TLS / TTFB / 总耗时）
  monitor/            指标上报（未启用时为空操作，按批写进 PG 的 events 表）与 paperlite-monitor 面板（取走、汇总）
tests/                pytest 测试（unittest 风格）
tools/bench_pg_chunks.py  PG 队列调度压测：按块领取 vs 逐条领取
deploy/pg/            PostgreSQL docker-compose
docs/                 文档
```

## 模块之间的关系

- **任务队列**：`cli._open_db` 读 `[pg]` 返回 `pgqueue.PgTaskSource`，下面的 worker 都通过它领任务、回写结果。
- **浏览器模式**：`cli` → `browser_pool` → `browser`（过盾）+ 任务队列（领任务、回写）+ `inspect`（巡检）。
- **curl 模式**：`producer`（`browser` 过盾 → `store` 入池）与 `consumer`（`store` 领身份 + 任务队列领任务）只通过身份池交换数据。
- **监控**：业务代码只调用 `monitor.reporter` 的 `page` / `browser_start` / `proxy_check`，心跳由 `cli` 挂上。

设计取舍见[架构与设计说明](architecture.md)。
