# 部署 PostgreSQL

PaperLite 的任务队列、curl 模式的身份池和监控数据都存在 PostgreSQL 里，不需要其他中间件。一个批次不管跑在一台还是一百台机器上，都只需要一台 PG；不同批次（`[pg] batch` 不同）可以共用同一台。

## 本机试用

```bash
docker run -d --name paperlite-pg -e POSTGRES_USER=paperlite -e POSTGRES_PASSWORD=pl \
  -e POSTGRES_DB=paperlite -p 5432:5432 postgres:16
```

```toml
[pg]
dsn   = "postgresql://paperlite:pl@127.0.0.1:5432/paperlite"
batch = "test"
```

## 正式部署

用仓库里的 [`deploy/pg/docker-compose.yml`](../../deploy/pg/docker-compose.yml)：

```bash
cd deploy/pg
echo "PG_PASS=$(openssl rand -hex 24)" > .env && chmod 600 .env
docker compose up -d
```

**数据存在 Docker 卷 `paperlite-pg-data` 里，宿主机路径是 `/var/lib/docker/volumes/paperlite-pg-data/_data`**（要 root 才能进）。compose 里 `volumes` 那一行右边的 `/var/lib/postgresql/data` 是容器内部的路径，在宿主机上看不到。卷名是固定的：以前用 `docker run -v paperlite-pg-data:/var/lib/postgresql/data` 起过的，换成 compose 会直接沿用原来的数据。

不确定数据在哪时问容器：

```bash
docker inspect paperlite-pg --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
du -sh /var/lib/docker/volumes/paperlite-pg-data/_data
```

**别清理这个卷**：`docker volume prune`、`docker system prune --volumes` 会删掉没有容器在用的卷——PG 容器删掉重建的间隙里卷就是"没人用"的。清理 Docker 时只用 `docker image prune` / `docker container prune`。

宿主机上如果还有 apt 装的 PostgreSQL，它的 `/var/lib/postgresql/16/main` 是另一套库，跟 PaperLite 无关。备份不要直接复制数据目录，用 `pg_dump`（见下面「备份」）。

各机器的配置：

```toml
[pg]
dsn   = "postgresql://paperlite:<PG_PASS>@<PG 服务器内网 IP>:5432/paperlite"
batch = "10.1016-2026-11"
```

| 项目 | 说明 |
|---|---|
| 连接数 | 每个下载进程 1 条连接，监控面板 1 条。compose 里 `max_connections=300`，够 100 台 × 1 进程；进程更多时加 pgbouncer（transaction 模式；程序只用事务级 advisory lock，兼容），别继续堆连接数 |
| 内存参数 | compose 按 16GB 内存的机器配：`shared_buffers=2GB`（约内存 1/4）、`effective_cache_size=8GB`（约 1/2）、`maintenance_work_mem=512MB`（导入建索引、清理更快）、`max_wal_size=4GB`（大批量导入少做检查点）。机器内存不同按比例调 |
| `work_mem` | 16MB。导入几百万行时按 DOI 排序，默认 4MB 会溢写临时文件。它是每个排序/哈希节点的额度，PaperLite 同时跑的重查询只有一两条，不会按连接数放大 |
| 磁盘相关 | `random_page_cost=1.1`、`effective_io_concurrency=200`，按 SSD（云盘）配。真是机械盘改回 4 和 2 |
| `pg_stat_statements` | compose 里预加载，排查慢 SQL 用：第一次连上后 `CREATE EXTENSION IF NOT EXISTS pg_stat_statements;`，再查 `SELECT calls, round(total_exec_time) ms, query FROM pg_stat_statements ORDER BY 2 DESC LIMIT 20;`。只能在启动参数里加，改了要重启 |
| `shm_size` | compose 里是 1g。Docker 默认只有 64MB，大查询会报 `could not resize shared memory` |
| `oom_score_adj` | -800：PG 和下载机（一堆浏览器）在同一台时，内存不够内核先杀浏览器（PaperLite 会换一个重开），不先杀 PG。**不要给 PG 设 `mem_limit`**，那样超限时被杀的正是 PG |
| 监听地址 | 只有本机用时绑 `127.0.0.1`；多机时绑内网 IP（`-p <内网IP>:5432:5432`），可同时绑两个 |
| 访问控制 | 5432 只对下载机和面板机所在的内网网段开放（云安全组 / 防火墙）。**Docker 发布的端口不受 ufw 控制**，要靠安全组 |
| 主从 / 高可用 | 程序不需要。要高可用可配流复制，但 `dsn` 必须指向**主库**，程序不读从库 |
| 已有 PG | 不用 compose 也行：建一个库和用户，`dsn` 指过去即可。表在第一次 `import` 时自动建 |

## 第一次导入之后

表会自动建好，不用手动执行任何 SQL。任务表状态更新频繁、行数上百万，建表时已把它的 autovacuum 阈值调到 2%（默认 20%，四百万行要攒八十万死行才清理一次）。

## 表都在哪

| 表 | 所在 schema | 内容 |
|---|---|---|
| `pl_tasks`、`pl_chunks`、`pl_skip_rules` | `[pg] schema`（默认 `public`） | 任务、块、巡检规则，见[任务库](../reference/task-database.md) |
| `pl_identities`、`pl_id_hosts`、`pl_id_workers`、`pl_id_pending` | `[pg] schema` | curl 模式的身份池（UNLOGGED，临时数据，PG 重启后清空、重新生产） |
| `events`、`page`、`page_h` 等 | `[monitor] schema`（默认 `paperlite_monitor`） | 监控：`events` 中转表（UNLOGGED）、原始事件、小时汇总，见[监控](monitoring.md#数据存储) |

## 备份

任务表记录了每篇论文的下载结果，建议定期备份：

```bash
pg_dump -Fc "postgresql://paperlite:<密码>@<PG 地址>:5432/paperlite" > paperlite-$(date +%F).dump
```

只备份某个批次：

```bash
psql "$DSN" -c "\copy (SELECT * FROM pl_tasks WHERE batch = '10.1016-2026-11') TO 'batch.csv' CSV HEADER"
```

## 清理一个批次

批次跑完、结果导出后可以删掉，释放空间：

```sql
DELETE FROM pl_tasks WHERE batch = '10.1016-2026-11';
DELETE FROM pl_chunks WHERE batch = '10.1016-2026-11';
DELETE FROM pl_skip_rules WHERE batch = '10.1016-2026-11';
VACUUM (ANALYZE) pl_tasks;
```
