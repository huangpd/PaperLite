# 监控

每次 `paperlite run` 都可以把运行指标写进 PostgreSQL（监控 schema `paperlite_monitor` 的 `events` 中转表）。`paperlite-monitor serve` 从中取走事件、汇总入库（保留 30 天），并提供网页面板，可切换最近 24 小时、7 天、30 天视图。

上报和面板都只和 PostgreSQL 交互，不需要别的中间件；默认就用任务队列那个库（`[pg] dsn`），监控表放在自己的 schema 里，与任务表分开。监控完全是旁路：未启用、PG 写失败时，下载都不受影响。上报内容只有统计数据与出口 IP，不含代理密码和页面内容。

## 快速试用

```bash
paperlite-monitor demo          # 用模拟数据启动面板（写入 schema paperlite_monitor_demo），打开 http://127.0.0.1:8700/
                                # 需要配置文件里有 [pg] dsn 或 [monitor] dsn
```

各下载机启用上报：

```toml
[monitor]
enabled = true
vendor  = "qingguo"             # 代理厂商标签，用于「厂商 × 域名」统计
# dsn 留空 = 用 [pg] 的 dsn
```

```bash
paperlite-monitor test          # 在下载机上验证：能写进监控库、面板是否在消费
paperlite-monitor serve         # 在面板机上启动，地址 http://HOST:8700/
```

## 面板内容

| 区块 | 指标 |
|---|---|
| IP 质检 | 质检次数、通过率、平均延迟、不通过原因 |
| 浏览器 | 在线数与目标数、启动成功率（新浏览器首篇打开的比例）、异常数、首篇平均过盾耗时 |
| 页面采集 | 成功数、失败数、成功率、过盾率、平均耗时与 P95 |
| 域名 | 各域名的成功率、过盾率、耗时与吞吐；服务端搜索、排序、分页；可按主域名合并 |
| 代理厂商 | 厂商 × 域名成功率；各厂商 IP 质检通过率 |
| WAF | Cloudflare、AWS WAF、DataDome 的过盾率与耗时，按响应头识别，只统计页面采集 |
| 实时 | 待下载与进行中任务数、各域名访问频率、实时事件流、终端列表 |

## 指标口径

- 页面指标**按尝试计**：过盾超时后重试的任务，每次尝试各计一次。
- "过盾"指页面实际加载出来；"成功"指过盾且 HTTP 状态码小于 400。
- 耗时只统计已加载的页面。
- 样本少于 20 次的域名灰显。
- 成功率或过盾率低于 70% 的域名标记为异常。

## 架构

```
 下载机 A（paperlite run）─┐  每 2 秒按批 INSERT
 下载机 B（paperlite run）─┼──────────────────────► PostgreSQL：paperlite_monitor.events（中转，UNLOGGED）
 下载机 C（paperlite run）─┘                                    │
                                                                 │ paperlite-monitor serve：一个事务里
                                                                 │ 取走一批 → 汇总入库 → 删掉
                                                                 ▼
                                             paperlite_monitor 的原始事件表 + 小时汇总表
                                                                 │
 运维浏览器 ──────────── http://面板机:8700（HTTP + 登录）◄──────┘
```

- **上报端**：事件先进本地有界缓冲（默认 1 万条），后台线程每 `flush_s`（2 秒）或攒够 1000 条写一次，用自己的一条连接，不和任务队列共用。PG 连不上时退避重连、缓冲满了丢最旧的，绝不阻塞下载。
- **面板端**：取、写、删在同一个事务里——面板崩溃或写库出错就整批回滚，事件留在表里下次再处理，不丢不重。面板停机期间事件在 `events` 里排队，最多 `backlog_max`（200 万）条，超出丢最旧的。
- 汇总只有面板一个进程在写，不会多写者冲突。

## 部署面板

面板可以和 PG 放一台机器，也可以单独一台；它只需要能连到 PG、能被浏览器访问。

### 安装

```bash
sudo useradd -r -m -d /opt/paperlite-monitor -s /usr/sbin/nologin paperlite
sudo -u paperlite python3 -m venv /opt/paperlite-monitor/.venv
sudo -u paperlite /opt/paperlite-monitor/.venv/bin/pip install paperlite
```

面板只用到 `paperlite-monitor` 命令和 PostgreSQL，不需要浏览器、代理配置。

### 配置

`/opt/paperlite-monitor/config.toml`：

```toml
[monitor]
dsn      = "postgresql://paperlite:<PG 密码>@<PG 地址>:5432/paperlite"   # 与下载机的 [pg] dsn 同一个库
# schema = "paperlite_monitor"
bind     = "0.0.0.0"                     # 对外监听
port     = 8700
username = "admin"                       # 网页登录用户名
password = "<面板网页登录密码>"          # openssl rand -hex 16
# token = ""                     # 可选：给脚本 / 外部监控调 /api/summary 用
# retention_days = 30            # 大屏数据（小时汇总）保留天数
# raw_retention_hours = 6        # 原始事件保留小时数
```

