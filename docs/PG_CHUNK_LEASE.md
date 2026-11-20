# PG 按块领取（研究分支 `research/pg-chunk-lease`）

目标：多台机器（目标 100 台、每天几千万篇）共享一个批次的任务队列。单机的 SQLite + flock 流程保持不变，
本分支新增可选模块 `paperlite.pgqueue`（`pip install 'paperlite[pg]'`），命令加 `--pg` 时启用。

## 0. 用法

每台机器的 config.toml 加 `[pg]` 段（`batch` 相同即共享同一批次）：

```toml
[pg]
dsn = "postgresql://user:pass@pg-host:5432/paperlite"   # 含密码，不要提交
batch = "10.1016-2026-11"
# schema = "paperlite"     # 可选
# chunk_size = 1000        # 每块任务数
# lease_s = 1800           # 块租约秒数，后台每 lease_s/6 续一次
# flush_n = 500            # 结果攒够多少条回写
# flush_s = 5.0            # 或攒了多久回写
# retry_delay_s = 0        # 要重试的任务至少等多久再进下一代块
```

```bash
paperlite import --pg --urls batch.csv     # 任一台机器导一次：COPY 入库、按 (host, doi) 排序、切块
paperlite run --pg --workers 20            # 每台机器各跑一个；浏览器模式 / --curl 都支持
paperlite status --pg                      # 任务状态 + 块统计（queued / leased / done）
```

巡检在 PG 模式下**只在本机领到的块里做**（见第 6 节）。被跳过的任务保持 pending、不再领取，可以放回：

```bash
paperlite inspect --pg --list              # 看巡检跳过了多少
paperlite inspect --pg --reset             # 放回巡检跳过的
```

PG 模式**不做卷期停止**（`volume_stop_after` 被忽略）。`export` / `retry` 暂不支持 `--pg`。

## 1. 为什么按块，而不是逐条领

逐条领取时，每篇论文都要和 PG 交互两次（领取、回写），3000 个 worker 同时抢队首：

- 领取热点：`SKIP LOCKED` 要跳过一串被别人锁住的行，并发越高越慢；
- 状态列在索引里，每次更新都是非 HOT 更新，死元组堆积，autovacuum 压力大；
- 每台机器拿到的是相隔很远的零散任务，卷期停止这类"本机连续看到相邻任务"的判断失效。

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
  `worker`、`skip_reason`（`title_dup`：块内巡检跳过，保持 pending、不再领取）；
  没有 `running` 状态（块内任务领取时不改状态，省掉每篇一次写）。`fillfactor = 80`。
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
  块内取 `pending AND skip_reason IS NULL AND next_attempt_at 已到`，按 `(challenge_count, id)` 排序。
  块里没有可领任务就直接记 done，继续领下一块。
- **续租** `LeaseKeeper`：后台线程每 `lease/6` 续一次（默认租约 30 分钟）。续租失败 = 块已被别人回收，
  `lost` 置位，worker 停止领这块剩下的任务（已下的结果照常上报）。
- **回写** `report`：批量 `executemany`，只改仍为 `pending` 的行。块被回收后两台机器都做了同一条时，先写的生效，
  后写的忽略；下载按 DOI 定路径，重复下载结果一致，所以"至少一次"就够了。
- **重试** `Result(status=RETRY, retry_delay_s=...)`：任务留在 pending，写 `next_attempt_at`，**不在本块内重试**；
  块里其余任务做完就 complete，难啃的任务不拖住整块。
- **下一代** `requeue_leftovers`：把 done 块里到点的剩余 pending 任务重新切块，`generation + 1`。
  第 0 代全部领完才轮到第 1 代，等价于单机版"被挑战 / 失败的排到后面"。advisory lock 保证同一批次只有一处在切。

## 5. 接入方式：`PgTaskSource`

worker（`consumer.Downloader`、`browser_pool.BrowserPool`）只用到 TaskDB 的一小部分接口：
`claim / finish / retry_later / challenged / count_claimable / has_status / host / hosts / counts`。
`PgTaskSource` 实现同样的接口，worker 代码不用改。巡检器（`WindowInspector`）需要的两个钩子
`count_pending_matching` / `flag_suspects` 也由它实现，只作用于本地队列：

