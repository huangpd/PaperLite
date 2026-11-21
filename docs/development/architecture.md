# 架构与设计说明

本文解释 PaperLite 关键行为"为什么这样做"。以下行为都是有意为之，很多来自真实事故（见[事故复盘](postmortems.md)）。改动过盾、熔断、巡检、按块领取的语义前，先读对应小节，避免无意中改回被否决的方案。

## 总体结构

```
          import                         run（一台或多台机器）
URL 文件 ────────▶ PostgreSQL 任务队列 ◀──────────┐
                  pl_tasks / pl_chunks            │ 按块领取 / 批量回写
                                                │
        ┌───────────── 浏览器模式 ──────────────┤
        │  N 个 worker，各自一个浏览器 + 一个 IP │
        │  过盾 → 连续下载 → 跟跳转 → 存文件     │
        └───────────────────────────────────────┤
                                                │
        ┌───────────── curl 模式 ───────────────┘
        │  生产者：浏览器过盾 ─▶ Redis 身份池
        │  下载线程：领身份 + 领任务 ─▶ curl_cffi 下载
        └────────────────────────────────────────

  旁路：monitor.reporter ─▶ RabbitMQ ─▶ paperlite-monitor 面板 ─▶ PostgreSQL（schema paperlite_monitor）
```

- 一份配置 = 一个批次（`[pg] batch`）。任务只存 PostgreSQL：进程按块领取（默认 1000 条）、带租约，块内在本地按顺序下载、结果攒批回写。一台机器和一百台机器是同一套机制。设计见 [PG 按块领取设计](pg-chunk-lease.md)。
- worker（`browser_pool`、`consumer`）通过 `pgqueue.PgTaskSource` 的 `claim` / `finish` / `retry_later` / `challenged` 领任务、交结果，不直接写 SQL。
- 浏览器模式不碰 Redis；curl 模式的生产者和下载线程只通过 Redis 交换身份，没有直接调用。
- Redis 不存代理密码，只存 `ip:port`，下载线程用本地 `config.toml` 补鉴权。
- 监控是旁路：业务代码只调用 `monitor.reporter` 的埋点函数，未启用时为空操作，绝不阻塞下载。

## 身份 = IP + cookie + UA

`cf_clearance` 同时绑定 IP 和 UA，因此三者一起保存、一起使用，更换其中任何一个都会得到 403。身份的有效期取 IP 租期与 cookie 有效期中较早者，所以需要持续生产身份。

## curl 身份验收

浏览器过盾不等于 `curl_cffi` 一定能访问。生产者在身份入 Redis 前，用同一 IP、UA 和 cookie 请求 `[landing]`；失败的身份不入池。

下载时若遇到 Cloudflare 的 `Access denied` / `Attention Required`（403/429），丢弃该身份、任务放回 `pending`，不把这次拒绝当作论文永久失败。

cookie 以 `CookieJar` 形式保存，保留同名 cookie 的 domain/path、host-only、secure、expires、HttpOnly、SameSite。生产者验收和下载共用同一个 Jar，响应更新或删除 cookie 后会保存最新状态；保存不会延长身份或代理的有效期。

## 按消费生产

curl 模式的生产目标 = min(`max_identities`, 在线下载线程数, 可领取任务数)。下载进程每 5 秒把自己剩余的可领取任务数（数到线程数为止）上报到 Redis，多台机器下同一主机时求和。下载全部退出或任务领完，生产者就停止过盾，常驻的 `produce` 只是空转等待，不消耗 IP。

代价是冷启动：第一个身份过盾约需 20 秒。旧的 `produce --keep N` 预热会在没人下载时持续消耗 IP，已删除。

## 过盾要么很快，要么卡死

成功的过盾通常耗时 12–30 秒，失败的则停在 "Verifying…" 直至超时。超过 `hedge_after_s`（45 秒）仍未通过时另开一个浏览器补位，原浏览器继续运行到 `pass_timeout_s`（90 秒）。机房 IP 常被要求人工勾选，因此实际上必须使用代理。

## URL 被挑战不等于身份失效

站点会单独盯上某些 URL，而且往往是连续的一批。遇到挑战时，先用落地页复验身份：

- 落地页正常，说明只是这条 URL 被盯上：降低其优先级（`challenge_count + 1`），领取时排到后面。
- 落地页也被挑战，才丢弃该身份。