```bash
sudo chown paperlite: /opt/paperlite-monitor/config.toml && sudo chmod 600 /opt/paperlite-monitor/config.toml
```

保险：`bind` 不是本机地址却没设 `password` / `token` → **拒绝启动**；没有 `dsn` → 拒绝启动。

### systemd 常驻

`/etc/systemd/system/paperlite-monitor.service`：

```ini
[Unit]
Description=PaperLite monitor dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=paperlite
WorkingDirectory=/opt/paperlite-monitor
ExecStart=/opt/paperlite-monitor/.venv/bin/paperlite-monitor --config /opt/paperlite-monitor/config.toml serve
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/paperlite-monitor

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now paperlite-monitor
journalctl -u paperlite-monitor -f
# 应看到：监控大屏 http://127.0.0.1:8700/ … 以及 开始消费监控事件（PostgreSQL schema paperlite_monitor 的 events 表）
curl -s http://127.0.0.1:8700/health    # {"ok": true}
```

PG 晚于面板启动、或中途重启都没关系：面板会退避重连。

## 下载机接入

在该批次的 `config.toml` 里加一段（其余不动）：

```toml
[monitor]
enabled = true
vendor  = "qingguo"      # 代理厂商标签
# node  = "gpu-01"       # 终端名，默认主机名
```

先验证，再照常跑：

```bash
paperlite-monitor --config config.toml test
# OK：测试心跳已写入 postgresql://paperlite:***@… schema=paperlite_monitor，终端 …
# 面板正在消费（「终端」表里会出现 batch=monitor-test）
paperlite run config.toml
```

面板「浏览器与终端」表里出现该终端即接入成功。批次名取配置文件名（`sciencedirect.toml` → `sciencedirect`）。

### 可选：上报用只能写的账号

下载机本来就用 `[pg] dsn` 连任务队列。如果监控想用单独的账号（比如监控库是另一台 PG），给终端建一个只能往 `events` 写、什么都读不了的账号，下载机的 `[monitor] dsn` 用它：

```sql
CREATE ROLE pl_terminal LOGIN PASSWORD '<密码>';
GRANT USAGE ON SCHEMA paperlite_monitor TO pl_terminal;
GRANT INSERT ON paperlite_monitor.events TO pl_terminal;
GRANT USAGE ON SEQUENCE paperlite_monitor.events_id_seq TO pl_terminal;
```

表由面板第一次启动时建好；`paperlite-monitor test` 用这种账号时看不到面板是否消费，只报告写入成功。

## 防火墙与安全组

| 端口 | 来源 |
|---|---|
| 8700（面板） | 运维人员的 IP（尽量别对全网） |
| 5432（PG） | 只放行下载机和面板机所在的内网网段，见[部署 PostgreSQL](postgres.md) |
| 22 | 你的运维 IP |

> ⚠️ **Docker 发布的端口不受 ufw 控制**（Docker 直接改 iptables），限制来源要靠云安全组。

## 数据存储

都在 `[monitor] schema`（默认 `paperlite_monitor`）里，第一次启动时自动建好。

| 层 | 表 | 保留 | 用途 |
|---|---|---|---|
| 中转 | `events`（UNLOGGED） | 处理完即删；积压上限 `backlog_max` | 下载机 → 面板 |
| 原始事件 | `page` `browser` `proxy_check` `log` | `raw_retention_hours`（默认 6 小时） | 实时事件流、近 5 分钟域名频率、排查明细 |
| 小时汇总 | `page_h` `browser_h` `proxy_h` `proxy_reason_h` `fleet_h` | `retention_days`（默认 30 天） | 大屏 24 小时 / 7 天 / 30 天的全部数字和趋势 |
| 终端 | `instance` | 30 天未上报即删 | 每个终端最新心跳（在线数、待下载） |

入库时先按小时聚合，所以大屏查询代价与事件量无关。P95 用每小时的耗时直方图合并得出，误差约 ±6%。过期数据每小时清理一次。

### 容量与对 PG 的压力

一个 20 浏览器的终端每天约 5 万次页面尝试 + 几千次 IP 质检 + 5760 次心跳（15 秒一次），平均约 1 条/秒。

| 规模 | 写入 | 原始事件（6 小时） | 小时汇总 |
|---|---|---|---|
| 10 个终端 | 约 10 条/秒，每 2 秒一批 | 约 20 万行 | 每天约 `24 × 域名数 × 厂商数` 行 |
| 100 个终端 | 约 100 条/秒，合计每秒约 50 次小批量 INSERT | 约 200 万行（约 0.5 GB） | 同上，30 天也只有几十万行 |

实测（本机 Docker postgres:16，30 天模拟数据）：入库约 6900 条/秒；大屏查询 24 小时 / 7 天 / 30 天 0.008s / 0.014s / 0.026s。真正占空间的是原始事件明细，量大时先调小 `raw_retention_hours`；任务队列压力也大时，把 `[monitor] dsn` 指到另一台 PG。

### 备份

监控数据丢了不影响下载，备份不是必须的：

