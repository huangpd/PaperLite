# PaperLite：大批量下载 PDF / 网页（过 Cloudflare）

**一份 config = 一个批次**：各种主机的 URL 混着导进**一个任务库**，默认用**浏览器模式**统一跑——每个浏览器 + IP 过一次盾后连续下，不同主机的页面各自过盾、能自己跟跳转、整页存 `.html`。不区分站点，不用关心主机。

位置在 `[paths]` 里定义、两者相互独立：

- **`db`** —— 任务库（SQLite），**必须本地盘**（要随机写/文件锁/mmap，goofys/s3fs 等 FUSE 挂载不支持）；
- **`storage`** —— 输出（PDF/HTML），可放对象存储挂载（顺序写）。

另有 **curl 模式**（`run --curl`）：单主机 cookie 池（浏览器过盾存 cookie、curl 批量领用下载，省资源、吞吐最高），用 Redis，适合一个站点的海量 URL；日常大概率用不到，混多主机会直接报错提示改用浏览器模式。

独立项目（原 `paper_downloader/cookie_pool/`，2026-10-25 拆出）：自带依赖和 venv，配置是运行目录的 `config.toml`，不依赖 `paper_downloader`，不用 PostgreSQL。

## 快速开始

需要 Python ≥ 3.11、一个短效代理提取接口（每次返回一个新 IP，青果格式）。浏览器模式**不用 Redis**（只有 curl 模式需要）。

```bash
pip install paperlite                     # 或 uv tool install paperlite；国内加 -i https://pypi.tuna.tsinghua.edu.cn/simple
mkdir work && cd work
paperlite init                            # 生成 ./config.toml（权限 600）
```

然后编辑 `config.toml`：填 `[proxy]` 的提取接口和代理账号密码、`[paths]` 的 `db`/`storage`，再在 `[run]` 里写好这次要跑的（模式、URL 文件……见下「配置文件启动」）。之后每次就一行：

```bash
paperlite run config.toml                # 按该文件的 [run] 导入 + 下载（默认浏览器模式），下载完自动退出
```

**推荐把每次运行都落到配置文件里**：命令行参数跑完就忘，配置文件一直留在本地——哪个库、什么模式、存哪，打开文件就知道，换机器、重跑、交接都照着它来。想临时试一把也可以直接敲子命令（`paperlite import --urls mixed.csv` 然后 `paperlite run`），参数见下「命令」。

从源码装（开发）：

```bash
git clone https://github.com/huangpd/PaperLite.git && cd PaperLite
uv sync                                    # 国内：uv venv && uv pip install -e . --index-url https://pypi.tuna.tsinghua.edu.cn/simple
uv run paperlite init
```

运行中每 30 秒打印一行进度。随时 Ctrl+C（或 SIGTERM）优雅退出：在途任务放回 pending、身份还池、释放生产者锁，再跑同一条命令从断点继续。

> `paperlite` 是安装生成的命令，下文统一写 `uv run paperlite ...`（源码目录内）。用清华源手动装的环境请改用 `.venv/bin/paperlite`——`uv run` 会按 `uv.lock` 去官方源重新同步，国内服务器会卡住。

## 命令

| 命令 | 作用 |
|---|---|
| `init [--force]` | 生成配置模板 `config.toml` |
| `import --urls <文件> [--type pdf\|html]` | 导入任务。txt 一行一链接，或带 `url` 列的 CSV（可选 `doi`/`source`/`batch_no`/`url_type`）。重复导入不重置已有任务 |
| `run [config.toml] [--urls <文件>] [--workers 20]` | 下载，完成即退出。**默认浏览器模式**（各种主机混着跑、跟跳转）；`--curl` 走单主机 cookie 池；`--no-proxy` 本地直连 |
| `produce [--keep N]` | 只生产身份（curl 模式），常驻 |
| `download [--workers 20]` | 只下载（curl 模式），需另有生产者在跑 |
| `status` | 各状态数量、失败按 `error_kind` 分组、停卷期（curl 下含身份池） |
| `retry --status failed [--kind network]` | 把失败（或 `non_pdf`）重置为 pending |
| `retry --stopped-volumes` | 让被停卷期重新抽样核实 |
| `export [--status non_pdf] [--file x.csv]` | 导出任务表为 CSV |
| `inspect` | 巡检：同标题重复到阈值就归纳 doi/url 规则，标记疑似失效/封面的未采任务 |
| `turnstile --url <页> --sitekey <key>` | 取 Cloudflare Turnstile token（见下） |

