# 部署 PostgreSQL

PaperLite 的任务队列和监控面板数据都存在 PostgreSQL 里。一个批次不管跑在一台还是一百台机器上，都只需要一台 PG；不同批次（`[pg] batch` 不同）可以共用同一台。

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

各机器的配置：

```toml
[pg]
dsn   = "postgresql://paperlite:<PG_PASS>@<PG 服务器内网 IP>:5432/paperlite"
batch = "10.1016-2026-11"
```

| 项目 | 说明 |
|---|---|
| 连接数 | 每个下载进程 1 条连接，监控面板 1 条。compose 里 `max_connections=300`，够 100 台 × 1 进程；进程更多时加 pgbouncer（transaction 模式） |
| `shared_buffers` | compose 里是 1GB，按内存的 25% 左右调 |
| 访问控制 | 5432 只对下载机和面板机所在的内网网段开放（云安全组 / 防火墙）。**Docker 发布的端口不受 ufw 控制**，要靠安全组 |
| 主从 / 高可用 | 程序不需要。要高可用可配流复制，但 `dsn` 必须指向**主库**，程序不读从库 |
| 已有 PG | 不用 compose 也行：建一个库和用户，`dsn` 指过去即可。表在第一次 `import` 时自动建 |

## 第一次导入之后

表会自动建好。任务表的状态更新很频繁，执行一次让 autovacuum 跟得上：

```sql
ALTER TABLE pl_tasks SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02);
```

用了 `[pg] schema` 的话，先 `SET search_path TO <schema>;`。

## 表都在哪

| 表 | 所在 schema | 内容 |
|---|---|---|
| `pl_tasks`、`pl_chunks`、`pl_skip_rules` | `[pg] schema`（默认 `public`） | 任务、块、巡检规则，见[任务库](../reference/task-database.md) |
| `page`、`page_h` 等 | `[monitor] schema`（默认 `paperlite_monitor`） | 监控面板数据，见[监控](monitoring.md#8-数据存储postgresql) |

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
