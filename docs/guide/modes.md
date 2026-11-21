# 运行模式

运行一个批次要做两个选择，两者相互独立、可以任意组合：

- **下载方式**：浏览器模式（默认）还是 curl 模式，由 `[run] mode` 决定。
- **任务队列后端**：SQLite（单机，默认）还是 PostgreSQL（多机），由 `[queue] backend` 决定，见[下文](#任务队列后端)。

## 下载方式

默认是浏览器模式，`run --curl` 或 `[run] mode = "curl"` 切换到 curl 模式。

### 对比

| | 浏览器模式（默认） | curl 模式 |
|---|---|---|
| 工作方式 | N 个 worker，各自独占浏览器与 IP，过盾一次后连续下载 | 浏览器过盾后把 IP + cookie + UA 存入身份池，`curl_cffi` 线程领用下载 |
| 每批次主机数 | 任意混合 | 仅限一个 |
| 跳转 / 网页 | 自动跟随，网页保存为 `.html` | 跟随 HTTP 跳转与中转页的 meta refresh；`url_type=html` 整页保存；跳到别的主机被挑战时改用浏览器打开 |
| 外部依赖 | 无 | Redis（身份池） |
| 巡检（重复标题跳过） | 有 | 无 |
| 卷期停止 | 无 | 有（仅 SQLite 后端） |
| 资源占用 | 每个 worker 一个浏览器进程 | 浏览器只用来过盾，下载线程很轻 |
| 适用场景 | 来源混杂的 URL 列表 | 单站点海量任务、追求单 IP 吞吐 |

**怎么选**：拿不准就用浏览器模式。只有一个站点、任务量几十万以上、而且该站点用 curl 能稳定下载时，再考虑 curl 模式。

### 浏览器模式

```bash
paperlite run config.toml              # [run] mode = "browser"
paperlite run --workers 20             # 命令行临时跑
paperlite run --no-proxy --headed      # 本地调试：直连本机 IP、显示浏览器窗口
```

- 每个新浏览器领任务前，先在第一个导入的主机上热身过盾，默认地址为 `https://{host}/`，可用 `[landing]` 覆盖。
- 其他主机的页面在首次渲染时各自过盾。
- 同一浏览器两个页面之间随机等待 `page_interval_s`，避开同域名频率限制。
- IP 在租期内断开（隧道失败、连接重置等）时，任务放回队列，同一浏览器连续失败两次即提前换 IP。

### curl 模式

curl 模式拆成两个角色，通过 Redis 交换身份：

- **生产者**：开浏览器过盾，验收后把身份（IP + cookie + UA）放进 Redis 池。
- **下载线程**：从任务库领任务、从 Redis 领身份，用 `curl_cffi` 下载。

```toml
[limits]
redis_url = "redis://127.0.0.1:6379/0"

[run]
mode    = "curl"
workers = 20                   # 下载线程数 = 同时在用的身份数
```

```bash
paperlite run config.toml       # 一个进程里同时跑生产者和下载线程
# 或拆开跑：
paperlite produce               # 只生产身份（常驻）
paperlite download --workers 20 # 只下载
```

**按消费生产**：生产目标 = min(`max_identities`, 在线下载线程数, 可领取任务数)。下载线程全部退出或任务领完，生产者就停止过盾，不再消耗 IP。代价是冷启动：第一个身份过盾约需 20 秒。

**身份验收**：浏览器过盾后，生产者用同一 IP、UA 和 cookie 通过 `curl_cffi` 请求 `[landing]` 地址，失败的身份不入池。建议把 `[landing]` 配成有代表性的真实文章页，只验首页 200 不能保证文章页可用。

**新站点注意**：non-PDF 判定规则（`classify.py`）是按 ACM 写的，换站点前先确认是否适用。

**升级**：Redis 里的 cookie 记录带 `cookies_format=cookiejar-v1` 标记，旧格式可直接读取；升级时生产者和下载线程要一起重启。

设计细节（身份模型、验收、熔断、卷期停止）见[架构与设计说明](../development/architecture.md)。

## 任务队列后端

| | SQLite（默认） | PostgreSQL |
|---|---|---|
| 配置 | `[queue] backend = "sqlite"`（不写也是），任务库 = `[paths] db` | `[queue] backend = "pg"` + `[pg]` 段 |
| 机器数 | 一台；同一个任务库同时只允许一个下载进程 | 任意多台，随时加减，所有机器对等 |
| 外部服务 | 无 | 一台 PostgreSQL；需要 `pip install 'paperlite[pg]'` |
| 领取 | 逐条 | 按块（默认 1000 条），带租约；机器挂了由别的机器接手 |
| 巡检 | 全库 | 只在本机领到的块里 |
| 卷期停止 | 有（curl 模式） | 无 |
| `export` / `retry` | 支持 | 暂不支持，用 SQL |

**怎么选**：一台机器跑得完就用 SQLite，什么都不用部署。批次大到一台跑不完、要多台机器一起跑时换 PostgreSQL，部署步骤见[多机部署](multi-machine.md)。

后端写在配置文件里，`import`、`run`、`status`、`inspect` 等所有命令都按它选库，不用每条命令加参数。临时对某条命令改用 PostgreSQL 可以加 `--pg`。两个后端的数据不互通。

## 存储位置

`[paths]` 中的两个位置相互独立：

- **`db`**：SQLite 任务库（PostgreSQL 后端下不用），**必须放在本地盘**。SQLite 依赖随机写、文件锁和 mmap，goofys、s3fs 等 FUSE 挂载都不支持。
- **`storage`**：PDF 与 HTML 的输出目录。只有顺序写，可以放对象存储挂载。

```toml
[paths]
db      = "/data/db/tasks.db"   # 本地盘
storage = "/mnt/obs/papers"     # 对象存储挂载
```

命令行的 `--db`、`--storage` 优先于配置文件。