**位置**：任务库 = `--db` > `[paths].db`；输出目录 = `--storage`（仅 `run`/`download`）> `[paths].storage`。日常把这些写进配置，命令行不用堆。全局参数：`--config` / `--log-level` / `--log-format` / `--version`。

**日志**：一份事件流，终端和文件各自渲染。日志文件（`[paths] log_dir`）始终是 JSON Lines，字段齐全、好检索。终端默认 `--log-format auto`——交互式窗口给人类可读文本（`时间 级别 消息  key=val`，按级别着色，认 `NO_COLOR`），被重定向 / 管道 / docker 采集（非 TTY）时自动转 JSON。所以交互盯着跑是文本、`nohup paperlite run config.toml > run.log` 落盘的 `run.log` 是 JSON，都不用加参数。要手动固定就用 `--log-format text` / `json`。

> curl 模式（`run --curl` / `download` / `produce`）要求任务库里只有一个主机，混了会报错并提示改用浏览器模式；浏览器模式不限。

## 常用操作

```bash
# 后台跑 + 停止
setsid nohup .venv/bin/paperlite run config.toml > run.log 2>&1 < /dev/null &
kill <pid>                                 # 或 pkill -f "paperlite run"（注意别连执行它的 shell 一起杀）
```

```toml
# 输出在对象存储挂载、任务库在本地盘：两者各写各的，互不推导
[paths]
db      = "/data/db/tasks.db"              # 本地盘
storage = "/mnt/obs/papers"                # 对象存储挂载
```

- **省冷启动**（curl 模式）：第一个身份要等一次过盾（约 20 秒）。下载前先 `produce --keep 20` 养身份，代价是没人下载时也耗 IP。
- **换/加站点**：浏览器模式随便混主机、无需配置。可选在 `[landing]` 给某主机配过盾落地页（任意会触发挑战的普通页）；curl 模式还要把 `[proxy] check_url` 换成该站可达地址，`classify.py` 的 non_pdf 判断按 ACM 写的，换站要看一下。

## 配置文件启动（`[run]`）

把一次运行的参数写进配置的 `[run]` 段，然后 `paperlite run config.toml` 就按这个文件启动，等价于一条展开的 `paperlite run ...`。参数都在文件里，命令行不用堆 `--out`/`--host`/`--db`——不怕忘、换机器重跑和交接都照着文件来。给了配置文件时，命令行其余 `run` 参数被忽略，一切以 `[run]` 为准。

```toml
[run]                 # 配置后一行启动：paperlite run config.toml
command = "run"       # 填 "run"：裸 paperlite（不带子命令）也按此启动；留空则裸 paperlite 只打印帮助（run config.toml 仍可用）
mode = "browser"      # browser（各种主机混着跑，默认）/ curl（单主机 cookie 池）；省略按 browser
proxy = true          # false 等价于 --no-proxy（仅 browser）
workers = 20          # browser=同时打开的浏览器数；curl=下载线程数
urls = "mixed.csv"    # 可选：启动时先导入这个 URL 文件
url_type = "pdf"      # pdf / html
headed = false        # 仅 browser：有界面（调试）
```

库和输出位置不在 `[run]` 里，而在 `[paths]`（`db` / `storage`）——与模式无关、所有命令共用。`import` / `status` / `retry` / `export` 等仍走子命令（各有专属参数，没有配置等价物）。

## 主要配置（config.toml）

| 配置 | 默认 | 说明 |
|---|---|---|
分三段：`[proxy]`（代理）、`[paths]`（位置）、`[limits]`（并发与行为）。