如果以"下一条是否也被挑战"来判断身份，会把健康的身份成批误杀：被盯上的论文往往是相邻的一批。

## IP 中途失效

短效 IP 常在租期结束前就断开，表现为 `ERR_TUNNEL_CONNECTION_FAILED`、`ERR_PROXY_*`、`ERR_CONNECTION_CLOSED/RESET`、`ERR_EMPTY_RESPONSE`。这类错误归因于 IP，而不是任务本身：

- 任务放回 `pending`，`error_kind=network`，不增加重试次数和挑战次数。
- 浏览器模式：同一浏览器连续失败两次即提前更换 IP。
- curl 模式：连接被断（curl 56）先用同一身份重试一次（代理偶尔成批断连）；仍失败或 IP 被站点封禁时丢弃身份，任务放回且不计重试。

## 熔断

连续 `max_fresh_rejects` 个新身份首次使用即被拒时，生产者暂停 `breaker_pause_s` 并告警，之后放行一个身份试探。这样可以避免代理配额耗尽时一分钟内烧掉数百个 IP。网络错误不计入阈值。

## 不做卷期停止

旧版在 curl 模式下有"卷期停止"：同一卷期接连几篇都没有 PDF，就把该卷期其余任务直接记为 `non_pdf`、不再请求。它只能从 ACM 式 DOI（`10.xxxx/数字.数字`）里认出卷期，对别的出版商不起作用，而且推断出来的结果没有实际请求过，容易误判。已移除；"成片失效"由下面的重复标题巡检覆盖。

## 流式下载

超时按卡顿时长计算（`download_timeout_s` 内收不到数据），而不是总时长，因此慢代理上的大文件也能完整下载。有效 PDF 必须在前 1024 字节内包含 `%PDF-`。

## 重复标题巡检

浏览器模式。DOI 是唯一的，因此大量页面标题相同，通常意味着错误页或封面页。

1. 任意已加载页面（成功页及 404 等失效页，不含过盾超时页）的标题在最近 `title_window_size` 条里重复达到 `title_dup_trigger` 次时，归纳出能解释这些重复的 DOI/URL 前缀。候选取最长公共前缀及其有限的粗化，选**最具体**的规则，而不是命中最多的。
2. 用已采数据验证规则：精确率 ≥ `rule_min_precision`（0.98）且命中数 ≥ `rule_min_support`。
3. 通过后才把命中的 pending 任务标记为跳过（`flag = suspect_cover`），规则记入 `skip_rules`。

该机制宁可少跳过，也不误跳仍然有效的任务。

巡检有两处：

- **下载时（浏览器模式）**：`WindowInspector` 只看本进程最近 `title_window_size` 条已加载页，命中后只跳过**本进程领到的块**里的任务，不查、不改全库，也不需要跨机器协调。导入时按 (主机, DOI) 排过序，同一期刊基本在同一块里，信号大部分都在本机。
- **`paperlite inspect`**：用全批次已采数据归纳，跳过全批次命中的 pending。已经发到别的机器本地队列里的任务这一轮照常下载（它们不会回库查标记）。

两处命中的任务都记 `skip_reason = title_dup`（保持 pending、不再领取），规则都记入 `pl_skip_rules`。早期版本的问题和修复过程见[事故复盘](postmortems.md)。

## 导入按 (主机, DOI) 排序

同一出版商、同一期刊的 DOI 前缀相同。导入时在 PostgreSQL 里按 (主机, DOI) 排序再插入、切块：一块只打一个站点，同一期刊的任务连在一起，浏览器少在站点间来回切换，巡检的信号也集中在同一块里。

## 为什么只用 PostgreSQL

任务队列要支持多台机器共享一个批次（按块领取、租约、并发领取不重复），这些靠数据库本身的行锁和 `SKIP LOCKED` 实现最简单。PostgreSQL 用一条 `docker run` 就能起来，单机用它也没有额外负担，所以单机多机用同一套实现，不维护第二种存储。任务队列和监控面板数据都在里面（不同 schema）。

## 输出存储

输出文件只有顺序写，可以放 goofys、s3fs 等对象存储挂载（`[paths] storage`）。文件路径按 `sha256(doi)` 的前两级分桶，避免单目录文件过多；DOI 全局唯一，不同站点的文件也不会撞。
