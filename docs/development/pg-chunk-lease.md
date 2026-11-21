# PG 按块领取设计

目标：多台机器（目标 100 台、每天几千万篇）共享一个批次的任务队列。任务队列只有这一种实现：`paperlite.pgqueue`，
一台机器也是同一套机制。部署与使用见[部署 PostgreSQL](../guide/postgres.md)和[多机部署](../guide/multi-machine.md)。本文讲为什么这样设计、怎么实现。

## 0. 组成

| 类 | 职责 |
|---|---|
| `PgTaskQueue` | 一个进程一个实例、一条连接。SQL 都在这里：导入、切块、领块、续租、回写、重试（`reset`）、导出（`rows`）、全批次巡检、统计 |
| `PgTaskSource` | 给下载 worker 的任务来源：按块领取 → 本地队列 → 结果攒批回写；下载时的块内巡检 |
| `LeaseKeeper` | 后台线程续租一块 |

`cli._open_db` 读 `[pg]` 建 `PgTaskQueue` + `PgTaskSource`；`retry` / `export` / `inspect` 直接用 `PgTaskQueue`。

## 1. 为什么按块，而不是逐条领

逐条领取时，每篇论文都要和 PG 交互两次（领取、回写），3000 个 worker 同时抢队首：

- 领取热点：`SKIP LOCKED` 要跳过一串被别人锁住的行，并发越高越慢；
- 状态列在索引里，每次更新都是非 HOT 更新，死元组堆积，autovacuum 压力大；
- 每台机器拿到的是相隔很远的零散任务，巡检这类"本机连续看到相邻任务"的判断失效。

按块领取：一块 = 按 `(host, doi)` 排好序后连续的约 1000 条。机器一次领一块，块内在本地按顺序处理，
结果攒批回写。PG 的交互从"每篇两次"变成"每块一次领取 + 几次批量回写"。

## 2. 实测（本机 Docker postgres:16，100 个 worker 各一条连接，只测调度开销、不下载）

| | 吞吐（篇/秒） | 说明 |
|---|---|---|
| 按块领取（1000 条/块，每 500 条回写一次） | **约 18,400** | 100 万条 54 秒做完，1000 块无重复领取 |
| 逐条 `SKIP LOCKED` 领取 + 单条回写 | 约 930 | 5 万条 54 秒 |

需求是每天 3000 万篇 ≈ 平均 350 篇/秒、峰值约 1000 篇/秒：逐条领取在笔记本上刚好卡在峰值，
按块领取有约 18 倍余量。导入 100 万条约 11 秒（COPY + 按 host、doi 排序插入），切块约 9 秒。

复现：`uv run --extra pg python tools/bench_pg_chunks.py --dsn ... --tasks 1000000 --workers 100`

## 3. 表结构

- `pl_tasks`：与上游 `paper_pdf_resources` 同名同义列，另加 `batch`、`host`、`chunk_id`、`next_attempt_at`、
  `worker`、`skip_reason`（`title_dup`：巡检跳过，保持 pending、不再领取）；
  没有 `running` 状态（块内任务领取时不改状态，省掉每篇一次写）。`fillfactor = 80`。
- `pl_chunks`：`chunk_id, batch, generation, order_key, host, size, first_doi, last_doi, status(queued/leased/done),
  worker, lease_until, attempts`。

导入时 `INSERT ... ORDER BY host, doi`，id 顺序即领取顺序。

## 4. 生命周期

```
import_urls ─▶ build_chunks（每 host 每 1000 条一块，换 host 必切）
                    │
                    ▼
 queued ──claim_chunk（SKIP LOCKED）──▶ leased ──complete──▶ done
   ▲                                     │
   └──────────租约过期（reap_expired）───┘
```

- **领取** `claim_chunk`：先回收过期租约，再按 `(generation, order_key)` 取第一块 queued。
  块内取 `pending AND skip_reason IS NULL AND next_attempt_at 已到`，按 `(challenge_count, id)` 排序。
  块里没有可领任务就直接记 done，继续领下一块。
- **续租** `LeaseKeeper`：后台线程每 `lease/6` 续一次（默认租约 30 分钟）。续租失败 = 块已被别人回收，
  `lost` 置位，worker 停止领这块剩下的任务（已下的结果照常上报）。
- **回写** `report`：批量 `executemany`，只改仍为 `pending` 的行。块被回收后两台机器都做了同一条时，先写的生效，
  后写的忽略；下载按 DOI 定路径，重复下载结果一致，所以"至少一次"就够了。
- **重试** `Result(status=RETRY, retry_delay_s=...)`：任务留在 pending，写 `next_attempt_at`，**不在本块内重试**；
  块里其余任务做完就 complete，难啃的任务不拖住整块。
- **下一代** `requeue_leftovers`：把 done 块里到点的剩余 pending 任务重新切块，`generation + 1`。
  第 0 代全部领完才轮到第 1 代：被挑战 / 要重试的任务排到后面。advisory lock 保证同一批次只有一处在切。

## 5. 接入方式：`PgTaskSource`

worker（`consumer.Downloader`、`browser_pool.BrowserPool`）用到的接口：
`claim / finish / retry_later / challenged / count_claimable / has_status / host / hosts / counts`。
巡检器（`WindowInspector`）需要的两个钩子 `count_pending_matching` / `flag_suspects` 也由它实现，只作用于本地队列：