| 段.项 | 默认 | 说明 |
|---|---|---|
| `[proxy] api_url` / `username` / `password` | | 提取接口与代理鉴权（必填） |
| `[proxy] check_url` / `check_url2` | cloudflare / httpbin | 新 IP 体检的两个判定目标，任一通过即合格 |
| `[proxy] check_max_latency_ms` / `verify_exit_ip` | 3000 / true | 体检延迟闸 / 核对回显出口 IP == proxy_ip |
| `[paths] db` | `tasks.db` | 任务库文件（**必须本地盘**） |
| `[paths] storage` | `output` | 输出目录 PDF/HTML（可放对象存储挂载） |
| `[paths] log_dir` | `log` | 日志目录；空则只打终端 |
| `[limits] max_browsers` | 20 | 同时运行的浏览器上限 |
| `[limits] pass_timeout_s` / `hedge_after_s` | 90 / 45 | 单次过盾上限 / 超过这么久就另开一个补位 |
| `[limits] request_gap_s` / `page_interval_s` | [3,5] / [10,30] | 同 IP 请求间隔 / 浏览器两篇间隔 |
| `[limits] max_file_size_mb` / `max_tries` | 200 / 3 | 单个 PDF 上限 / 网络错误重试次数 |
| `[limits] volume_stop_after` | 3 | curl：同卷期接连这么多篇无 PDF 就停该卷期其余；0 关闭 |
| `[limits] title_dup_trigger` | 10 | 巡检：同标题重复这么多篇就归纳规则跳过未采任务；0 关闭 |
| `[limits] max_identities` / `max_fresh_rejects` | 20 / 5 | curl：健康身份上限 / 熔断阈值 |

完整列表见 `paperlite init` 生成的模板（`src/paperlite/config.example.toml`）。查找顺序：`--config` > 环境变量 `PAPERLITE_CONFIG` > 当前目录 `config.toml`。

## 监控大屏（`[monitor]` + RabbitMQ）

每个终端（一次 `paperlite run`）把运行指标发到**同一个公共 RabbitMQ**，`paperlite-monitor serve` 消费入库（SQLite，保留 30 天）并出网页大屏，可切最近 24 小时 / 7 天 / 30 天。监控是旁路：没配、RabbitMQ 连不上、没装 pika，下载都照常跑。

**公网部署（只需公网 IP，无需域名：自建 CA 按 IP 签证书、RabbitMQ TLS、最小权限账号、面板自带 HTTPS + 登录密码、数据库与容量）见 [`docs/MONITOR.md`](docs/MONITOR.md)。**

```bash
pip install 'paperlite[monitor]'                     # 多装一个 pika（uv：uv sync --extra monitor）

# 每个终端的 config.toml 加上（其余不用改）：
#   [monitor]
#   enabled  = true
#   amqp_url = "amqps://pl_terminal:密码@服务器IP:5671/paperlite"
#   amqp_ca_file = "ca.pem"       # 服务器上 deploy/monitor/gen-certs.sh 生成的 CA
#   vendor   = "qingguo"          # 代理厂商标签，大屏按「厂商 × 域名」统计
paperlite-monitor test                                # 终端上验证到 RabbitMQ 的连通性
paperlite run config.toml                             # 照常跑，自动上报

# 面板机器（[monitor] 段写 amqp_url / db / password 等）：
paperlite-monitor serve                               # 设了 tls_cert 即 https://IP:8700/，设了 password 则需登录
paperlite-monitor demo                                # 不连 RabbitMQ，灌模拟数据看效果
```

| 大屏区块 | 指标 | 口径 |
|---|---|---|
| IP 质检 | 质检数、通过率、均延迟、不通过原因 | 每个新 IP 一条（体检通过才给浏览器），不分域名 |
| 浏览器 | 在线数 / 目标数、启动成功率、异常数、均过盾秒数 | 在线数来自心跳（`heartbeat_s`，默认 15s）；启动 = 起浏览器 + 过热身落地页；异常 = 启动抛错（区别于过盾超时） |
| 页面采集 | 成功、失败、成功率、过盾率 | **按尝试计**（过盾超时后稍后重试的，每次都算一次）；过盾 = 页面真加载出来；成功 = 过盾且 HTTP < 400 |
| 页面耗时 | 平均、P95 | 只算已加载的页（超时尝试的耗时就是超时上限，算进去会把 P95 拉成常数）；P95 由每小时直方图合并，误差约 ±6% |
| 域名 | 每域名成功率 / 过盾率 / 耗时 / 当前每分钟篇数 | 即「IP 过了质检后能否过该域名风控」 |
| 代理厂商 | 厂商 × 域名成功率、厂商 IP 质检通过率 | 厂商 = 终端配置的 `vendor` |
| WAF 过盾 | Cloudflare / AWS WAF / DataDome 的过盾率、均耗时、P95 | WAF 按主文档响应头识别（`cf-ray`/`cf-mitigated`、`x-amzn-waf-action`/`aws-waf-token`、`x-datadome`/`datadome=`），表示「站点前面是哪家」；页面 + 热身落地页都计 |
| 实时任务 | 待下载 / 进行中、域名访问频率（近 5 分钟）、实时事件、终端列表 | 事件含页面/浏览器结果和各终端 WARNING 以上日志 |

