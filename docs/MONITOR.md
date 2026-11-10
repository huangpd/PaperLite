# 监控面板部署指南（公网 IP + 端口，无需域名）

把 PaperLite 的监控大屏部署到一台有公网 IP 的服务器上：各地终端经公网把指标发进 RabbitMQ，面板消费入库，浏览器用 `http://服务器IP:8700` 访问并输入用户名密码。指标口径见 README「监控大屏」，本文只讲怎么搭。

> 本文用到的 `deploy/monitor/` 下的文件（`docker-compose.yml`、`rabbitmq.conf`）、账号权限命令、面板登录均已实测（RabbitMQ 3.13）。示例 IP `203.0.113.10` 和各密码请换成自己的。

## 目录

1. [架构与端口](#1-架构与端口)
2. [准备](#2-准备)
3. [部署 RabbitMQ](#3-部署-rabbitmq)
4. [部署监控面板](#4-部署监控面板)
5. [浏览器访问](#5-浏览器访问)
6. [终端接入](#6-终端接入)
7. [防火墙与安全组](#7-防火墙与安全组)
8. [数据库（面板 SQLite）](#8-数据库面板-sqlite)
9. [配置项一览](#9-配置项一览monitor)
10. [日常运维](#10-日常运维)
11. [故障排查](#11-故障排查)
12. [安全说明](#12-安全说明)

---

## 1. 架构与端口

```
 终端 A（paperlite run）─┐
 终端 B（paperlite run）─┼── amqp://203.0.113.10:5672 ──► RabbitMQ ──► 面板队列 ──► paperlite-monitor serve ──► monitor.db
 终端 C（paperlite run）─┘                                  （同一台服务器；面板连本机 127.0.0.1:5672）  │
                                                                                                      │ HTTP + 登录
 运维浏览器 ──────────────────── http://203.0.113.10:8700 ◄────────────────────────────────────────────┘
```

RabbitMQ 与面板放**同一台机器**，下文按此写。

| 端口 | 谁监听 | 对外？ | 用途 |
|---|---|---|---|
| 5672 | RabbitMQ | **公网开放** | AMQP，终端上报、同机面板消费 |
| 8700 | paperlite-monitor | **公网开放** | 面板网页（HTTP + 登录） |
| 15672 | RabbitMQ | 仅 127.0.0.1 | RabbitMQ 管理后台，远程用 SSH 隧道看 |

## 2. 准备

- 一台有**固定公网 IP** 的 Linux 服务器（1 核 2G 足够，见第 8 节容量），已装 Docker（含 compose 插件）、Python ≥ 3.11、`openssl`（生成密码用）、`git`
- 云厂商安全组放行 22、5672、8700（第 7 节）
- 生成四个密码（**用 hex，免得特殊字符要做 URL 编码**）：

```bash
openssl rand -hex 24   # RabbitMQ 管理员 admin
openssl rand -hex 24   # 终端上报账号 pl_terminal
openssl rand -hex 24   # 面板消费账号 pl_panel
openssl rand -hex 16   # 面板网页登录密码
```

> 密码若含 `@ : / % #` 等字符，写进 `amqp_url` 时必须 URL 编码（`@` → `%40`），否则会连错主机或认证失败。用 hex 就没这个问题。

## 3. 部署 RabbitMQ

```bash
git clone https://github.com/huangpd/PaperLite.git /tmp/PaperLite
sudo mkdir -p /opt/paperlite-mq && sudo cp /tmp/PaperLite/deploy/monitor/* /opt/paperlite-mq/
```

### 3.1 配置与启动

`deploy/monitor/` 里已经带好两个文件，一般不用改：

- `rabbitmq.conf`：监听 5672；guest 只许本机登录；单条消息上限 1MB
- `docker-compose.yml`：5672 对外；15672 **只绑 127.0.0.1**；默认 vhost `paperlite`；数据存在 `mq-data` 卷

```bash
cd /opt/paperlite-mq
echo "MQ_ADMIN_PASS=<管理员密码>" | sudo tee .env >/dev/null && sudo chmod 600 .env
sudo docker compose up -d
sudo docker exec paperlite-mq rabbitmq-diagnostics -q listeners   # 应看到 5672 amqp
```

设了 `RABBITMQ_DEFAULT_USER` 后**不会创建 guest 账号**，也没有默认的 `/` vhost，只有 `paperlite`。

### 3.2 建账号（最小权限）

```bash
mq() { sudo docker exec paperlite-mq rabbitmqctl -q "$@"; }

# 终端：只能声明并写入 paperlite.monitor 这个 exchange，什么都读不了
mq add_user pl_terminal '<终端密码>'
mq set_permissions -p paperlite pl_terminal '^paperlite\.monitor$' '^paperlite\.monitor$' '^$'

# 面板：能声明 exchange + 自己的队列、绑定、消费；不能往 exchange 发消息
mq add_user pl_panel '<面板消费密码>'
mq set_permissions -p paperlite pl_panel \
   '^paperlite\.monitor(\.dashboard)?$' '^paperlite\.monitor\.dashboard$' '^paperlite\.monitor(\.dashboard)?$'

mq list_permissions -p paperlite
```

三列依次是 configure / write / read 的正则。实测效果：

| 操作 | pl_terminal | pl_panel |
|---|---|---|
| 往 `paperlite.monitor` 发消息 | ✅ | ❌ ACCESS_REFUSED |
| 读面板队列 | ❌ ACCESS_REFUSED | ✅ |
| 建其它队列 / 写其它 exchange | ❌ | ❌ |
| 进其它 vhost | ❌ | ❌ |

> 如果你在 `[monitor]` 里改了 `exchange` / `queue` 名，上面的正则要跟着改。

## 4. 部署监控面板

### 4.1 安装

```bash
sudo useradd -r -m -d /opt/paperlite-monitor -s /usr/sbin/nologin paperlite
sudo -u paperlite python3 -m venv /opt/paperlite-monitor/.venv
sudo -u paperlite /opt/paperlite-monitor/.venv/bin/pip install paperlite      # 或源码：pip install /tmp/PaperLite
```

面板只用到 `paperlite-monitor` 命令和 pika，不需要浏览器、代理配置。

### 4.2 配置

`/opt/paperlite-monitor/config.toml`（只需要 `[monitor]` 一段）：

```toml
[monitor]
amqp_url = "amqp://pl_panel:<面板消费密码>@127.0.0.1:5672/paperlite"   # 同机：连本机 5672
db = "/opt/paperlite-monitor/monitor.db"                              # 绝对路径，必须本地盘
bind = "0.0.0.0"                                                       # 对外监听
port = 8700
username = "admin"                                                     # 网页登录用户名
password = "<面板网页登录密码>"                                        # 网页登录密码
# token = ""                     # 可选：给脚本/外部监控调 /api/summary 用
# retention_days = 30            # 大屏数据保留天数
# raw_retention_hours = 48       # 原始事件保留小时数
```

```bash
sudo chown paperlite: /opt/paperlite-monitor/config.toml && sudo chmod 600 /opt/paperlite-monitor/config.toml
```

保险：`bind` 不是本机地址却没设 `password`/`token` → **拒绝启动**。

### 4.3 systemd 常驻

`/etc/systemd/system/paperlite-monitor.service`：

```ini
[Unit]
Description=PaperLite monitor dashboard
After=network-online.target docker.service
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
# 应看到：监控大屏 http://127.0.0.1:8700/  登录用户 admin …  以及  已连接 RabbitMQ，消费队列 paperlite.monitor.dashboard
curl -s http://127.0.0.1:8700/health    # {"ok": true}
```

RabbitMQ 晚于面板启动也没关系：面板会退避重连。

## 5. 浏览器访问

打开 `http://203.0.113.10:8700`，弹出登录框，输入 `username` / `password`。

## 6. 终端接入

每台跑 PaperLite 的机器：

```bash
pip install paperlite                   # 已含 pika（≥0.2.2；更早的版本要写 'paperlite[monitor]'）
```

1. 在该批次的 `config.toml` 里加一段（其余不动）：

```toml
[monitor]
enabled      = true
amqp_url     = "amqp://pl_terminal:<终端密码>@203.0.113.10:5672/paperlite"
vendor       = "qingguo"    # 代理厂商标签，面板按「厂商 × 域名」统计
# node       = "gpu-01"     # 终端名，默认主机名
# heartbeat_s = 15
```

2. 先验证连通性，再照常跑：

```bash
paperlite-monitor --config config.toml test
# OK：测试心跳已送达 exchange=paperlite.monitor …
paperlite run config.toml
```

面板「浏览器与终端」表里出现该终端即接入成功。批次名取配置文件名（`sciencedirect.toml` → `sciencedirect`）。

- 终端网络断了 / RabbitMQ 挂了：下载照常，事件在本地缓冲（默认 1 万条，满了丢最旧的），恢复后自动补发。

## 7. 防火墙与安全组

只对公网开 3 个端口：

| 端口 | 来源 |
|---|---|
| 22 | 你的运维 IP（尽量别对全网） |
| 5672 | 全网，或只放行终端所在的 IP 段（更安全） |
| 8700 | 全网，或只放行运维人员的 IP（更安全） |

```bash
sudo ufw allow 22/tcp && sudo ufw allow 5672/tcp && sudo ufw allow 8700/tcp && sudo ufw enable
```

> ⚠️ **Docker 发布的端口不受 ufw 控制**（Docker 直接改 iptables）。所以 15672 必须像 compose 里那样写成 `127.0.0.1:15672:15672`，不能只靠 ufw 挡；5672 要限来源只能靠云安全组。云厂商安全组是另一道闸，两边都要配。

看 RabbitMQ 管理后台（只监听本机）：

```bash
ssh -L 15672:127.0.0.1:15672 user@203.0.113.10    # 然后本地打开 http://127.0.0.1:15672，用 admin 登录
```

## 8. 数据库（面板 SQLite）

面板把消息写进 `[monitor].db` 指定的 SQLite 文件（默认 `monitor.db`，相对路径以启动目录为准，**建议写绝对路径**）。必须放本地盘（WAL 模式要文件锁，NFS/FUSE 挂载不行）。

### 8.1 两层存储

| 层 | 表 | 保留 | 用途 |
|---|---|---|---|
| 原始事件 | `page` `browser` `proxy_check` `log` | `raw_retention_hours`（默认 48h） | 实时事件流、近 5 分钟域名频率、排查明细 |
| 小时汇总 | `page_h` `browser_h` `proxy_h` `proxy_reason_h` `fleet_h` | `retention_days`（默认 30 天） | 大屏 24 小时 / 7 天 / 30 天的全部数字和趋势 |
| 终端 | `instance` | 30 天未上报即删 | 每个终端最新心跳（在线数、待下载） |

入库时先按小时聚合，所以大屏查询代价与事件量无关。P95 用每小时的耗时直方图合并得出，误差约 ±6%。过期数据每小时清理一次。

### 8.2 容量估算

一个 20 浏览器的终端每天约 5 万次页面尝试 + 几千次 IP 质检 + 5760 次心跳（15s 一次）。

实测（30 天、3 个终端的量级，300 万条消息，30 个域名、4 种 WAF、2 个厂商——偏悲观的组合）：

| 指标 | 实测 |
|---|---|
| 入库速度 | 约 2.2 万条/秒（远超需要：10 个终端合计约 10 条/秒） |
| 小时汇总行数 | 约 17 万行 |
| 库文件大小 | 约 250 MB（其中原始事件 48 小时） |
| 大屏查询（24h / 7 天 / 30 天） | 0.07s / 0.5s / 1.4s（多人同时看有 5 秒缓存） |

粗算：小时汇总每天约 `24 × 域名数 × 厂商数` 行；原始事件约 200 字节/条 × 48 小时的量。终端很多导致库太大时，先调小 `raw_retention_hours`（如 12）。

### 8.3 备份

WAL 模式下可在线热备，不用停服务：

```bash
sqlite3 /opt/paperlite-monitor/monitor.db ".backup '/backup/monitor-$(date +%F).db'"
# 没装 sqlite3 命令行时：
/opt/paperlite-monitor/.venv/bin/python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d)" \
    /opt/paperlite-monitor/monitor.db /backup/monitor-$(date +%F).db
```

监控数据丢了不影响下载，备份不是必须的；想保留历史趋势再做。

### 8.4 清空 / 迁移

- **清空重来**：`systemctl stop paperlite-monitor`，删 `monitor.db*`（三个文件），再 start。RabbitMQ 队列里还没消费的消息会补进新库。
- **换服务器**：停面板 → 拷走 `monitor.db` → 新机器按本文部署 → 各终端把 `amqp_url` 里的 IP 改成新的。

### 8.5 直接查库

```bash
sqlite3 -readonly /opt/paperlite-monitor/monitor.db \
  "SELECT host, SUM(n) 尝试, ROUND(1.0*SUM(ok)/SUM(n),3) 成功率 FROM page_h
   WHERE hour >= CAST(strftime('%s','now') AS INT)/3600 - 24 GROUP BY host ORDER BY 2 DESC;"
```

也可以带 token 调接口拿 JSON：`curl -s 'http://203.0.113.10:8700/api/summary?window=7d&token=<token>'`（`window` = `1d` / `7d` / `30d`）。

## 9. 配置项一览（`[monitor]`）

| 配置项 | 默认 | 用在 | 说明 |
|---|---|---|---|
| `enabled` | `false` | 终端 | `true` 才上报 |
| `amqp_url` | `amqp://guest:guest@127.0.0.1:5672/%2F` | 两端 | 终端用 `amqp://…@公网IP:5672/paperlite`，同机面板用 `amqp://…@127.0.0.1:5672/paperlite`。最后一段是 vhost |
| `exchange` | `paperlite.monitor` | 两端 | 改了要同步改 RabbitMQ 权限正则 |
| `node` | 主机名 | 终端 | 终端名；面板按 `node:pid` 区分进程 |
| `vendor` | `unknown` | 终端 | 代理厂商标签 |
| `heartbeat_s` | `15` | 终端 | 心跳间隔；面板 `max(60s, 4×heartbeat_s)` 没收到判离线 |
| `buffer_size` | `10000` | 终端 | 本地发送缓冲条数 |
| `forward_logs` | `true` | 终端 | WARNING 以上日志也作为实时事件上报 |
| `queue` | `paperlite.monitor.dashboard` | 面板 | 面板的持久队列 |
| `queue_max_length` | `2000000` | 面板 | 队列上限，满了丢最旧的 |
| `db` | `monitor.db` | 面板 | SQLite 路径（本地盘） |
| `bind` / `port` | `127.0.0.1` / `8700` | 面板 | 监听地址；对外用 `0.0.0.0` |
| `username` / `password` | `admin` / 空 | 面板 | 网页登录（HTTP Basic）；`password` 非空即开启 |
| `token` | 空 | 面板 | 可选的接口令牌（`?token=` 或请求头 `X-Token`） |
| `retention_days` | `30` | 面板 | 小时汇总保留天数 |
| `raw_retention_hours` | `48` | 面板 | 原始事件保留小时数 |

## 10. 日常运维

| 要做的事 | 怎么做 |
|---|---|
| 面板是否活着 | `curl -s http://203.0.113.10:8700/health` → `{"ok": true}`（不需要登录，可接外部探活） |
| 消息有没有积压 | `sudo docker exec paperlite-mq rabbitmqctl -q list_queues -p paperlite name messages consumers`：`messages` 应接近 0、`consumers` 为 1 |
| 服务器公网 IP 变了 | 各终端把 `amqp_url` 里的 IP 改掉；浏览器改用新地址 |
| 改面板登录密码 | 改 `config.toml` 的 `password`，`systemctl restart paperlite-monitor` |
| 轮换终端密码 | `rabbitmqctl change_password pl_terminal '<新密码>'`，再逐台改终端 `amqp_url`。旧连接不会被立刻踢掉，要立刻生效可 `docker restart paperlite-mq`（终端会自动重连，期间事件在本地缓冲） |
| 升级面板 | `pip install -U …` 后 `systemctl restart paperlite-monitor` |
| 升级 RabbitMQ | 改 compose 里的镜像版本（只在同一大版本内升，如 3.13.x），`docker compose pull && docker compose up -d`；数据在 `mq-data` 卷里 |

## 11. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `paperlite-monitor test` 报 `已连上 RabbitMQ，但还没有面板队列绑定` | 面板还没启动过（队列是面板建的）。先起面板 |
| 报 `ACCESS_REFUSED - Login was refused` | 用户名/密码错，或密码里有特殊字符没 URL 编码 |
| 报 `NOT_ALLOWED - vhost / not found` | `amqp_url` 末尾漏了 `/paperlite`（默认 vhost 是 `/`，本部署没有它） |
| 报 `ACCESS_REFUSED - … access to exchange/queue` | 权限正则与 `exchange`/`queue` 名不一致（3.2 节） |
| 连接超时 / 被拒 | 安全组/ufw 没放 5672 或 8700；在终端上 `nc -vz 203.0.113.10 5672` 自查 |
| 浏览器反复弹登录框 | 用户名或密码错 |
| 面板拒绝启动：`面板监听 0.0.0.0（非本机）却没设密码` | 设 `password` |
| 面板里终端一直「离线」 | 终端没开 `enabled = true`、没装 pika（终端日志会提示）、或连不上 MQ（终端日志每 5 分钟提示一次） |
| 自测 `curl` 访问结果奇怪（502、连不上） | 本机设了 `http_proxy`/`https_proxy`（代理软件），curl 走了代理。加 `--noproxy '*'` 再试 |
| 面板数据比预期少 | `queue_max_length` 太小导致积压时丢了最旧的；或终端本地缓冲在长时间断网时溢出 |

## 12. 安全说明

- **传输不加密**：终端 → RabbitMQ（5672）、浏览器 → 面板（8700）都是明文，账号密码和上报内容在网络上可被截获。用安全组把 5672 只放行终端 IP、8700 只放行运维 IP，能挡掉大部分风险。15672 只绑本机。
- **上报内容**：只有出口 IP、域名、耗时、计数、WARNING 以上日志文本。**不含**代理账号密码、页面内容、cookie。
- **终端密码泄露的影响**：`pl_terminal` 只能往 `paperlite.monitor` 写消息，读不到任何数据；最坏是被灌假数据污染大屏。处理：轮换密码（第 10 节）。
- **面板密码**：保存在面板机 `config.toml`（权限 600）里，比对用常量时间。没有登录失败锁定，请用足够长的随机密码（`openssl rand -hex 16`）；安全组里把 8700 只放行运维 IP 最稳。
- **管理后台**：15672 只绑本机，用 SSH 隧道访问；`admin` 是唯一的管理员账号。
