# PaperLite：大批量下载 PDF / 网页（过 Cloudflare）

**用本工具第一个要定的是模式，按"要下几个站点"选**：

- **同一站点、海量 URL → curl 模式（默认）**：浏览器只负责持续过 Cloudflare、把身份（IP + cookie + UA）攒进 Redis 池，curl_cffi 领用后高速批量下载。省资源、吞吐最高；代价是一个任务库只能一个主机（混了会直接报错提示改用浏览器）。
- **多个站点混在一起 → 浏览器模式（`run --browser`）**：每个浏览器 + IP 过一次盾后连续下同域名多篇，不同主机的页面各自过盾，能自己跟跳转、整页存 `.html`。一个库可混多主机。

早期版本以"主机"为中心（一主机一库，到处 `--host`）；改版后一个库可容纳多主机，主机不再是硬边界——真正先定的就是上面这个"单站点还是跨站点"，`--host` / `--out` 只是在本地有多个站点目录时用来选哪个。

生产与下载只通过 Redis 交互，可分进程、分机器跑；任务与结果存站点输出目录下的 SQLite 库 `tasks.db`。

独立项目（原 `paper_downloader/cookie_pool/`，2026-10-25 拆出）：自带依赖和 venv，配置是运行目录的 `config.toml`，不依赖 `paper_downloader`，不用 PostgreSQL。

## 快速开始

需要 Python ≥ 3.11、Redis、一个短效代理提取接口（每次返回一个新 IP，青果格式）。

```bash
pip install paperlite                     # 或 uv tool install paperlite；国内加 -i https://pypi.tuna.tsinghua.edu.cn/simple
mkdir work && cd work
paperlite init                            # 生成 ./config.toml（权限 600）

# Redis：只监听本机
docker run -d --name paperlite-redis -p 127.0.0.1:6379:6379 redis:latest redis-server --save ""
```

然后编辑 `config.toml`：填 `[proxy]` 的提取接口和代理账号密码，再在 `[run]` 里把这次要跑的东西写全（模式、URL 文件、输出目录……见下「配置文件启动」）。之后每次就一行：

```bash
paperlite run config.toml                # 按该文件的 [run] 导入 + 下载，下载完自动退出
```

**推荐把每次运行都落到配置文件里**：命令行参数跑完就忘，配置文件一直留在本地——哪个库、哪个站点、什么模式，打开文件就知道，换机器、重跑、交接都照着它来。想临时试一把也可以直接敲子命令（`paperlite import --urls acm.txt` 然后 `paperlite run`），参数见下「命令」。

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
| `run [--urls <文件>] [--workers 20]` | 下载，完成即退出。模式见上文（默认 curl / 单主机；`--browser` 可混多主机；`--browser --no-proxy` 本地直连） |
| `produce [--keep N]` | 只生产身份，常驻 |
| `download [--workers 20]` | 只下载，需另有生产者在跑 |
| `status` | 各状态数量、失败按 `error_kind` 分组、停卷期、身份池 |
| `retry --status failed [--kind network]` | 把失败（或 `non_pdf`）重置为 pending |
| `retry --stopped-volumes` | 让被停卷期重新抽样核实 |
| `export [--status non_pdf] [--file x.csv]` | 导出任务表为 CSV |
| `inspect` | 巡检：同标题重复到阈值就归纳 doi/url 规则，标记疑似封面的未采任务 |
| `turnstile --url <页> --sitekey <key>` | 取 Cloudflare Turnstile token（见下） |

日常跑 `run` 建议写进 `[run]`（见下），不用在命令行堆参数。下面这些 flag 仍然可用，适合临时一次性操作：`--out`（站点输出目录）、`--host`（主机，目录取 `<out_root>/<主机>`）、`--db`（任务库路径）。本地只有一个站点目录时都能省略，多个时才需要指定选哪个。全局参数：`--config` / `--log-level` / `--log-format` / `--version`。

