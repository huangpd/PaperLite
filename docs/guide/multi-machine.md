# 多机部署（PostgreSQL 后端）

一台机器跑不完一个批次时，把任务队列从本地 SQLite 换成 PostgreSQL：多台机器共享同一个批次，各自领任务、各自下载。

## 什么时候需要

| | SQLite（默认） | PostgreSQL |
|---|---|---|
| 机器数 | 一台 | 任意多台，随时加减 |
| 外部服务 | 无 | 一台 PostgreSQL |
| 配置 | `[paths] db` | `[queue] backend = "pg"` + `[pg]` |
| 领取方式 | 逐条领取 | 按块领取：一块约 1000 条，带租约 |
| 巡检（重复标题跳过） | 全库 | 只在本机领到的块里 |
| 卷期停止 | 有（curl 模式） | 无 |
| `export` / `retry` | 支持 | 暂不支持，用 SQL |

一台机器够用就用 SQLite，不需要额外部署。原理与压测见 [PG 按块领取设计](../development/pg-chunk-lease.md)。

## 架构

所有下载机**对等，没有主从之分**：任何一台都可以导入、都可以下载。需要单独部署的只有一台 PostgreSQL；curl 模式另需一台各机共用的 Redis（浏览器模式不需要）。

```
                    ┌──────────────────────┐
                    │  PostgreSQL（一台）   │  任务表 pl_tasks、块表 pl_chunks
                    └──────────┬───────────┘
          领块 / 续租 / 批量回写 │（每个进程 1 条连接）
     ┌─────────────┬───────────┼───────────┬─────────────┐
  下载机 1       下载机 2     下载机 3    ……         下载机 N
     │             │           │                         │
     └─────────────┴─── 共享存储 /mnt/shared/papers ──────┘
                 （curl 模式另有共用的 Redis 身份池）
```

导入时按 (主机, DOI) 排序并切成块。每台机器一次领一块，块内在本地按顺序下载，结果攒批写回。机器正常退出时交还没做完的块；异常退出时，块的租约到期后由别的机器接手。

## 1. 部署 PostgreSQL

```bash
docker run -d --name paperlite-pg --restart unless-stopped \
  -e POSTGRES_USER=paperlite -e POSTGRES_PASSWORD='强密码' -e POSTGRES_DB=paperlite \
  -p 5432:5432 -v /data/pgdata:/var/lib/postgresql/data \
  postgres:16 -c max_connections=300 -c shared_buffers=4GB
```

| 项目 | 说明 |
|---|---|
| 连接数 | 每个下载进程只占 1 条连接；100 台 × 1 进程，`max_connections=300` 足够。进程更多时加 pgbouncer（transaction 模式） |
| 访问控制 | 只放行内网网段（`pg_hba.conf` 或防火墙限制 5432） |
| 主从复制 | 程序不需要。要高可用可配 PG 流复制，但 `dsn` 必须指向**主库**，程序不读从库 |
| `shared_buffers` | 约为内存的 25% |

第一次导入后表会自动建好，再执行一次（让 autovacuum 跟上频繁的状态更新）：

```sql
ALTER TABLE pl_tasks SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02);
```

## 2. 每台下载机安装

```bash
pip install 'paperlite[pg]'          # [pg] 会装上 PostgreSQL 驱动 psycopg
```

PG 后端发布到 PyPI 之前，从源码构建 wheel 再拷到各台机器：

```bash
# 开发机
uv build                             # 生成 dist/paperlite-X.Y.Z-py3-none-any.whl

# 每台下载机
uv pip install 'paperlite-X.Y.Z-py3-none-any.whl[pg]' --force-reinstall \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

- 版本号与已发布的版本相同时必须加 `--force-reinstall`，否则不会覆盖。
- uv 不读 `pip.conf`，镜像要用 `--index-url` 指定。

验证：

```bash
python -c "import importlib.metadata as m; print(m.version('paperlite'), m.version('psycopg'))"
```

## 3. 配置文件（每台机器同一份）

```toml
[proxy]
api_url  = "..."
username = "..."
password = "..."

[queue]
backend = "pg"                   # 所有命令（import / run / status / inspect）都用 PostgreSQL

[pg]
dsn   = "postgresql://paperlite:强密码@pg-host:5432/paperlite"
batch = "10.1016-2026-11"        # 同一批次的所有机器必须完全一样
# schema        = "paperlite"    # 可选：表放在单独的 schema
# chunk_size    = 1000           # 每块任务数
# lease_s       = 1800           # 块租约秒数，后台每 lease_s/6 续一次
# flush_n       = 500            # 结果攒够多少条回写
# flush_s       = 5.0            # 或攒了多久回写
# retry_delay_s = 0              # 要重试的任务至少等多久再进下一代块

