# 监控面板部署指南

把 PaperLite 的监控大屏部署到一台公网服务器上：各地终端经公网把指标发进 RabbitMQ，面板消费入库、出网页，浏览器访问需登录。指标口径见 README「监控大屏」，本文只讲怎么搭。

> 本文的 `docker-compose.yml`、`rabbitmq.conf`、账号权限命令、TLS 校验行为、Caddyfile、nginx 配置均已实测（RabbitMQ 3.13、Caddy 2.8、nginx 1.27）。示例里的 `example.com`、密码请换成自己的。

## 目录

1. [架构与端口](#1-架构与端口)
2. [准备](#2-准备)
3. [部署 RabbitMQ](#3-部署-rabbitmq)
4. [部署监控面板](#4-部署监控面板)
5. [终端接入](#5-终端接入)
6. [防火墙与安全组](#6-防火墙与安全组)
7. [数据库（面板 SQLite）](#7-数据库面板-sqlite)
8. [配置项一览](#8-配置项一览monitor)
9. [日常运维](#9-日常运维)
10. [故障排查](#10-故障排查)
11. [安全说明](#11-安全说明)

---

## 1. 架构与端口

```
 终端 A（paperlite run）─┐
 终端 B（paperlite run）─┼── amqps://…:5671（TLS，公网）──►  RabbitMQ ──► 面板队列 ──► paperlite-monitor serve ──► monitor.db
 终端 C（paperlite run）─┘                                    （同一台服务器，面板用本机 5672 明文连）        │ 127.0.0.1:8700
                                                                                                          ▼
 运维浏览器 ──────────── https://monitor.example.com（443，用户名+密码）──────────────►  Caddy / nginx（HTTPS 反代）
```

推荐 RabbitMQ 与面板**同一台机器**，下文按此写。

| 端口 | 谁监听 | 对外？ | 用途 |
|---|---|---|---|
| 5671 | RabbitMQ | **公网开放** | AMQPS（TLS），终端上报 |
| 5672 | RabbitMQ | 仅 127.0.0.1 | 明文 AMQP，同机面板消费 |
| 15672 | RabbitMQ | 仅 127.0.0.1 | 管理后台，远程用 SSH 隧道看 |
| 8700 | paperlite-monitor | 仅 127.0.0.1 | 面板 HTTP，由反代转发 |
| 443 / 80 | Caddy（或 nginx） | **公网开放** | 面板 HTTPS；80 用于申请证书和跳转 |

需要两个域名（A 记录都指向这台服务器）：`monitor.example.com`（面板）、`mq.example.com`（RabbitMQ）。

> **为什么一定要 TLS + 域名**：AMQP 明文连接里账号密码是明文，公网必须走 5671 的 TLS。终端用 `amqps://` 连接时会**校验证书和主机名**——用 IP 连会报 `IP address mismatch`，所以要域名 + 正规证书（Let's Encrypt 免费）。

## 2. 准备

- 一台公网 Linux 服务器（1 核 2G 足够；见第 7 节容量），已装 Docker（含 compose 插件）、Python ≥ 3.11
- 两个域名的 A 记录已生效：`dig +short mq.example.com` 能解析到本机
- 云厂商安全组先放行 22、80、443、5671（第 6 节）
- 生成四个密码（**用 hex，免得特殊字符要做 URL 编码**）：

```bash
openssl rand -hex 24   # MQ_ADMIN_PASS：RabbitMQ 管理员
openssl rand -hex 24   # 终端上报账号 pl_terminal
openssl rand -hex 24   # 面板消费账号 pl_panel
openssl rand -hex 16   # 面板网页登录密码
```

> 密码若含 `@ : / % #` 等字符，写进 `amqp_url` 时必须 URL 编码（`@` → `%40`），否则会连错主机或认证失败。用 hex 就没这个问题。

## 3. 部署 RabbitMQ

### 3.1 目录

```bash
sudo mkdir -p /opt/paperlite-mq/certs && cd /opt/paperlite-mq
```

### 3.2 证书（Let's Encrypt）

RabbitMQ 需要证书**文件**，用 certbot 申请。80 端口稍后要给 Caddy，所以 certbot 改用 8888 端口，由 Caddy 把 `mq.example.com` 的验证请求转过来（第 4.4 节的 Caddyfile 已含这段）。**先完成第 4.4 节装好 Caddy，再回来执行：**

```bash
sudo apt install -y certbot
sudo certbot certonly --standalone --http-01-port 8888 -d mq.example.com \
     --agree-tos -m you@example.com --non-interactive
```

（如果这台机器暂时没有别的服务占 80，也可以直接 `--standalone` 不加 `--http-01-port`。）

把证书复制给容器用，并设置续期后自动复制 + 热加载（RabbitMQ 容器内用户是 uid 100 / gid 101）：

```bash
sudo tee /etc/letsencrypt/renewal-hooks/deploy/paperlite-mq.sh >/dev/null <<'EOF'
#!/bin/sh
set -e
SRC=/etc/letsencrypt/live/mq.example.com
DST=/opt/paperlite-mq/certs
install -m 644 "$SRC/fullchain.pem" "$DST/cert.pem"
install -m 644 "$SRC/chain.pem"     "$DST/chain.pem"
install -m 600 -o 100 -g 101 "$SRC/privkey.pem" "$DST/privkey.pem"
# 新证书对新连接生效，不用重启（已建立的连接不受影响）
docker exec paperlite-mq rabbitmqctl -q eval 'ssl:clear_pem_cache().' >/dev/null 2>&1 || true
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/paperlite-mq.sh
sudo /etc/letsencrypt/renewal-hooks/deploy/paperlite-mq.sh      # 首次手动执行一次
```

### 3.3 rabbitmq.conf

`/opt/paperlite-mq/rabbitmq.conf`：

```ini
# 远程终端只走 TLS（5671）；明文 5672 只给同机的面板用（compose 里只映射到 127.0.0.1）
listeners.tcp.default = 5672
listeners.ssl.default = 5671
ssl_options.cacertfile = /etc/rabbitmq/certs/chain.pem
ssl_options.certfile   = /etc/rabbitmq/certs/cert.pem
ssl_options.keyfile    = /etc/rabbitmq/certs/privkey.pem
ssl_options.verify     = verify_none
ssl_options.fail_if_no_peer_cert = false
ssl_options.versions.1 = tlsv1.3
ssl_options.versions.2 = tlsv1.2
# guest 只允许本机登录（默认即如此，显式写上防误改）
loopback_users.guest = true
# 单条消息上限：监控消息都很小，防滥用
max_message_size = 1048576
```

`verify_none` 指**不要求客户端出示证书**（终端靠用户名密码认证）；服务端证书照样由终端校验。

### 3.4 docker-compose.yml

`/opt/paperlite-mq/docker-compose.yml`：

```yaml
services:
  rabbitmq:
    image: rabbitmq:3.13-management-alpine
    container_name: paperlite-mq
    hostname: paperlite-mq            # 固定主机名：RabbitMQ 数据目录按节点名存，换名会"丢"数据
    restart: unless-stopped
    environment:
      RABBITMQ_DEFAULT_USER: admin
      RABBITMQ_DEFAULT_PASS: ${MQ_ADMIN_PASS:?请先 export MQ_ADMIN_PASS}
      RABBITMQ_DEFAULT_VHOST: paperlite
    ports:
      - "5671:5671"                   # AMQPS：对公网开放，终端用
      - "127.0.0.1:5672:5672"         # 明文 AMQP：只给本机面板用
      - "127.0.0.1:15672:15672"       # 管理后台：只在本机，远程用 SSH 隧道看
    volumes:
      - ./rabbitmq.conf:/etc/rabbitmq/conf.d/90-paperlite.conf:ro
      - ./certs:/etc/rabbitmq/certs:ro
      - mq-data:/var/lib/rabbitmq
volumes:
  mq-data:
```

设了 `RABBITMQ_DEFAULT_USER` 后**不会创建 guest 账号**，也不会有默认的 `/` vhost，只有 `paperlite`。

```bash
cd /opt/paperlite-mq
echo "MQ_ADMIN_PASS=<管理员密码>" > .env && chmod 600 .env
docker compose up -d
docker exec paperlite-mq rabbitmq-diagnostics -q listeners    # 应看到 5671 amqp/ssl 和 5672 amqp
```

### 3.5 建账号（最小权限）

```bash
mq() { docker exec paperlite-mq rabbitmqctl -q "$@"; }

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
sudo -u paperlite /opt/paperlite-monitor/.venv/bin/pip install 'paperlite[monitor]'
# 监控尚未发布到 PyPI 时，从源码装：
#   git clone https://github.com/huangpd/PaperLite.git /tmp/PaperLite
#   sudo -u paperlite /opt/paperlite-monitor/.venv/bin/pip install '/tmp/PaperLite[monitor]'
```

面板只依赖 `paperlite` 包里的 `paperlite-monitor` 命令和 pika，不需要浏览器、代理配置。

### 4.2 配置

`/opt/paperlite-monitor/config.toml`（只需要 `[monitor]` 一段）：

```toml
[monitor]
amqp_url = "amqp://pl_panel:<面板消费密码>@127.0.0.1:5672/paperlite"   # 同机：走本机明文 5672
db = "/opt/paperlite-monitor/monitor.db"                              # 绝对路径，必须本地盘
bind = "127.0.0.1"                                                     # 只给反代访问
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

**登录方式**：配了 `password` 后，浏览器打开面板会弹出系统登录框（HTTP Basic）。没配 `password`/`token` 时面板只允许 `bind = 127.0.0.1`——改成 `0.0.0.0` 会直接拒绝启动，防止裸奔上公网。

> HTTP Basic 的密码在 HTTP 里只是 base64，**必须由 HTTPS 反代对外**（4.4 节），不要把 8700 直接开到公网。

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
journalctl -u paperlite-monitor -f      # 应看到「已连接 RabbitMQ，消费队列 paperlite.monitor.dashboard」
curl -s http://127.0.0.1:8700/health    # {"ok": true}
```

RabbitMQ 晚于面板启动也没关系：面板会退避重连。

### 4.4 HTTPS 反代（Caddy，推荐）

Caddy 自动申请和续期证书，配置最短：

```bash
sudo apt install -y caddy      # Ubuntu 24.04 / Debian 12 自带；更老的系统按 caddyserver.com 文档加官方源
```

`/etc/caddy/Caddyfile`：

```caddyfile
# 面板：Caddy 自动申请/续期 HTTPS 证书
monitor.example.com {
	encode gzip
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options nosniff
		X-Frame-Options DENY
		Referrer-Policy no-referrer
		-Server
	}
	reverse_proxy 127.0.0.1:8700
}

# RabbitMQ 的证书由 certbot 申请（RabbitMQ 要证书文件）：80 端口在 Caddy 手里，
# 只把该域名的 ACME 验证路径转给 certbot（--http-01-port 8888），其余一律 404
http://mq.example.com {
	handle /.well-known/acme-challenge/* {
		reverse_proxy 127.0.0.1:8888
	}
	respond 404
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

然后回到 3.2 申请 `mq.example.com` 的证书。打开 `https://monitor.example.com`，输入用户名密码即可。

<details>
<summary>用 nginx 代替 Caddy</summary>

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo certbot certonly --nginx -d monitor.example.com
```

`/etc/nginx/conf.d/paperlite-monitor.conf`：

```nginx
server {
    listen 80;
    server_name monitor.example.com;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl;
    http2 on;
    server_name monitor.example.com;
    ssl_certificate     /etc/letsencrypt/live/monitor.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/monitor.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
    location / {
        proxy_pass http://127.0.0.1:8700;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

RabbitMQ 的 `mq.example.com` 证书走 webroot：在同一文件里再加一段（只开放 ACME 验证路径）：

```nginx
server {
    listen 80;
    server_name mq.example.com;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 404; }
}
```

```bash
sudo mkdir -p /var/www/certbot && sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/certbot -d mq.example.com --agree-tos -m you@example.com --non-interactive
```

3.2 节的续期钩子照用（跳过 3.2 里的 `--standalone` 命令）。
</details>

## 5. 终端接入

每台跑 PaperLite 的机器：

```bash
pip install 'paperlite[monitor]'        # 多装 pika；源码环境：uv sync --extra monitor
```

在该批次的 `config.toml` 里加一段（其余不动）：

```toml
[monitor]
enabled  = true
amqp_url = "amqps://pl_terminal:<终端密码>@mq.example.com:5671/paperlite"   # 注意 amqps 和 5671
vendor   = "qingguo"      # 代理厂商标签，面板按「厂商 × 域名」统计
# node   = "gpu-01"       # 终端名，默认主机名
# heartbeat_s = 15
```

先验证连通性，再照常跑：

```bash
paperlite-monitor --config config.toml test
# OK：测试心跳已送达 exchange=paperlite.monitor …
paperlite run config.toml
```

面板「浏览器与终端」表里出现该终端即接入成功。批次名取配置文件名（`sciencedirect.toml` → `sciencedirect`）。

- 终端网络断了 / RabbitMQ 挂了：下载照常，事件在本地缓冲（默认 1 万条，满了丢最旧的），恢复后自动补发。
- 公司内网有 TLS 中间人代理、或用自签证书时：设环境变量 `SSL_CERT_FILE=/path/to/ca.pem` 让 Python 信任对应 CA。

## 6. 防火墙与安全组

只对公网开 4 个端口：

| 端口 | 来源 |
|---|---|
| 22 | 你的运维 IP（尽量别对全网） |
| 80、443 | 全网（证书验证 + 面板） |
| 5671 | 全网，或只放行终端所在的 IP 段（更安全） |

```bash
sudo ufw allow 22/tcp && sudo ufw allow 80,443/tcp && sudo ufw allow 5671/tcp && sudo ufw enable
```

> ⚠️ **Docker 发布的端口不受 ufw 控制**（Docker 直接改 iptables）。所以 5672、15672 必须像上面 compose 里那样写成 `127.0.0.1:5672:5672`，不能只靠 ufw 挡。云厂商安全组是另一道闸，两边都要配。

看 RabbitMQ 管理后台（只监听本机）：

```bash
ssh -L 15672:127.0.0.1:15672 user@server     # 然后本地打开 http://127.0.0.1:15672，用 admin 登录
```

## 7. 数据库（面板 SQLite）

面板把消息写进 `[monitor].db` 指定的 SQLite 文件（默认 `monitor.db`，相对路径以启动目录为准，**建议写绝对路径**）。必须放本地盘（WAL 模式要文件锁，NFS/FUSE 挂载不行）。

### 7.1 两层存储

| 层 | 表 | 保留 | 用途 |
|---|---|---|---|
| 原始事件 | `page` `browser` `proxy_check` `log` | `raw_retention_hours`（默认 48h） | 实时事件流、近 5 分钟域名频率、排查明细 |
| 小时汇总 | `page_h` `browser_h` `proxy_h` `proxy_reason_h` `fleet_h` | `retention_days`（默认 30 天） | 大屏 24 小时 / 7 天 / 30 天的全部数字和趋势 |
| 终端 | `instance` | 30 天未上报即删 | 每个终端最新心跳（在线数、待下载） |

入库时先按小时聚合，所以大屏查询代价与事件量无关。P95 用每小时的耗时直方图合并得出，误差约 ±6%。过期数据每小时清理一次。

### 7.2 容量估算

一个 20 浏览器的终端每天约 5 万次页面尝试 + 几千次 IP 质检 + 5760 次心跳（15s 一次）。

实测（30 天、3 个终端的量级，300 万条消息，30 个域名、4 种 WAF、2 个厂商——偏悲观的组合）：

| 指标 | 实测 |
|---|---|
| 入库速度 | 约 2.2 万条/秒（远超需要：10 个终端合计约 10 条/秒） |
| 小时汇总行数 | 约 17 万行 |
| 库文件大小 | 约 250 MB（其中原始事件 48 小时） |
| 大屏查询（24h / 7 天 / 30 天） | 0.07s / 0.5s / 1.4s（多人同时看有 5 秒缓存） |

粗算：小时汇总每天约 `24 × 域名数 × 厂商数` 行；原始事件约 200 字节/条 × 48 小时的量。终端很多导致库太大时，先调小 `raw_retention_hours`（如 12）。

### 7.3 备份

WAL 模式下可在线热备，不用停服务：

```bash
sqlite3 /opt/paperlite-monitor/monitor.db ".backup '/backup/monitor-$(date +%F).db'"
# 没装 sqlite3 命令行时：
/opt/paperlite-monitor/.venv/bin/python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d)" \
    /opt/paperlite-monitor/monitor.db /backup/monitor-$(date +%F).db
```

监控数据丢了不影响下载，备份不是必须的；想保留历史趋势再做。

### 7.4 清空 / 迁移

- **清空重来**：`systemctl stop paperlite-monitor`，删 `monitor.db*`（三个文件），再 start。RabbitMQ 队列里还没消费的消息会补进新库。
- **换服务器**：停面板 → 拷走 `monitor.db`（或用 7.3 的备份）→ 新机器按第 4 节部署 → 启动。终端无需改动（只要 `mq.example.com` 指向不变）。

### 7.5 直接查库

```bash
sqlite3 -readonly /opt/paperlite-monitor/monitor.db \
  "SELECT host, SUM(n) 尝试, ROUND(1.0*SUM(ok)/SUM(n),3) 成功率 FROM page_h
   WHERE hour >= CAST(strftime('%s','now') AS INT)/3600 - 24 GROUP BY host ORDER BY 2 DESC;"
```

也可以带 token 调接口拿 JSON：`curl -s 'https://monitor.example.com/api/summary?window=7d&token=<token>'`（`window` = `1d` / `7d` / `30d`）。

## 8. 配置项一览（`[monitor]`）

| 配置项 | 默认 | 用在 | 说明 |
|---|---|---|---|
| `enabled` | `false` | 终端 | `true` 才上报 |
| `amqp_url` | `amqp://guest:guest@127.0.0.1:5672/%2F` | 两端 | 终端用 `amqps://…:5671/paperlite`，同机面板用 `amqp://…@127.0.0.1:5672/paperlite`。最后一段是 vhost |
| `exchange` | `paperlite.monitor` | 两端 | 改了要同步改 RabbitMQ 权限正则 |
| `node` | 主机名 | 终端 | 终端名；面板按 `node:pid` 区分进程 |
| `vendor` | `unknown` | 终端 | 代理厂商标签 |
| `heartbeat_s` | `15` | 终端 | 心跳间隔；面板 `max(60s, 4×heartbeat_s)` 没收到判离线 |
| `buffer_size` | `10000` | 终端 | 本地发送缓冲条数 |
| `forward_logs` | `true` | 终端 | WARNING 以上日志也作为实时事件上报 |
| `queue` | `paperlite.monitor.dashboard` | 面板 | 面板的持久队列 |
| `queue_max_length` | `2000000` | 面板 | 队列上限，满了丢最旧的 |
| `db` | `monitor.db` | 面板 | SQLite 路径（本地盘） |
| `bind` / `port` | `127.0.0.1` / `8700` | 面板 | 监听地址 |
| `username` / `password` | `admin` / 空 | 面板 | 网页登录（HTTP Basic）；`password` 非空即开启 |
| `token` | 空 | 面板 | 可选的接口令牌（`?token=` 或请求头 `X-Token`） |
| `retention_days` | `30` | 面板 | 小时汇总保留天数 |
| `raw_retention_hours` | `48` | 面板 | 原始事件保留小时数 |

## 9. 日常运维

| 要做的事 | 怎么做 |
|---|---|
| 面板是否活着 | `curl -s https://monitor.example.com/health` → `{"ok": true}`（不需要登录，可接外部探活） |
| 消息有没有积压 | `docker exec paperlite-mq rabbitmqctl -q list_queues -p paperlite name messages consumers`：`messages` 应接近 0、`consumers` 为 1 |
| 证书续期 | certbot 自带定时任务；`sudo certbot renew --dry-run` 演练。Caddy 自动续期 |
| 改面板登录密码 | 改 `config.toml` 的 `password`，`systemctl restart paperlite-monitor` |
| 轮换终端密码 | `rabbitmqctl change_password pl_terminal '<新密码>'`，再逐台改终端 `amqp_url`。旧连接不会被立刻踢掉，要立刻生效可 `docker restart paperlite-mq`（终端会自动重连，期间事件在本地缓冲） |
| 升级面板 | `pip install -U 'paperlite[monitor]'` 后 `systemctl restart paperlite-monitor` |
| 升级 RabbitMQ | 改 compose 里的镜像版本（只在同一大版本内升，如 3.13.x），`docker compose pull && docker compose up -d`；数据在 `mq-data` 卷里 |

## 10. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `paperlite-monitor test` 报 `已连上 RabbitMQ，但还没有面板队列绑定` | 面板还没启动过（队列是面板建的）。先起面板 |
| 报 `ACCESS_REFUSED - Login was refused` | 用户名/密码错，或密码里有特殊字符没 URL 编码 |
| 报 `NOT_ALLOWED - vhost / not found` | `amqp_url` 末尾漏了 `/paperlite`（默认 vhost 是 `/`，本部署没有它） |
| 报 `ACCESS_REFUSED - … access to exchange/queue` | 权限正则与 `exchange`/`queue` 名不一致（3.5 节） |
| 报 `CERTIFICATE_VERIFY_FAILED … IP address mismatch` | 用了 IP 连 `amqps://`，改用域名 |
| 报 `CERTIFICATE_VERIFY_FAILED … unable to get local issuer` | 证书链不全（RabbitMQ 要用 `fullchain.pem` 作 `cert.pem`，3.2 钩子已处理），或内网有 TLS 拦截（设 `SSL_CERT_FILE`） |
| 连接超时 | 安全组/ufw 没放 5671；`nc -vz mq.example.com 5671` 自查 |
| 面板里终端一直「离线」 | 终端没开 `enabled = true`、没装 pika（终端日志会提示）、或连不上 MQ（终端日志每 5 分钟提示一次） |
| 浏览器反复弹登录框 | 密码错；或反代把 `Authorization` 头丢了（Caddy/nginx 默认会转发，自定义配置要注意） |
| `面板监听 0.0.0.0（非本机）却没设密码` 拒绝启动 | 设 `password`，或改回 `bind = "127.0.0.1"` 由反代对外 |
| 面板数据比预期少 | `queue_max_length` 太小导致积压时丢了最旧的；或终端本地缓冲在长时间断网时溢出 |

## 11. 安全说明

- **上报内容**：只有出口 IP、域名、耗时、计数、WARNING 以上日志文本。**不含**代理账号密码、页面内容、cookie。
- **终端密码泄露的影响**：`pl_terminal` 只能往 `paperlite.monitor` 写消息，读不到任何数据；最坏是被灌假数据污染大屏。处理：轮换密码（第 9 节）。
- **面板密码**：保存在面板机 `config.toml`（权限 600）里，比对用常量时间；HTTP Basic 必须走 HTTPS。没有登录失败锁定，请用足够长的随机密码（`openssl rand -hex 16`）；需要更强防护可在反代上加 IP 白名单。
- **管理后台**：15672 只绑本机，用 SSH 隧道访问；`admin` 是唯一的管理员账号。
- **RabbitMQ 明文端口**：5672 只绑 127.0.0.1，只有同机面板用。