**日志**：一份事件流，终端和文件各自渲染。日志文件（`[pool] log_dir`）始终是 JSON Lines，字段齐全、好检索。终端默认 `--log-format auto`——交互式窗口给人类可读文本（`时间 级别 消息  key=val`，按级别着色，认 `NO_COLOR`），被重定向 / 管道 / docker 采集（非 TTY）时自动转 JSON。所以交互盯着跑是文本、`nohup paperlite run ... > run.log` 落盘的 `run.log` 是 JSON，都不用加参数。要手动固定就用 `--log-format text` / `json`。

定位规则（命令行不指定时的默认）——**站点目录**：`--out` > `--host` / URL 主机 / `--db` 记录的主机 → `<out_root>/<主机>` > `out_root` 下唯一站点；**任务库**：`--db` > `[pool] db_dir`（`<db_dir>/<目录名>-<哈希>.db`）> `<输出目录>/tasks.db`。

> curl 模式（`run` 默认 / `download` / `produce`）要求任务库里只有一个主机，混了会报错并提示用 `run --browser`；浏览器模式不限。

## 常用操作

```bash
# 后台跑 + 停止
setsid nohup .venv/bin/paperlite run --workers 20 > logs/acm.log 2>&1 < /dev/null &
kill <pid>                                 # 或 pkill -f "paperlite run"（注意别连执行它的 shell 一起杀）

# 输出目录在对象存储挂载（goofys/s3fs）上：任务库必须放本地盘
#   SQLite 要随机写/文件锁/mmap，FUSE 不支持；PDF 是顺序写可以放挂载上
```

```toml
[pool]
db_dir = "/data/paperlite/db"              # 任务库自动放这里，输出目录随便挂
```

- **生产/下载分开**：生产者常驻，下载随用随开；多机下载共用一个 Redis（`redis_url` 指同一台，跨机需改监听并加密码）。同主机同时只一个生产者干活，多开待命，前一个退出 15 秒内接手。
- **省冷启动**：第一个身份要等一次过盾（约 20 秒）。下载前先 `produce --keep 20` 养身份，代价是没人下载时也耗 IP。
- **换站点**：`[landing]` 加一行该主机的落地页（任意会触发挑战的普通页），`[proxy] check_url` 换成该站可达地址；`classify.py` 的 non_pdf 判断按 ACM 写的，换站要看一下。

## 配置文件启动（`[run]`）

把一次运行的参数写进配置的 `[run]` 段，然后 `paperlite run config.toml` 就按这个文件启动，等价于一条展开的 `paperlite run ...`。参数都在文件里，命令行不用堆 `--out`/`--host`/`--db`——不怕忘、换机器重跑和交接都照着文件来。给了配置文件时，命令行其余 `run` 参数被忽略，一切以 `[run]` 为准。

```toml
[run]                 # 配置后一行启动：paperlite run config.toml
command = "run"       # 填 "run"：裸 paperlite（不带子命令）也按此启动；留空则裸 paperlite 只打印帮助（run config.toml 仍可用）
mode = "curl"         # curl（单主机，默认）/ browser（可混多主机）；省略按 curl
proxy = true          # false 等价于 --no-proxy（仅 browser）
workers = 20          # curl=下载线程数；browser=同时打开的浏览器数
urls = "acm.txt"      # 可选：启动时先导入这个 URL 文件
url_type = "pdf"      # pdf / html
out = ""              # 可选：精确输出目录，非空覆盖 [pool] out_root
host = ""             # 可选：站点主机，目录取 <out_root>/<主机>
db = ""               # 可选：精确任务库文件，非空覆盖 [pool] db_dir
headed = false        # 仅 browser：有界面（调试）
```

`import` / `status` / `retry` / `export` 等仍走子命令（它们各有专属参数，没有配置等价物）。

## 主要配置（config.toml）