| 接口 | PgTaskSource 的做法 |
|---|---|
| `claim` | 本地队列有就弹出；空了领下一块（上一块还有在途任务也照领）。远端没块时先 `requeue_leftovers`；本进程还有块在收尾就等它们出结果再判断，避免浏览器模式提前收工、把刚放回的重试任务丢在库里 |
| `finish` / `retry_later` / `challenged` | 进缓冲区，每 `flush_n` 条或 `flush_s` 秒回写；一块的任务都出结果 → 先回写再 `complete` |
| `count_claimable` | 本地排队数 + 远端是否还有块（每 5 秒最多查一次 PG）；curl 模式每个线程每轮都会调，不能每次打 PG |
| `has_status(running)` | 本进程在途任务数 |
| `close` | 回写剩余结果；没做完的块 `release` 回 queued，别的机器马上能领，不用等租约过期 |
| 租约丢失 | 本地还没领的任务放弃（别的机器在做），在途的结果照常上报 |

## 6. 现有功能在多机下怎么落

| 单机功能 | 多机做法 | 状态 |
|---|---|---|
| 卷期停止（`volume_stop_after`） | 不做：卷期号只能从 ACM 式 DOI（`10.xxxx/数字.数字`）里认，针对性太强、不通用 | 移除 |
| 巡检（`title_dup_trigger`，浏览器模式） | 只在本机领到的块里跳过，见下 | 已实现 |
| 被挑战排后（`challenge_count`） | `Result(challenged=True)`：块内排序靠后；放回重试时进入下一代 | 已实现 |
| 重试退避 | `next_attempt_at` | 已实现 |
| 身份池（curl 模式） | 不变，继续用 Redis | — |
| 跨机器的按站点限速 / 并发上限 | Redis 计数（每个 host 的在线 worker 数、令牌桶） | 待设计 |
| 进度和统计 | `stats()`；大表上改为汇总表，不要直接 `GROUP BY` 全表 | 部分 |

**为什么巡检只在块内做**：单机版的判断依赖全库状态（全库批量标记），多机下要么每篇查一次 PG，
要么已经发到别的机器本地队列里的任务看不到标记。导入时按 (host, doi) 排过序，同一卷期、同一期刊基本在同一块里，
信号大部分在本机，块内判断就够了，也不需要跨机器协调。

**巡检**：
- 仍是 `WindowInspector`（本机最近 `title_window_size` 条已加载页），规则归纳和精确率护栏不变；
  只是"有多少待采匹配""标记哪些"换成本地队列：命中的记 `skip_reason = title_dup`。
- 代价：精确率只在本机窗口上验证（单机版也是窗口）；别的机器手里的同前缀任务不受影响，它们自己的窗口会各自触发。

**撤销**：`clear_skips(reason)` 清掉 `skip_reason`、切成下一代块重新下载（CLI 见第 0 节）。

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

`tests/test_pgqueue.py`（需要 `PAPERLITE_TEST_PG`，没有时跳过）：

- 队列：导入排序和去重、按主机切块、16 线程并发领取不重复、租约过期回收与原主人续租失败、重复上报不覆盖、
  重试退避与下一代、空块跳过、LeaseKeeper；
- PgTaskSource：多线程领完且每条一次、重试在同一次运行里排到最后再做、挑战到上限判 failed、
  close 交还未完成的块、租约丢失放弃本地任务；
- 卷期停止：PG 模式下 `volume_stop_after` 被忽略，同卷期接连没有 PDF 也逐条请求；
- 块内巡检：钩子只作用于本地队列（别的机器手里的块不受影响）、浏览器模式同标题触发后跳过本块剩余同前缀任务；
- 端到端：3 个 curl `Downloader`（各 2 线程）共享一个批次，30 条每条恰好请求一次；
  2 个 `BrowserPool`（各 2 浏览器）共享一个批次，20 页每页恰好渲染一次；
- `[pg]` 配置解析和 `--pg` 开关（不需要 PG）。

```bash
docker run -d --name paperlite-pg -e POSTGRES_PASSWORD=pl -e POSTGRES_DB=paperlite -p 5439:5432 postgres:16-alpine
PAPERLITE_TEST_PG=postgresql://postgres:pl@127.0.0.1:5439/paperlite uv run pytest tests/test_pgqueue.py
```
