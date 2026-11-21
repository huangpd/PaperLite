# 任务库

任务在 `[pg]` 指向的 PostgreSQL 里（`[pg] schema`，默认 `public`），第一次导入时自动建表。多个批次共用同一组表，用 `batch` 列区分；下面的查询都要带上 `batch = '...'`。

| 表 | 内容 |
|---|---|
| `pl_tasks` | 每个 URL 一行：任务与下载结果 |
| `pl_chunks` | 块：按 (主机, DOI) 排好序后连续的一段任务，进程按块领取 |
| `pl_skip_rules` | 巡检归纳出的跳过规则 |

为什么按块领取、块的生命周期，见 [PG 按块领取设计](../development/pg-chunk-lease.md)。

## `pl_tasks`

列名与上游 `paper_pdf_resources` 表同名同义，便于对接。

| 列 | 说明 |
|---|---|
| `id` | 自增主键。导入时按 (主机, DOI) 排序插入，id 顺序即领取顺序 |
| `batch` | 批次名，即 `[pg] batch` |
| `url_key` | 规范化 URL 的 SHA-256，唯一。规范化 = 协议和域名转小写、去掉 `#` 片段；路径与参数原样保留 |
| `doi` | 导入值（转小写）；没给则从 URL 推导（DOI 或 PII），认不出用 URL 哈希。决定文件名 |
| `url`、`source`、`batch_no` | 导入时的原值 |
| `url_type` | `pdf`（期望得到 PDF）或 `html`（保存网页本身） |
| `host` | URL 的主机名，切块按它分组（一块只打一个站点） |
| `chunk_id` | 当前所属的块 |
| `status`、`error_kind`、`error_message` | 见下文 |
| `skip_reason` | `title_dup`：巡检跳过，保持 `pending`、不再领取；`inspect --reset` 清除 |
| `next_attempt_at` | 要重试的任务最早可再领的时间 |
| `final_url`、`title` | 跟随跳转后实际保存的地址；页面标题 |
| `http_status`、`file_size`、`elapsed_ms`、`exit_ip` | 最近一次尝试的指标 |
| `retry_count` | 网络错误或 5xx 的重试次数 |
| `challenge_count` | 带有效 cookie 仍被挑战的次数（站点单独盯上了这条 URL），领取时排在块内后面 |
| `file_path` | `<storage>/<h[0:2]>/<h[2:4]>/<doi>.pdf`（网页为 `.html`），其中 `h = sha256(doi)`；是下载那台机器上的绝对路径 |
| `worker` | 最后处理它的进程（`主机名-进程号`） |
| `created_at`、`updated_at` | `timestamptz` |

## `pl_chunks`

| 列 | 说明 |
|---|---|
| `chunk_id`、`batch` | 块号、批次 |
| `generation` | 第几代：第 0 代是导入时切的；之后每轮把剩下要重试的任务再切成下一代，前一代领完才轮到下一代 |
| `order_key`、`host`、`first_doi`、`last_doi`、`size` | 块在排序中的位置、主机、首尾 DOI、任务数 |
| `status` | `queued`（待领）→ `leased`（领取中）→ `done` |
| `worker`、`lease_until` | 持有者与租约到期时间；到期未续租即被回收为 `queued` |
| `attempts` | 被领取的次数；反复领取仍做不完可能是毒块 |

## `pl_skip_rules`

| 列 | 说明 |
|---|---|
| `doi_prefix`、`url_prefix` | 规则：两个前缀都命中才跳过；空串 = 不限该维度 |
| `title` | 触发规则的重复标题 |
| `support`、`precision` | 规则在已采数据上的命中数和精确率 |
| `flagged` | 据此跳过的任务数（多台机器各自触发同一规则时累加） |
| `scope` | `batch` = `paperlite inspect` 全批次巡检；`chunk` = 下载时本机块内巡检 |

## status

```
pending ──领块──▶（本进程在途，不落库）──▶ success
   ▲                                  ├─▶ non_pdf
   └──── 重试：留在 pending，进下一代块 ┘└─▶ failed
```

| 值 | 含义 |
|---|---|
| `pending` | 等待下载（包括别的机器正在下载的、等待重试的、巡检跳过的） |
| `success` | 拿到了 PDF，或 `html` 任务拿到了网页 |
| `non_pdf` | 页面正常加载但没有 PDF（`html` 任务没有这个状态） |
| `failed` | 重试用完仍失败 |

领取时不改状态（省掉每篇一次写），所以库里没有 `running`；`paperlite status` 显示的 `running` 是本进程的在途数。

## error_kind

| 值 | 出现在 | 含义 |
|---|---|---|
| `not_found` | `non_pdf` / `failed` | 站点 404 页，DOI 不存在 |
| `paywall` | `non_pdf` | 页面提示需要订阅 |
| `landing_page` | `non_pdf` | 只有摘要页，没有全文入口 |
| `blocked` | `pending` | 站点封了该 IP（如 ACM 的 "IP blocked"）：丢弃这个身份，任务放回队列 |
| `network` | `pending` / `failed` | 连接失败、超时、IP 中途失效。IP 中途失效时任务放回且不计重试次数；普通网络错误重试 `max_tries` 次后记 `failed` |
| `server_error` | `failed` | 5xx |
| `rate_limited` | `failed` | HTTP 429 在 `Retry-After` 等待后仍未恢复（站点限流，不是 Cloudflare 封禁） |
| `challenge_failed` | `failed` | 带有效 cookie 仍被挑战满 `max_url_challenges` 次 |
| `extract_failed` | `failed` | 拿到的 PDF 过小、超过大小上限或不是有效 PDF |

`not_found` / `paywall` / `landing_page` 的判定规则在 `page.py`，按 ACM 页面编写。

## 导出列

`paperlite export` 输出的列：`id, doi, url, url_type, host, source, batch_no, final_url, title, status, error_kind, skip_reason, http_status, file_size, elapsed_ms, exit_ip, retry_count, challenge_count, file_path, error_message, worker, chunk_id, updated_at`。

## 常用查询

```sql
-- 状态与失败分类
SELECT status, error_kind, count(*) FROM pl_tasks WHERE batch = 'B' GROUP BY 1, 2 ORDER BY 3 DESC;

-- 块进度（按代）
SELECT generation, status, count(*) FROM pl_chunks WHERE batch = 'B' GROUP BY 1, 2 ORDER BY 1, 2;

-- 各主机进度
SELECT host, status, count(*) FROM pl_tasks WHERE batch = 'B' GROUP BY 1, 2 ORDER BY 1, 2;

-- 最近一小时的成功数
SELECT count(*) FROM pl_tasks WHERE batch = 'B' AND status = 'success' AND updated_at > now() - interval '1 hour';

-- 某个 DOI 前缀的结果
SELECT status, error_kind, count(*) FROM pl_tasks WHERE batch = 'B' AND doi LIKE '10.1145/%' GROUP BY 1, 2;
```

更多查询（各机器在跑的块、处理速度、毒块）见[多机部署](../guide/multi-machine.md#6-看进度)。运行中查询是安全的；改数据请用 `retry` / `inspect --reset` 等命令。