| 配置 | 默认 | 说明 |
|---|---|---|
| `[proxy] api_url` / `username` / `password` | | 提取接口与代理鉴权（必填） |
| `out_root` | `pdfs` | 不给 `--out` 时的输出根目录 |
| `db_dir` | 空 | 任务库目录（本地盘）；留空则放 `<--out>/tasks.db`。挂载到对象存储时必须设 |
| `max_browsers` | 20 | 同时过盾的浏览器上限 |
| `max_identities` / `min_identities` | 20 / 0 | 健康身份上限 / 没人下载时养着的数；实际按在线下载线程数生产 |
| `pass_timeout_s` / `hedge_after_s` | 90 / 45 | 单次过盾上限 / 超过这么久就另开一个补位 |
| `request_gap_s` | [3, 5] | 同一 IP 两次请求的随机间隔 |
| `download_timeout_s` | 120 | 卡顿超时：连续这么久收不到数据才算超时（不限总时长） |
| `max_file_size_mb` | 200 | 单个 PDF 上限 |
| `max_tries` | 3 | 网络错误重试次数（cookie 被拒不计） |
| `max_url_challenges` | 3 | 单 URL 带有效 cookie 仍被挑战满这么多次 → `challenge_failed` |
| `volume_stop_after` | 3 | 同卷期接连这么多篇无 PDF 就停该卷期其余任务；0 关闭 |
| `max_fresh_rejects` / `breaker_pause_s` | 5 / 600 | 新身份接连一用就被拒时熔断暂停生产 |

完整列表见 `paperlite init` 生成的模板（`src/paperlite/config.example.toml`）。查找顺序：`--config` > 环境变量 `PAPERLITE_CONFIG` > 当前目录 `config.toml`。

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
| `file_path` | 成功后绝对路径 `<out>/<h[0:2]>/<h[2:4]>/<doi>.pdf`，`h=sha256(doi)` |

查库：`sqlite3 pdfs/acm/tasks.db "SELECT status,error_kind,count(*) FROM paper_pdf_resources GROUP BY 1,2"`。另有 `meta`（本库主机）、`volume_stops`（被停卷期）、`skip_rules`（巡检规则）表。

## 设计要点

- **身份 = IP + cookie + UA**：cf_clearance 绑 IP + UA，三者一起存一起用，换任意一个都 403。青果短效 IP 约 10 分钟，身份过期取 IP 与 cookie 较早者，约 9 分钟换新，故须持续生产。
- **过盾要么快要么卡死**：成功多在 12–30 秒，失败停在 "Verifying…" 转到超时。默认 `pass_timeout_s=90` 并对冲：超 `hedge_after_s=45` 秒没过就另开浏览器补位，旧的继续跑到超时。机房直连出口会要求手动勾选，必须走代理。
- **被挑战 ≠ 身份坏**：站点会单独盯上某些 URL（往往成批相邻）。遇挑战先用落地页验身份——落地页正常只是这条 URL 被盯上（`challenge_count+1` 排后面），落地页也被挑战才丢身份。不能用"下一条也被挑战"判身份，否则会把好身份接连误杀。
- **熔断**：连续 `max_fresh_rejects` 个新身份一用就被拒 → 生产者暂停 `breaker_pause_s` 并告警，之后放一个试探；网络错误不计入。防代理配额耗尽时 1 分钟烧光 IP。
- **卷期停止**：ACM 的 404 / 无 PDF 基本按卷期整组出现。未下到 PDF 的卷期同时最多放 `volume_stop_after` 篇，都 non_pdf 就停该卷期其余 pending（记 `inferred=1`，未请求）；下到过一篇 PDF 的卷期永不停。`retry --stopped-volumes` 重新核实。
- **流式下载**：卡顿超时而非总时长，慢代理上大文件不被掐断；`%PDF-` 须在前 1024 字节内。网络错误 `(56)` 先用同一身份重试一次再丢。
- **巡检**（浏览器模式）：同标题重复到 `title_dup_trigger` 次，从 doi/url 两维归纳规则、用已采数据验精确率，达标才把命中的 pending 标 `suspect_cover` 跳过。只看本次运行的内存滑动窗口。

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
  proxy.py         短效代理取 IP 并体检
  config.py cli.py 配置 / 命令行
tests/             pytest（unittest 风格；Redis 用例按 PAPERLITE_TEST_REDIS 门控）
config.toml        paperlite init 生成（含代理密码，不提交）
```