```bash
pg_dump -n paperlite_monitor -Fc "postgresql://paperlite:<密码>@<PG 地址>:5432/paperlite" > monitor-$(date +%F).dump
```

### 清空 / 迁移

- **清空重来**：`systemctl stop paperlite-monitor`，`DROP SCHEMA paperlite_monitor CASCADE`，再 start（自动重建）。
- **换面板机**：面板进程是无状态的，新机器按本文部署、`dsn` 指向同一个 PG 即可，下载机不用改。

### 直接查库

```sql
SET search_path TO paperlite_monitor;
SELECT host, SUM(n) AS 尝试, ROUND(SUM(ok)::numeric / SUM(n), 3) AS 成功率 FROM page_h
WHERE hour >= extract(epoch FROM now())::int / 3600 - 24 GROUP BY host ORDER BY 2 DESC;

SELECT count(*) FROM events;      -- 待处理事件：面板在跑时应接近 0
```

也可以带 token 调接口拿 JSON：`curl -s 'http://面板机:8700/api/summary?window=7d&token=<token>'`（`window` = `1d` / `7d` / `30d`）。

## 配置项一览（`[monitor]`）

| 配置项 | 默认 | 用在 | 说明 |
|---|---|---|---|
| `enabled` | `false` | 下载机 | `true` 才上报 |
| `node` | 主机名 | 下载机 | 终端名；面板按 `node:pid` 区分进程 |
| `vendor` | `unknown` | 下载机 | 代理厂商标签 |
| `heartbeat_s` | `15` | 下载机 | 心跳间隔；面板 `max(60s, 4×heartbeat_s)` 没收到判离线 |
| `buffer_size` | `10000` | 下载机 | 本地发送缓冲条数 |
| `flush_s` | `2.0` | 下载机 | 按批写入的间隔 |
| `forward_logs` | `true` | 下载机 | WARNING 以上日志也作为实时事件上报 |
| `dsn` | 空（用 `[pg].dsn`） | 两端 | 监控用的 PostgreSQL |
| `schema` | `paperlite_monitor` | 两端 | 监控表所在 schema |
| `backlog_max` | `2000000` | 面板 | 待处理事件积压上限，超出丢最旧的 |
| `bind` / `port` | `127.0.0.1` / `8700` | 面板 | 监听地址；对外用 `0.0.0.0` |
| `username` / `password` | `admin` / 空 | 面板 | 网页登录（HTTP Basic）；`password` 非空即开启 |
| `token` | 空 | 面板 | 可选的接口令牌（`?token=` 或请求头 `X-Token`） |
| `retention_days` | `30` | 面板 | 小时汇总保留天数 |
| `raw_retention_hours` | `6` | 面板 | 原始事件保留小时数 |

旧版的 `amqp_url`、`exchange`、`queue`、`queue_max_length`、`db` 写了会报错并提示删掉。

## 日常运维

| 要做的事 | 怎么做 |
|---|---|
| 面板是否活着 | `curl -s http://面板机:8700/health` → `{"ok": true}`（不需要登录，可接外部探活） |
| 事件有没有积压 | `SELECT count(*) FROM paperlite_monitor.events`：面板在跑时应接近 0 |
| 改面板登录密码 | 改 `config.toml` 的 `password`，`systemctl restart paperlite-monitor` |
| 升级面板 | `pip install -U …` 后 `systemctl restart paperlite-monitor` |

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `paperlite-monitor test` 报"写入监控库失败" | 下载机连不上 PG，或账号没有写权限。检查 `dsn`、安全组与 `pg_hba.conf` |
| `test` 显示"面板没有取走它" | 面板没启动，或面板的 `dsn` / `schema` 和下载机不一致 |
| 面板拒绝启动：`面板监听 0.0.0.0（非本机）却没设密码` | 设 `password` |
| 面板日志"消费监控事件出错" | PG 暂时不可用；面板会退避重连，恢复后自动接着处理 |
| 面板里终端一直「离线」 | 下载机没开 `enabled = true`，或写不进 PG（下载机日志每 5 分钟提示一次） |
| `events` 一直在涨 | 面板没在消费：看面板进程和日志 |
| 浏览器反复弹登录框 | 用户名或密码错 |
| 面板数据比预期少 | 面板停机太久、积压超过 `backlog_max` 丢了最旧的；或下载机长时间连不上 PG、本地缓冲溢出 |

## 安全说明

- **面板网页不加密**：浏览器 → 面板（8700）是明文 HTTP，安全组里把 8700 只放行运维 IP 最稳；要对外开放请在前面加 HTTPS 反向代理。
- **上报内容**：只有出口 IP、域名、耗时、计数、WARNING 以上日志文本。**不含**代理账号密码、页面内容、cookie。
- **面板密码**：保存在面板机 `config.toml`（权限 600）里，比对用常量时间。没有登录失败锁定，请用足够长的随机密码（`openssl rand -hex 16`）。
- **数据库**：监控和任务队列共用 PG 时，安全要求同[部署 PostgreSQL](postgres.md)：5432 只对内网开放。
