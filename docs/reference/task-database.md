# 任务库

任务存在哪里由 `[queue] backend` 决定：

- **SQLite**（默认）：`[paths].db` 指向的文件，任务在 `paper_pdf_resources` 表，见下文前半部分。
- **PostgreSQL**：任务在 `pl_tasks` 表，按块领取的状态在 `pl_chunks` 表，见[后半部分](#postgresql-后端)。

两种后端的列名和取值尽量一致（`status`、`error_kind`、指标列相同），查询可以对照着写。

## SQLite 后端

### `paper_pdf_resources`

| 列 | 说明 |
|---|---|
| `id` | 自增主键，按此顺序领取任务（被挑战过的排后，见 `challenge_count`） |
| `url_key` | 规范化 URL 的 SHA-256，唯一。规范化 = 协议和域名转小写、去掉 `#` 片段；路径与参数原样保留 |
| `doi` | 导入值（转小写）；没给则从 URL 推导（DOI 或 PII），认不出用 URL 哈希。决定文件名 |
| `url`、`source`、`batch_no` | 导入时的原值 |
| `url_type` | `pdf`（期望得到 PDF）或 `html`（保存网页本身） |
| `final_url` | 跟随跳转后实际保存的地址 |
| `title` | 页面标题 |
| `flag` | 巡检标记：`suspect_cover` 表示疑似封面或失效页，不再领取 |
| `volume` | DOI 卷期号（`10.1145/1012326.1012328` → `1012326`），用于卷期停止；新式纯数字 DOI 为空 |
| `status` | 见下文 |
| `error_kind` | 见下文 |
| `error_message` | 错误详情 |
| `http_status`、`file_size`、`elapsed_ms`、`exit_ip` | 最近一次尝试的指标 |
| `retry_count` | 网络错误或 5xx 的重试次数 |
| `challenge_count` | 带有效 cookie 仍被挑战的次数（站点单独盯上了这条 URL） |
| `inferred` | 为 1 表示结果由卷期停止推断得出，没有实际请求 |
| `file_path` | `<storage>/<h[0:2]>/<h[2:4]>/<doi>.pdf`（网页为 `.html`），其中 `h = sha256(doi)` |
| `claimed_by` | 领取该任务的 worker |
| `created_at`、`updated_at` | 北京时间文本 `YYYY-MM-DD HH:MM:SS` |

### 辅助表

| 表 | 内容 |
|---|---|
| `meta` | 键值对，如第一个导入的主机 |
| `volume_stops` | 被停止的卷期：卷期号、判停依据的样本 DOI、跳过的任务数、时间 |
| `skip_rules` | 巡检规则：`doi_prefix`、`url_prefix`、触发标题、命中数、精确率、标记数 |

### 常用查询

```bash
# 状态与失败分类
sqlite3 tasks.db "SELECT status, error_kind, count(*) FROM paper_pdf_resources GROUP BY 1, 2"

# 最近一小时的成功数
sqlite3 tasks.db "SELECT count(*) FROM paper_pdf_resources
                  WHERE status = 'success' AND updated_at > datetime('now', '+8 hours', '-1 hour')"

# 各主机进度
sqlite3 tasks.db "SELECT substr(url, 1, instr(substr(url, 9), '/') + 8) AS host, status, count(*)
                  FROM paper_pdf_resources GROUP BY 1, 2"

# 某个 DOI 前缀的结果
sqlite3 tasks.db "SELECT status, error_kind, count(*) FROM paper_pdf_resources WHERE doi LIKE '10.1145/%' GROUP BY 1, 2"
```

运行中查询是安全的（只读）；不要在下载进程运行时手动改表，用 `retry` / `inspect --reset` 等命令。

## PostgreSQL 后端

表在 `[pg]` 指定的库（和 `schema`）里，第一次导入时自动创建。多个批次共用同一组表，用 `batch` 列区分。

### `pl_tasks`

与 `paper_pdf_resources` 同名同义的列：`id`、`url_key`、`doi`、`url`、`url_type`、`final_url`、`title`、`status`、`error_kind`、`error_message`、`http_status`、`retry_count`、`challenge_count`、`file_path`、`file_size`、`elapsed_ms`、`exit_ip`、`created_at`、`updated_at`（`timestamptz`）。

PostgreSQL 特有的列：

| 列 | 说明 |
|---|---|
| `batch` | 批次名，即 `[pg] batch` |
| `host` | URL 的主机名，切块按它分组 |
| `chunk_id` | 当前所属的块 |
| `next_attempt_at` | 重试任务最早可再领的时间 |
| `skip_reason` | `title_dup`：块内巡检跳过，保持 `pending`、不再领取；`inspect --reset` 清除 |
| `worker` | 最后处理它的进程（`主机名-进程号`），文件存各机本地盘时靠它找文件 |

与 SQLite 的差别：没有 `running` 状态（块里的任务领取时不改状态，省掉每篇一次写），没有 `volume`、`inferred`、`flag`。

### `pl_chunks`

| 列 | 说明 |
|---|---|
| `chunk_id`、`batch` | 块号、批次 |
| `generation` | 第几代：第 0 代是导入时切的，之后每轮把剩下要重试的任务再切成下一代 |
| `order_key`、`host`、`first_doi`、`last_doi`、`size` | 块在排序中的位置、主机、首尾 DOI、任务数 |
| `status` | `queued`（待领）→ `leased`（领取中）→ `done` |
| `worker`、`lease_until` | 持有者与租约到期时间；到期未续租即被回收为 `queued` |
| `attempts` | 被领取的次数；反复领取仍做不完可能是毒块 |

### 常用查询

```sql
-- 状态与失败分类
SELECT status, error_kind, count(*) FROM pl_tasks WHERE batch = 'B' GROUP BY 1, 2;

-- 块进度
SELECT generation, status, count(*) FROM pl_chunks WHERE batch = 'B' GROUP BY 1, 2 ORDER BY 1, 2;
```

更多查询（各机器在跑的块、处理速度、导出、放回重试）见[多机部署](../guide/multi-machine.md#6-看进度)。

## status

两种后端通用。

```
pending ──领取──▶ running ──▶ success
   ▲                 │    ├─▶ non_pdf
   └──重试 / 中断────┘    └─▶ failed
```

| 值 | 含义 |
|---|---|
| `pending` | 等待下载 |
| `running` | 正在下载；进程中断后下次启动会自动放回 `pending`。仅 SQLite 后端 |
| `success` | 拿到了 PDF，或 `html` 任务拿到了网页 |
| `non_pdf` | 页面正常加载但没有 PDF（`html` 任务没有这个状态） |
| `failed` | 重试用完仍失败 |

## error_kind

两种后端通用。

| 值 | 出现在 | 含义 |
|---|---|---|
| `not_found` | `non_pdf` | 站点 404 页，DOI 不存在 |
| `paywall` | `non_pdf` | 页面提示需要订阅 |
| `landing_page` | `non_pdf` | 只有摘要页，没有全文入口 |
| `blocked` | `pending` | 站点封了该 IP（如 ACM 的 "IP blocked"）：丢弃这个身份，任务放回队列 |
| `network` | `pending` / `failed` | 连接失败、超时、IP 中途失效。IP 中途失效时任务放回 `pending` 且不计重试次数；普通网络错误重试 `max_tries` 次后记 `failed` |
| `server_error` | `failed` | 5xx |
| `rate_limited` | `failed` | HTTP 429 在 `Retry-After` 等待后仍未恢复（站点限流，不是 Cloudflare 封禁） |
| `challenge_failed` | `failed` | 带有效 cookie 仍被挑战满 `max_url_challenges` 次 |
| `extract_failed` | `failed` | 拿到的 PDF 过小、超过大小上限或不是有效 PDF |

`not_found` / `paywall` / `landing_page` 的判定规则在 `classify.py`，按 ACM 页面编写。