| 接口 | PgTaskSource 的做法 |
|---|---|
| `claim` | 本地队列有就弹出；空了领下一块（上一块还有在途任务也照领）。远端没块时先 `requeue_leftovers`；本进程还有块在收尾就等它们出结果再判断，避免浏览器模式提前收工、把刚放回的重试任务丢在库里 |
| `finish` / `retry_later` / `challenged` | 进缓冲区，每 `flush_n` 条或 `flush_s` 秒回写；一块的任务都出结果 → 先回写再 `complete` |
| `count_claimable` | 本地排队数 + 远端是否还有块（每 5 秒最多查一次 PG）；curl 模式每个线程每轮都会调，不能每次打 PG |
| `has_status(running)` | 本进程在途任务数 |
| `close` | 回写剩余结果；没做完的块 `release` 回 queued，别的机器马上能领，不用等租约过期 |
| 租约丢失 | 本地还没领的任务放弃（别的机器在做），在途的结果照常上报 |

## 6. 各功能怎么落在块上

| 功能 | 做法 |
|---|---|
| 巡检（`title_dup_trigger`） | 下载时只在本机领到的块里跳过，见下；`paperlite inspect` 对全批次做一次 |
| 被挑战排后（`challenge_count`） | `Result(challenged=True)`：块内排序靠后；放回重试时进入下一代 |
| 重试退避 | `next_attempt_at` |
| 手动重试（`retry`） | `reset`：放回 pending、清零计数，`requeue_leftovers` 切进下一代 |
| 导出（`export`） | `rows`：服务端游标流式读出 |
| 身份池（curl 模式） | 不变，继续用 Redis |
| 跨机器的按站点限速 / 并发上限 | 待设计：Redis 计数（每个 host 的在线 worker 数、令牌桶） |
| 进度和统计 | `stats()`；大表上改为汇总表，不要直接 `GROUP BY` 全表（待做） |
| 卷期停止 | 不做：卷期号只能从 ACM 式 DOI（`10.xxxx/数字.数字`）里认，针对性太强、不通用 |

**为什么下载时的巡检只在块内做**：全批次标记要么每篇查一次 PG，要么已经发到别的机器本地队列里的任务看不到标记。
导入时按 (host, doi) 排过序，同一期刊基本在同一块里，信号大部分在本机，块内判断就够了，也不需要跨机器协调。

**块内巡检**：
- 仍是 `WindowInspector`（本机最近 `title_window_size` 条已加载页），规则归纳和精确率护栏不变；
  只是"有多少待采匹配""标记哪些"换成本地队列：命中的记 `skip_reason = title_dup`，规则记入 `pl_skip_rules`（`scope = chunk`）。
- 代价：精确率只在本机窗口上验证；别的机器手里的同前缀任务不受影响，它们自己的窗口会各自触发。

**全批次巡检**（`paperlite inspect`）：`PgTaskQueue.crawled_titles / count_pending_matching / flag_suspects`，
用全批次已采数据归纳，跳过全批次命中的 pending（`scope = batch`）。

**撤销**：`clear_skips(reason)` 清掉 `skip_reason` 和规则、切成下一代块重新下载（`paperlite inspect --reset`）。

## 7. 参数

| 参数 | 默认 | 取舍 |
|---|---|---|
| 块大小 | 1000 | 太小：PG 交互多；太大：机器挂了重做多、批次收尾慢。30 线程的机器约 10～30 分钟做完一块 |
| 租约 | 1800 秒，每 300 秒续一次 | 要长于一块的正常耗时，续期覆盖慢块 |
| 回写批量 | 500 条或 5 秒 | 机器崩溃时最多丢这么多结果（会因块被回收而重做，不会丢任务） |

## 8. 待研究

1. ~~接入 worker~~：已完成，见第 5 节。
2. **收尾**：剩余块少于机器数时，把块拆小（例如 100 条），避免最后几块拖住整个批次。
3. **毒块**：`attempts` 过多的块（每次领到都让机器崩溃）要隔离，不再自动回收。
4. **大表运维**：按 batch 分区、done 块的任务归档；死元组和 autovacuum 参数在真实负载下观察。
5. **连接数**：100 台机器 × 每进程 1 条连接可以直连；进程更多时加 pgbouncer（transaction 模式；
   `SKIP LOCKED` 和 advisory xact lock 都兼容）。
6. **结果缓冲**：PG 维护或不可用时，结果先写本地文件，恢复后补写。

## 9. 测试

`tests/test_pgqueue.py`（需要 `PAPERLITE_TEST_PG`，没有时跳过；`tests/test_paperlite.py` 里的 worker 测试也跑在 PG 上）：

- 队列：导入排序和去重、按主机切块、16 线程并发领取不重复、租约过期回收与原主人续租失败、重复上报不覆盖、
  重试退避与下一代、空块跳过、LeaseKeeper；
- PgTaskSource：多线程领完且每条一次、重试在同一次运行里排到最后再做、挑战到上限判 failed、
  close 交还未完成的块、租约丢失放弃本地任务；
- 块内巡检：钩子只作用于本地队列（别的机器手里的块不受影响）、规则记入 `pl_skip_rules`、浏览器模式同标题触发后跳过本块剩余同前缀任务；
- 端到端：3 个 curl `Downloader`（各 2 线程）共享一个批次，30 条每条恰好请求一次；
  2 个 `BrowserPool`（各 2 浏览器）共享一个批次，20 页每页恰好渲染一次；
- `[pg]` 配置解析（不需要 PG）。

```bash
docker run -d --name paperlite-pg -e POSTGRES_PASSWORD=pl -e POSTGRES_DB=paperlite -p 5439:5432 postgres:16-alpine
PAPERLITE_TEST_PG=postgresql://postgres:pl@127.0.0.1:5439/paperlite uv run pytest tests/test_pgqueue.py
```
