# 多机部署

一台机器跑不完一个批次时，加机器就行：所有机器用同一份配置（同一个 `[pg] dsn` + `batch`），各自领任务、各自下载。机器**对等，没有主从之分**，随时加减。

任务队列本来就在 PostgreSQL 里，单机和多机用的是同一套机制，不需要切换任何设置。原理与压测见 [PG 按块领取设计](../development/pg-chunk-lease.md)。

## 架构

```
                    ┌──────────────────────┐
                    │  PostgreSQL（一台）   │  任务表 pl_tasks、块表 pl_chunks
                    └──────────┬───────────┘
          领块 / 续租 / 批量回写 │（每个进程 1 条连接）
     ┌─────────────┬───────────┼───────────┬─────────────┐
  下载机 1       下载机 2     下载机 3    ……         下载机 N
     │             │           │                         │
     └─────────────┴─── 共享存储 /mnt/shared/papers ──────┘
                 （curl 模式的身份池也在这台 PostgreSQL 里）
```

导入时按 (主机, DOI) 排序并切成块（默认 1000 条）。每台机器一次领一块，块内在本地按顺序下载，结果攒批写回。机器正常退出时交还没做完的块；异常退出时，块的租约到期后由别的机器接手。

## 1. 部署 PostgreSQL

见[部署 PostgreSQL](postgres.md)。100 台机器 × 每台 1 个进程，`max_connections=300` 足够。

## 2. 每台下载机安装

```bash
pip install paperlite
```

还没发布到 PyPI 的版本，在开发机上 `uv build` 出 wheel，拷到各台机器：

```bash
uv pip install paperlite-X.Y.Z-py3-none-any.whl --force-reinstall \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

- 版本号与已装的相同时必须加 `--force-reinstall`，否则不会覆盖。
- uv 不读 `pip.conf`，镜像要用 `--index-url` 指定。

## 3. 配置文件（每台机器同一份）

```toml
[pg]
dsn   = "postgresql://paperlite:<密码>@pg-host:5432/paperlite"
batch = "10.1016-2026-11"        # 同一批次的所有机器必须完全一样

[proxy]
api_url  = "..."
username = "..."
password = "..."

[paths]
storage = "/mnt/shared/papers"   # 建议所有机器挂同一个共享存储、路径一致
log_dir = "log"                  # 日志各写各的本地目录

[limits]
max_browsers = 20

[run]
command = "run"
mode    = "browser"              # 或 "curl"
workers = 20
urls    = ""                     # 下载机保持为空：导入只在一台机器上做
```

注意：

- **`storage`**：PG 里记录的 `file_path` 是下载那台机器上的绝对路径。各存本地盘的话，以后要靠 `worker` 列（`主机名-进程号`）才知道文件在哪台机器，所以建议共享存储、路径一致。
- **`dsn` 含密码**：`config.toml` 不要提交到 git。
- **监控**：开了 `[monitor]` 的话，批次名取配置**文件名**，各台机器的配置文件名要一致。

## 4. 导入（任选一台，只做一次）

```bash
paperlite import --urls batch.csv
paperlite status                 # 确认任务数和块数
```

- 追加新文件：同样再导入一次；已存在的 URL 不会重置。
- 多台机器同时导入也安全（切块按批次串行），但没必要。

## 5. 各下载机启动

```bash
paperlite run config.toml
```

| 场景 | 行为 |
|---|---|
| 后台常驻 | tmux / nohup / systemd 均可 |
| 加机器 | 新机器启动后自动领还没被领走的块 |
| 同一台机器多开进程 | 可以，每个进程各领各的块；注意浏览器数和内存 |
| 正常停止（Ctrl+C / SIGTERM） | 已有结果写回 PG，没做完的块立刻交还，别的机器马上能接着做 |
| 异常退出（`kill -9`、宕机） | 块等租约过期（默认 30 分钟）后由别的机器接手；已写回的结果保留 |
| 结束 | 领不到新块、手里的块也做完后自动退出；之后有任务到了重试时间，再启动一次就会接着做 |

systemd 示例（`/etc/systemd/system/paperlite.service`）：

```ini
[Unit]
Description=PaperLite worker
After=network-online.target

[Service]
WorkingDirectory=/data/paperlite
ExecStart=/data/paperlite/.venv/bin/paperlite run config.toml
KillSignal=SIGINT
TimeoutStopSec=120
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

SIGINT 和 SIGTERM 都会走正常退出流程（回写结果、交还块）；`TimeoutStopSec` 要留够在途任务收尾的时间。

## 6. 看进度

```bash
paperlite status                 # 各状态任务数、块统计（queued / leased / done）、巡检跳过数
paperlite export --status success --file done.csv
paperlite retry --status failed --kind network
```

直接查 PG（表结构见[任务库](../reference/task-database.md)）：

```sql
-- 每台机器在跑哪些块
SELECT worker, count(*) FROM pl_chunks
WHERE batch = '10.1016-2026-11' AND status = 'leased' GROUP BY worker;

-- 最近 10 分钟处理速度
SELECT count(*) FROM pl_tasks
WHERE batch = '10.1016-2026-11' AND status <> 'pending' AND updated_at > now() - interval '10 min';

-- 反复领取仍做不完的块（可能是毒块）
SELECT chunk_id, host, attempts, first_doi FROM pl_chunks
WHERE batch = '10.1016-2026-11' AND attempts >= 3 ORDER BY attempts DESC;
```

## 7. 巡检与 curl 模式

| | 浏览器模式 | curl 模式 |
|---|---|---|
| 身份池 | 不需要 | 在同一个 PostgreSQL 里，所有机器共用 |
| 身份生产 | 无 | 每台的 `run` 都会起生产者，但同一主机同一时刻只有一个在生产（身份池里的生产者锁） |
| 下载时巡检 | 有，只跳本机领到的块 | 无 |

`paperlite inspect` 可以随时在任一台机器上对全批次做一次巡检。详见[日常运维](operations.md#巡检重复标题跳过)。

## 8. 当前限制

- 没有跨机器的按站点限速：机器越多，同一站点的总并发越高。建议先用 2～3 台小规模试跑，观察封禁情况再加机器。
- 毒块不会自动隔离：同一块反复让机器崩溃时会被反复领取，用第 6 节的查询发现后手动处理。
- 每台机器每 30 秒会对整个批次做一次状态统计；批次到几千万行时这会成为 PG 的负担，上量前需要改成汇总表。