- **消息**：topic exchange `paperlite.monitor`，routing key = 事件类型（`page`/`browser`/`proxy_check`/`heartbeat`/`log`），JSON 信封格式见 `monitor/schema.py`。只发出口 IP 与统计，不发代理密码、不发页面内容。
- **不丢、不堵**：终端本地有界缓冲（`buffer_size`，满了丢最旧的），后台线程发送、断线退避重连，绝不阻塞下载。面板用持久队列（`paperlite.monitor.dashboard`，上限 `queue_max_length` 丢最旧），面板停机期间消息排队、重启后补齐；先落盘再 ack。
- **终端标识**：`node`（默认主机名）+ pid；批次 = 配置文件名（`sciencedirect.toml` → `sciencedirect`）。心跳超过 `max(60s, 4×heartbeat_s)` 没来即判离线。
- **存储**：入库时按小时汇总，大屏查询代价与事件量无关（30 天约 1 秒内）；原始事件只留 `raw_retention_hours`（默认 48h）给实时事件流。
- **安全**：面板默认只听 127.0.0.1；设了 `password` 即需登录（HTTP Basic）。对外开放时用 `tls_cert`/`tls_key` 让面板自带 HTTPS（或放在 HTTPS 反代后面）；监听非本机地址却没设密码会拒绝启动。
- curl 模式只上报心跳和 IP 质检（页面级埋点只在浏览器模式）。

## Turnstile token（通用）

给目标页 URL 和 sitekey 即可取 token，不依赖站点配置和 Redis：

```bash
uv run paperlite turnstile --url https://example.com/login --sitekey 0x4AAAA... --json
# {"ok": true, "token": "1.xxxx…", "user_agent": "...", "seconds": 15.3, ...}

uv run paperlite turnstile --serve 8191 --concurrency 4        # HTTP 服务
curl localhost:8191/solve -d '{"url":"...","sitekey":"0x4AAAA...","proxy":"http://u:p@1.2.3.4:8000"}'
```

原理：CloakBrowser 打开目标 URL 但拦截该文档、换成只渲染此 sitekey 的最小页（origin 仍是目标站，能过 hostname 校验），在 Turnstile iframe 上按坐标点复选框，从 callback 拿 token；卡 "Verifying…" 时每 8 秒补点一次。token 一次性、约 300 秒有效，有的站校验提交 IP/UA，须用同一代理和返回的 `user_agent`。过盾（`produce`/`run`）遇交互式 Turnstile 也自动点，见 `turnstile.py`。

## 测试

```bash
uv run pytest                                              # Redis 相关的会跳过
PAPERLITE_TEST_REDIS=redis://127.0.0.1:6379/0 uv run pytest   # 连真实 Redis 全跑
PAPERLITE_TEST_AMQP=amqp://guest:guest@127.0.0.1:5672/%2F uv run pytest tests/test_monitor.py   # 监控连真实 RabbitMQ
```

## 任务库 tasks.db

表 `paper_pdf_resources`（结构对齐 paper_downloader 的 PG 同名表）。关键列：

| 列 | 说明 |
|---|---|
| `id` | 自增主键，按它的顺序领取 |
| `url_key` | sha256(规范化 url)，唯一 |
| `doi` | 导入值（小写）或从链接回退推导；决定文件名 |
| `url` / `source` / `batch_no` | 导入原值 |
| `url_type` | `pdf`（默认，应返回 PDF）/ `html`（要网页本身，整页存 `.html`） |
| `volume` | DOI 卷期号（`10.1145/1012326.1012328` → `1012326`），用于卷期停止 |
| `status` | `pending` → `running` → `success` / `non_pdf` / `failed` |
| `error_kind` | `not_found`/`paywall`/`landing_page`（non_pdf）；`network`/`server_error`/`extract_failed`；`challenge_failed` |
| `title` / `final_url` | 页面标题；网页任务跟完跳转的最终地址 |
| `http_status` / `file_size` / `elapsed_ms` / `exit_ip` | 最近一次尝试的指标 |
| `retry_count` / `challenge_count` | 网络错误/5xx 计数；带有效 cookie 仍被挑战的次数 |
| `inferred` | 1 = 按卷期推断、未实际请求 |
| `file_path` | 成功后绝对路径 `<storage>/<h[0:2]>/<h[2:4]>/<doi>.pdf`（网页为 `.html`），`h=sha256(doi)` |