[paths]
storage = "/mnt/shared/papers"   # 建议所有机器挂同一个共享存储、路径一致
log_dir = "log"                  # 日志各写各的本地目录
# db 在 PG 后端下用不到

[limits]
max_browsers = 20
# redis_url = "redis://redis-host:6379/0"   # 只有 curl 模式需要：所有机器指向同一个 Redis

[run]
command = "run"
mode    = "browser"              # 或 "curl"
workers = 20
urls    = ""                     # 下载机保持为空：导入只在一台机器上做
```

注意：

- **`storage`**：PG 里记录的 `file_path` 是下载那台机器上的绝对路径。各存本地盘的话，以后要靠 `worker` 列（`主机名-进程号`）才知道文件在哪台机器，所以建议共享存储、路径一致。
- **`batch`**：区分批次。不同批次可以共用同一个 PG，互不影响。
- **`dsn` 含密码**：`config.toml` 不要提交到 git。
- **`--pg`**：配置文件里没写 `[queue] backend = "pg"` 时，任意命令加 `--pg` 也会临时改用 PostgreSQL。
- **监控**：开了 `[monitor]` 的话，批次名取配置**文件名**，各台机器的配置文件名要一致。

## 4. 导入（任选一台，只做一次）

```bash
paperlite import --urls batch.csv
paperlite status                 # 确认任务数和块数
```

- 导入时在 PG 里按 (主机, DOI) 排序并切好块，不需要事先排序。
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
paperlite status
```

显示各状态任务数、块统计（queued 待领 / leased 领取中 / done 已完成）、巡检跳过数。

直接查 PG（表结构见[任务库](../reference/task-database.md#postgresql-后端)）：

```sql
-- 每台机器在跑哪些块
SELECT worker, count(*) FROM pl_chunks
WHERE batch = '10.1016-2026-11' AND status = 'leased' GROUP BY worker;

-- 最近 10 分钟处理速度
SELECT count(*) FROM pl_tasks
WHERE batch = '10.1016-2026-11' AND status <> 'pending' AND updated_at > now() - interval '10 min';

-- 失败分类
SELECT status, error_kind, count(*) FROM pl_tasks
WHERE batch = '10.1016-2026-11' AND status <> 'pending' GROUP BY 1, 2 ORDER BY 3 DESC;

-- 导出成功的任务（代替 export）
\copy (SELECT doi, url, file_path FROM pl_tasks WHERE batch = '10.1016-2026-11' AND status = 'success') TO 'done.csv' CSV HEADER

-- 把网络错误放回队列（代替 retry）。下载机领不到新块时会自动把它们切成新一代块；
-- 下载机都已退出的话，再启动一台即可
UPDATE pl_tasks SET status = 'pending', error_kind = NULL, retry_count = 0, challenge_count = 0
WHERE batch = '10.1016-2026-11' AND status = 'failed' AND error_kind = 'network';

-- 反复领取仍做不完的块（可能是毒块）
SELECT chunk_id, host, attempts, first_doi FROM pl_chunks
WHERE batch = '10.1016-2026-11' AND attempts >= 3 ORDER BY attempts DESC;
```

## 7. 巡检与 curl 模式

| | 浏览器模式 | curl 模式 |
|---|---|---|
| Redis | 不需要 | 需要，所有机器用同一个 |
| 身份生产 | 无 | 每台的 `run` 都会起生产者，但同一主机同一时刻只有一个在生产（Redis 锁） |
| 巡检 | 有（只跳本机领到的块） | 无 |
| 卷期停止 | 无 | 无（PG 后端不做） |

巡检跳过的任务保持 pending、不再领取，可以放回：

```bash
paperlite inspect --list     # 看跳过了多少
paperlite inspect --reset    # 放回重新下载
```

## 8. 当前限制

- `export` / `retry` 不支持 PG 后端，会直接报错：结果用 SQL 查（见第 6 节）。
- 没有跨机器的按站点限速：机器越多，同一站点的总并发越高。建议先用 2～3 台小规模试跑，观察封禁情况再加机器。
- 毒块不会自动隔离：同一块反复让机器崩溃时会被反复领取，用第 6 节的查询发现后手动处理。
- 每台机器每 30 秒会对整个批次做一次状态统计；批次到几千万行时这会成为 PG 的负担，上量前需要改成汇总表。
- SQLite 与 PG 的数据不互通，没有迁移工具：已在 `tasks.db` 里跑了一半的批次，需要把原始 CSV 重新导入 PG。
