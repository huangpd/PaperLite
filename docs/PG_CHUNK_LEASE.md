# PG 按块领取（研究分支 `research/pg-chunk-lease`）

目标：多台机器（目标 100 台、每天几千万篇）共享一个批次的任务队列。单机的 SQLite + flock 流程保持不变，
本分支只新增可选模块 `paperlite.pgqueue`（`pip install 'paperlite[pg]'`），CLI 尚未接入。

## 1. 为什么按块，而不是逐条领

逐条领取时，每篇论文都要和 PG 交互两次（领取、回写），3000 个 worker 同时抢队首：

- 领取热点：`SKIP LOCKED` 要跳过一串被别人锁住的行，并发越高越慢；
- 状态列在索引里，每次更新都是非 HOT 更新，死元组堆积，autovacuum 压力大；
- 每台机器拿到的是相隔很远的零散任务，卷期停止、巡检这些"本机连续看到相邻任务"的判断失效。

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

- `pl_tasks`：与 SQLite 的 `paper_pdf_resources` 同名同义列，另加 `batch`、`host`、`chunk_id`、`next_attempt_at`、
  `worker`；没有 `running` 状态（块内任务领取时不改状态，省掉每篇一次写）。`fillfactor = 80`。
- `pl_chunks`：`chunk_id, batch, generation, order_key, host, size, first_doi, last_doi, status(queued/leased/done),
  worker, lease_until, attempts`。

导入时 `INSERT ... ORDER BY host, doi`，id 顺序即领取顺序，导入前不需要 DuckDB 排序。

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
  块内取 `pending AND flag IS NULL AND next_attempt_at 已到`，按 `(challenge_count, id)` 排序。
  块里没有可领任务就直接记 done，继续领下一块。
- **续租** `LeaseKeeper`：后台线程每 `lease/6` 续一次（默认租约 30 分钟）。续租失败 = 块已被别人回收，
  `lost` 置位，worker 停止领这块剩下的任务（已下的结果照常上报）。
- **回写** `report`：批量 `executemany`，只改仍为 `pending` 的行。块被回收后两台机器都做了同一条时，先写的生效，
  后写的忽略；下载按 DOI 定路径，重复下载结果一致，所以"至少一次"就够了。
- **重试** `Result(status=RETRY, retry_delay_s=...)`：任务留在 pending，写 `next_attempt_at`，**不在本块内重试**；
  块里其余任务做完就 complete，难啃的任务不拖住整块。
- **下一代** `requeue_leftovers`：把 done 块里到点的剩余 pending 任务重新切块，`generation + 1`。
  第 0 代全部领完才轮到第 1 代，等价于单机版"被挑战 / 失败的排到后面"。advisory lock 保证同一批次只有一处在切。

## 5. 现有功能在多机下怎么落

| 单机功能 | 多机做法 | 状态 |
|---|---|---|
| 卷期停止（`volume_stop_after`） | 一块基本是同一卷期，在本机块内判断；判停后对 PG 执行 `UPDATE ... WHERE volume = ? AND status = 'pending'` | 待接入 |
| 巡检（`flag` / `skip_rules`） | 本机滑动窗口归纳规则，写共享的 `skip_rules`；标记 pending 后，下次领块时自然过滤 | 待接入 |
| 被挑战排后（`challenge_count`） | `Result(challenged=True)`：块内排序靠后；放回重试时进入下一代 | 已实现 |
| 重试退避 | `next_attempt_at` | 已实现 |
| 身份池（curl 模式） | 不变，继续用 Redis | — |
| 跨机器的按站点限速 / 并发上限 | Redis 计数（每个 host 的在线 worker 数、令牌桶） | 待设计 |
| 进度和统计 | `stats()`；大表上改为汇总表，不要直接 `GROUP BY` 全表 | 部分 |

## 6. 参数

| 参数 | 默认 | 取舍 |
|---|---|---|
| 块大小 | 1000 | 太小：PG 交互多；太大：机器挂了重做多、批次收尾慢。30 线程的机器约 10～30 分钟做完一块 |
| 租约 | 1800 秒，每 300 秒续一次 | 要长于一块的正常耗时，续期覆盖慢块 |
| 回写批量 | 500 条或 5 秒 | 机器崩溃时最多丢这么多结果（会因块被回收而重做，不会丢任务） |

## 7. 待研究

1. **接入 worker**：`consumer.py` / `browser_pool.py` 目前直接调用 `TaskDB.claim/finish`。需要抽一层"任务源"接口：
   单机实现走 TaskDB，多机实现走"领块 → 本地队列 → 攒批回写"。
2. **收尾**：剩余块少于机器数时，把块拆小（例如 100 条），避免最后几块拖住整个批次。
3. **毒块**：`attempts` 过多的块（每次领到都让机器崩溃）要隔离，不再自动回收。
4. **大表运维**：按 batch 分区、done 块的任务归档；死元组和 autovacuum 参数在真实负载下观察。
5. **连接数**：100 台机器 × 每进程 1 条连接可以直连；进程更多时加 pgbouncer（transaction 模式；
   `SKIP LOCKED` 和 advisory xact lock 都兼容）。
6. **结果缓冲**：PG 维护或不可用时，结果先写本地文件，恢复后补写。

## 8. 测试

`tests/test_pgqueue.py`（需要 `PAPERLITE_TEST_PG`，没有时跳过）：导入排序和去重、按主机切块、
16 线程并发领取不重复、租约过期回收与原主人续租失败、重复上报不覆盖、重试退避与下一代、空块跳过、LeaseKeeper。

```bash
docker run -d --name paperlite-pg -e POSTGRES_PASSWORD=pl -e POSTGRES_DB=paperlite -p 5439:5432 postgres:16-alpine
PAPERLITE_TEST_PG=postgresql://postgres:pl@127.0.0.1:5439/paperlite uv run pytest tests/test_pgqueue.py
```