查库：`sqlite3 tasks.db "SELECT status,error_kind,count(*) FROM paper_pdf_resources GROUP BY 1,2"`。另有 `meta`（本库主机）、`volume_stops`（被停卷期）、`skip_rules`（巡检规则）表。

## 设计要点

- **身份 = IP + cookie + UA**：cf_clearance 绑 IP + UA，三者一起存一起用，换任意一个都 403。青果短效 IP 约 10 分钟，身份过期取 IP 与 cookie 较早者，约 9 分钟换新，故须持续生产。
- **过盾要么快要么卡死**：成功多在 12–30 秒，失败停在 "Verifying…" 转到超时。默认 `pass_timeout_s=90` 并对冲：超 `hedge_after_s=45` 秒没过就另开浏览器补位，旧的继续跑到超时。机房直连出口会要求手动勾选，必须走代理。
- **被挑战 ≠ 身份坏**：站点会单独盯上某些 URL（往往成批相邻）。遇挑战先用落地页验身份——落地页正常只是这条 URL 被盯上（`challenge_count+1` 排后面），落地页也被挑战才丢身份。不能用"下一条也被挑战"判身份，否则会把好身份接连误杀。
- **熔断**：连续 `max_fresh_rejects` 个新身份一用就被拒 → 生产者暂停 `breaker_pause_s` 并告警，之后放一个试探；网络错误不计入。防代理配额耗尽时 1 分钟烧光 IP。
- **卷期停止**：ACM 的 404 / 无 PDF 基本按卷期整组出现。未下到 PDF 的卷期同时最多放 `volume_stop_after` 篇，都 non_pdf 就停该卷期其余 pending（记 `inferred=1`，未请求）；下到过一篇 PDF 的卷期永不停。`retry --stopped-volumes` 重新核实。
- **流式下载**：卡顿超时而非总时长，慢代理上大文件不被掐断；`%PDF-` 须在前 1024 字节内。网络错误 `(56)` 先用同一身份重试一次再丢。
- **巡检**（浏览器模式）：DOI 唯一却得到相同标题≈整批失效或整批封面。任何**真加载出来**的页（成功页 + 404 等失效页，不含过盾超时页）标题重复到 `title_dup_trigger` 次，就从 doi/url 两维归纳**最具体**的前缀规则、用已采数据验精确率（≥0.98），达标才把命中的 pending 标记跳过——宁可少跳、分批触发，也不误标未采的活任务。只看本次运行的内存滑动窗口。

性能（ACM 实测）：20 线程每分钟处理 120–160 条（PDF 约 140）；每身份用约 9 分钟下 60–70 篇，含过盾失败，**每 1 万篇约需 180–300 个 IP**（换 30 分钟时效 IP 约 1/3）。踩坑细节见 `POSTMORTEMS.md`。

## 目录

```
src/paperlite/     代码（src 布局；config.example.toml 是配置模板）
  store.py         Redis 身份池（Lua 原子领用、租约、生产者锁）
  producer.py      按需生产身份、控制并发过盾
  browser.py       一次过盾（独立 CloakBrowser 进程经代理打开落地页）
  turnstile.py     Turnstile 定位点击、按 sitekey 取 token、HTTP 服务
  browser_pool.py  浏览器模式的浏览器复用
  consumer.py      下载线程（领任务+身份、流式下载、身份健康判定）
  tasks.py         SQLite 任务库、卷期停止
  inspect.py       巡检归纳规则
  classify.py      non_pdf 分类（not_found/paywall/landing_page）
  proxy.py         短效代理取 IP + 分层体检（TCP/TLS/TTFB 四段计时、核对出口 IP、延迟闸）
  config.py cli.py 配置（[proxy]/[limits]/[paths]/[run]）/ 命令行
  monitor/         监控：reporter.py 埋点上报（未启用即 no-op）；db/consumer/dashboard/cli 为面板端（paperlite-monitor）
tests/             pytest（unittest 风格；Redis 用例按 PAPERLITE_TEST_REDIS 门控）
config.toml        paperlite init 生成（含代理密码，不提交）
```
