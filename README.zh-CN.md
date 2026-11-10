# PaperLite

[![PyPI](https://img.shields.io/pypi/v/paperlite.svg)](https://pypi.org/project/paperlite/)
[![Python](https://img.shields.io/pypi/pyversions/paperlite.svg)](https://pypi.org/project/paperlite/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[English](README.md) | **简体中文**

PaperLite 是一个面向大批量场景的 PDF 与网页下载工具，适用于部署在 Cloudflare 等反爬防护之后的站点。它通过轮换的短效代理驱动真实浏览器，将每个任务持久化到本地 SQLite 数据库，中断后可从断点继续。

- **一份配置即一个批次**：任意多个主机的 URL 导入同一个任务库，统一调度。
- **默认浏览器模式**：每个 worker 独占一个浏览器和一个 IP，过盾一次后连续下载；自动跟随跳转，可保存 PDF 或完整 HTML 页面。
- **可续跑、可追溯**：任务状态、错误分类和每次尝试的指标均存于 SQLite；`Ctrl+C` 会把在途任务放回队列。
- **自我保护**：代理体检、IP 中途失效自动换 IP、代理配额耗尽时熔断，以及跳过失效页或重复内容的启发式规则。
- **可选的集群监控**：各终端把指标上报到 RabbitMQ，由内置面板跨机器汇总展示。

## 目录

- [工作原理](#工作原理)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [配置](#配置)
- [命令参考](#命令参考)
- [运维](#运维)
- [监控](#监控)
- [Turnstile token](#turnstile-token)
- [任务库](#任务库)
- [设计说明](#设计说明)
- [开发](#开发)
- [许可证](#许可证)

## 工作原理

PaperLite 有两种运行模式，共用同一套任务库、存储布局和命令。

| | 浏览器模式（默认） | curl 模式（`--curl`） |
|---|---|---|
| 工作方式 | N 个 worker，各自独占浏览器与 IP | 浏览器过盾后把 cookie 存入身份池，`curl_cffi` 线程领用下载 |
| 每批次主机数 | 任意混合 | 仅限一个 |
| 跳转 / 网页 | 自动跟随，网页保存为 `.html` | 以 PDF 为主 |
| 外部依赖 | 无 | Redis（身份池） |
| 适用场景 | 来源混杂的 URL 列表 | 单站点海量任务、追求单 IP 吞吐 |

`[paths]` 中的两个位置相互独立：

- **`db`**：SQLite 任务库，**必须放在本地盘**。SQLite 依赖随机写、文件锁和 mmap，goofys、s3fs 等 FUSE 挂载都不支持。
- **`storage`**：PDF 与 HTML 的输出目录。只有顺序写，可放在对象存储挂载上。

## 环境要求

- Python 3.11 及以上，Linux 或 macOS。
- 一个短效代理提取接口，每次调用返回一个新的 `ip:port`（JSON 或纯文本均可），以及代理鉴权账号。本地测试可用 `--no-proxy` 不走代理。
- Redis：仅 curl 模式需要。
- RabbitMQ：仅启用监控时需要。

## 安装

```bash
pip install paperlite
# 或
uv tool install paperlite
```

国内网络可加镜像源，例如 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。

从源码安装：

```bash
git clone https://github.com/huangpd/PaperLite.git
cd PaperLite
uv sync
uv run paperlite --version
```

## 快速开始

```bash
mkdir work && cd work
paperlite init                 # 生成 ./config.toml
```

编辑 `config.toml`：

1. `[proxy]`：提取接口地址与代理账号密码。
2. `[paths]`：`db` 与 `storage`。
3. `[run]`：本次任务（模式、并发数、URL 文件）。

然后启动批次：

```bash
paperlite run config.toml
```

该命令会导入 URL 文件、下载全部任务，队列清空后自动退出，运行期间每 30 秒输出一行进度。`Ctrl+C` 或 `SIGTERM` 会优雅退出：在途任务回到 `pending`，再次执行同一命令即从断点继续。

> **建议**：每次运行都写进配置文件，而不是堆在命令行上。配置文件记录了批次使用的任务库、模式和输出位置，重跑、换机器、交接都可以照着它复现。

## 配置

配置文件的查找顺序为：`--config`，其次是环境变量 `PAPERLITE_CONFIG`，最后是当前目录的 `config.toml`。`paperlite init` 生成的模板（[`src/paperlite/config.example.toml`](src/paperlite/config.example.toml)）包含全部配置项及说明。

### `[run]`：启动参数

```toml
[run]
command  = "run"        # 让不带子命令的 paperlite 也按此启动；留空则只打印帮助
mode     = "browser"    # browser | curl
proxy    = true         # false 等同于 --no-proxy（仅浏览器模式）
workers  = 20           # 浏览器模式为并发浏览器数，curl 模式为下载线程数
urls     = "mixed.csv"  # 可选，启动时先导入
url_type = "pdf"        # pdf | html
headed   = false        # 仅浏览器模式：显示浏览器窗口，用于调试
```

给 `run` 传入配置文件时，以 `[run]` 段为准，命令行上的其他 `run` 参数会被忽略。其他子命令（`import`、`status`、`retry`、`export` 等）仍使用各自的命令行参数。

### 主要配置项

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `[proxy] api_url`、`username`、`password` | （必填） | 提取接口与代理鉴权 |
| `[proxy] check_url`、`check_url2` | Cloudflare、httpbin | 新 IP 的体检目标，任一通过即视为合格。应保持为回显出口 IP 的地址，不要改成目标站：体检只验连通性、延迟和出口 IP，能否过目标站的防护由过盾环节判断 |
| `[proxy] check_max_latency_ms` | 3000 | 体检延迟上限 |
| `[proxy] verify_exit_ip` | true | 要求回显的出口 IP 与代理 IP 一致 |
| `[paths] db` | `tasks.db` | 任务库（仅限本地盘） |
| `[paths] storage` | `output` | 输出目录（可挂载） |
| `[paths] log_dir` | `log` | 日志目录；留空则只输出到终端 |
| `[limits] max_browsers` | 20 | 并发浏览器上限 |
| `[limits] pass_timeout_s` | 90 | 单次过盾的最长时间 |
| `[limits] hedge_after_s` | 45 | 过盾超过该时长仍未完成时，另开一个浏览器补位 |
| `[limits] request_gap_s` | [3, 5] | 同一 IP 两次请求之间的间隔 |
| `[limits] page_interval_s` | [10, 30] | 同一浏览器两个页面之间的间隔 |
| `[limits] max_file_size_mb` | 200 | 单个文件大小上限 |
| `[limits] max_tries` | 3 | 网络错误的重试次数 |
| `[limits] volume_stop_after` | 3 | curl 模式：同一卷期连续这么多篇无 PDF 即停止该卷期；0 表示关闭 |
| `[limits] title_dup_trigger` | 10 | 同一标题重复到该次数时触发规则归纳；0 表示关闭 |
| `[limits] max_identities` | 20 | curl 模式：健康身份上限 |
| `[limits] max_fresh_rejects` | 5 | curl 模式：熔断阈值 |

命令行覆盖规则：`--db` 优先于 `[paths].db`；`--storage`（`run`、`download` 可用）优先于 `[paths].storage`。

## 命令参考

| 命令 | 说明 |
|---|---|
| `init [--force]` | 生成 `config.toml` 模板 |
| `import --urls FILE [--type pdf\|html]` | 导入任务。支持一行一个 URL 的文本文件，或含 `url` 列的 CSV（可选 `doi`、`source`、`batch_no`、`url_type` 列）。重复导入不会重置已有任务 |
| `run [CONFIG] [--urls FILE] [--workers N]` | 下载直至队列清空。默认浏览器模式；`--curl` 切换到 curl 模式；`--no-proxy` 使用本机 IP |
| `produce [--keep N]` | curl 模式：只运行身份生产者（常驻） |
| `download [--workers N]` | curl 模式：只运行下载线程，需另有生产者在运行 |
| `status` | 各状态计数、按 `error_kind` 分组的失败数、已停卷期，curl 模式下还包括身份池 |
| `retry --status failed [--kind KIND]` | 将失败（或 `non_pdf`）任务重置为 `pending` |
| `retry --stopped-volumes` | 让被卷期规则停止的卷期重新抽样核实 |
| `export [--status S] [--file out.csv]` | 将任务表导出为 CSV |
| `inspect [--list] [--reset]` | 查看、重建或清除重复标题跳过规则 |
| `turnstile --url URL --sitekey KEY` | 获取 Cloudflare Turnstile token（见[下文](#turnstile-token)） |

全局参数：`--config`、`--log-level`、`--log-format auto|text|json`、`--version`。

**日志**：`[paths].log_dir` 下的日志文件始终为 JSON Lines。终端输出默认为 `auto`：在交互式终端中输出带颜色的可读文本（遵循 `NO_COLOR`）；标准输出被重定向、接入管道或由容器采集时输出 JSON。

## 运维

**后台运行：**

```bash
setsid nohup paperlite run config.toml > run.log 2>&1 < /dev/null &
pkill -f "paperlite run"        # 优雅停止（SIGTERM）
```

**任务库在本地盘、输出在挂载盘：**

```toml
[paths]
db      = "/data/db/tasks.db"   # 本地盘
storage = "/mnt/obs/papers"     # 对象存储挂载
```

**新增站点**：无需按站点配置，主机直接取自任务库中的 URL。

- 每个新浏览器领任务前，先在第一个导入的主机上热身过盾，默认地址为 `https://{host}/`。
- 其他主机的页面在首次渲染时各自过盾。
- 若该主机首页不适合热身（例如不触发挑战或会跳转到别处），可在 `[landing]` 中覆盖热身地址，例如 `"www.example.org" = "https://www.example.org/journal/abc"`。
- curl 模式的身份生产使用同一主机和热身地址；另需检查 `classify.py` 中的 non-PDF 判定规则（按 ACM 编写）是否适用。

**curl 模式冷启动**：第一个身份过盾约需 20 秒。可先运行 `produce --keep 20` 预热身份池，代价是没有下载线程时也会消耗 IP。

> 通过镜像源安装的源码环境，请直接调用 `.venv/bin/paperlite`。`uv run` 会按 `uv.lock` 从默认源重新同步，在受限网络下可能卡住。

## 监控

每次 `paperlite run` 都可以把运行指标上报到一个共用的 RabbitMQ。`paperlite-monitor serve` 负责消费这些消息、写入面板自己的 SQLite（保留 30 天），并提供网页面板，可切换最近 24 小时、7 天、30 天视图。监控完全是旁路：未启用、RabbitMQ 不可达或缺少客户端库时，下载都不受影响。

在每个终端上启用：

```toml
[monitor]
enabled  = true
amqp_url = "amqp://pl_terminal:PASSWORD@BROKER_IP:5672/paperlite"
vendor   = "qingguo"            # 代理厂商标签，用于「厂商 × 域名」统计
```

```bash
paperlite-monitor test          # 在终端上验证到 RabbitMQ 的连通性
paperlite-monitor serve         # 面板地址 http://HOST:8700/
paperlite-monitor demo          # 使用模拟数据启动面板，无需 RabbitMQ
```

面板包含以下内容：

| 区块 | 指标 |
|---|---|
| IP 质检 | 质检次数、通过率、平均延迟、不通过原因 |
| 浏览器 | 在线数与目标数、启动成功率、异常数、平均过盾耗时 |
| 页面采集 | 成功数、失败数、成功率、过盾率、平均耗时与 P95 |
| 域名 | 各域名的成功率、过盾率、耗时与吞吐；服务端搜索、排序、分页；可按主域名合并 |
| 代理厂商 | 厂商 × 域名成功率；各厂商 IP 质检通过率 |
| WAF | Cloudflare、AWS WAF、DataDome 的过盾率与耗时，按响应头识别 |
| 实时 | 待下载与进行中任务数、各域名访问频率、实时事件流、终端列表 |

指标口径：

- 页面指标**按尝试计**：过盾超时后重试的任务，每次尝试各计一次。
- “过盾”指页面实际加载出来；“成功”指过盾且 HTTP 状态码小于 400。
- 耗时只统计已加载的页面。
- 样本少于 20 次的域名灰显。
- 成功率或过盾率低于 70% 的域名标记为异常。

上报消息只包含统计数据与出口 IP，不含代理密码和页面内容。终端把事件放入本地有界缓冲，由后台线程发送；面板先落盘再确认消息。

部署、账号权限、安全与容量规划见 [`docs/MONITOR.md`](docs/MONITOR.md)。

## Turnstile token

PaperLite 可以为任意页面求解 Cloudflare Turnstile，不依赖任务库和 Redis：

```bash
paperlite turnstile --url https://example.com/login --sitekey 0x4AAAA... --json
# {"ok": true, "token": "1.xxxx…", "user_agent": "...", "seconds": 15.3, ...}

paperlite turnstile --serve 8191 --concurrency 4
curl localhost:8191/solve -d '{"url":"...","sitekey":"0x4AAAA...","proxy":"http://u:p@1.2.3.4:8000"}'
```

求解过程：

1. 用 CloakBrowser 打开目标 URL，但拦截该文档，替换为只渲染指定 sitekey 的最小页面。页面的 origin 仍是目标站点，因此能通过 hostname 校验。
2. 点击 Turnstile iframe 中的复选框，从回调中取得 token。

token 一次性有效，有效期约 300 秒。部分站点会校验提交时的 IP 和 UA，提交时请使用同一代理和返回的 `user_agent`。

## 任务库

任务存放在 `paper_pdf_resources` 表中，表结构与上游 PostgreSQL 同名表保持一致。

| 列 | 说明 |
|---|---|
| `id` | 自增主键，按此顺序领取任务 |
| `url_key` | 规范化 URL 的 SHA-256，唯一 |
| `doi` | 导入值（转小写）或从 URL 推导，决定文件名 |
| `url`、`source`、`batch_no` | 导入时的原值 |
| `url_type` | `pdf`（期望得到 PDF）或 `html`（保存网页本身） |
| `volume` | DOI 卷期号（`10.1145/1012326.1012328` → `1012326`），用于卷期停止 |
| `status` | `pending` → `running` → `success` / `non_pdf` / `failed` |
| `error_kind` | `not_found`、`paywall`、`landing_page`（non-PDF）；`network`、`server_error`、`extract_failed`、`challenge_failed` |
| `title`、`final_url` | 页面标题；跟随跳转后的最终地址 |
| `http_status`、`file_size`、`elapsed_ms`、`exit_ip` | 最近一次尝试的指标 |
| `retry_count`、`challenge_count` | 网络错误或 5xx 的重试次数；持有效 cookie 仍被挑战的次数 |
| `inferred` | 为 1 表示结果由卷期停止推断得出，未实际请求 |
| `file_path` | `<storage>/<h[0:2]>/<h[2:4]>/<doi>.pdf`（网页为 `.html`），其中 `h = sha256(doi)` |

辅助表：`meta`、`volume_stops`、`skip_rules`。

```bash
sqlite3 tasks.db "SELECT status, error_kind, count(*) FROM paper_pdf_resources GROUP BY 1, 2"
```

## 设计说明

以下行为都是有意为之，相关事故记录见 [`POSTMORTEMS.md`](POSTMORTEMS.md)。

- **身份 = IP + cookie + UA**：`cf_clearance` 同时绑定 IP 和 UA，因此三者一起保存、一起使用，更换其中任何一个都会得到 403。身份的有效期取 IP 租期与 cookie 有效期中较早者，所以需要持续生产身份。
- **过盾要么很快，要么卡死**：成功的过盾通常耗时 12–30 秒，失败的则停在 “Verifying…” 直至超时。超过 `hedge_after_s`（45 秒）仍未通过时另开一个浏览器补位，原浏览器继续运行到 `pass_timeout_s`（90 秒）。机房 IP 常被要求人工勾选，因此实际上必须使用代理。
- **URL 被挑战不等于身份失效**：站点会单独盯上某些 URL，而且往往是连续的一批。遇到挑战时，先用落地页复验身份：
  - 落地页正常，说明只是这条 URL 被盯上，将其降低优先级（`challenge_count + 1`）。
  - 落地页也被挑战，才丢弃该身份。

  如果以“下一条是否也被挑战”来判断身份，会把健康的身份成批误杀。
- **IP 中途失效**：短效 IP 常在租期结束前就断开，表现为 `ERR_TUNNEL_CONNECTION_FAILED`、`ERR_PROXY_*`、`ERR_CONNECTION_CLOSED/RESET`、`ERR_EMPTY_RESPONSE`。这类错误归因于 IP，而不是任务本身：
  - 任务放回 `pending`，`error_kind=network`，不增加重试次数和挑战次数。
  - 同一浏览器连续失败两次即提前更换 IP。
- **熔断**：连续 `max_fresh_rejects` 个新身份首次使用即被拒时，生产者暂停 `breaker_pause_s` 并告警，之后放行一个身份试探。这可以避免代理配额耗尽时在一分钟内烧掉数百个 IP。网络错误不计入阈值。
- **卷期停止**（curl 模式）：缺失的 PDF 往往以整个卷期为单位出现。对尚未下到 PDF 的卷期，同时最多放出 `volume_stop_after` 个任务；若全部为 non-PDF，则将该卷期其余任务标记为 `non_pdf` 并记 `inferred=1`。下到过 PDF 的卷期永远不会被停止。
- **流式下载**：超时按卡顿时长计算，而不是总时长，因此慢代理上的大文件也能完整下载。有效 PDF 必须在前 1024 字节内包含 `%PDF-`。
- **重复标题巡检**（浏览器模式）：DOI 是唯一的，因此大量页面标题相同通常意味着错误页或封面页。
  1. 任意已加载页面（成功页及 404 页，不含过盾超时页）的标题重复达到 `title_dup_trigger` 次时，PaperLite 归纳出能解释这些重复的最具体的 DOI/URL 前缀。
  2. 用已采集的数据验证该规则，要求精确率不低于 0.98。
  3. 验证通过后，才将命中的 pending 任务标记为跳过。

  该机制宁可少跳过，也不误跳仍然有效的任务。

**参考吞吐**（ACM Digital Library，20 个 worker）：每分钟处理 120–160 个任务。每个身份约可使用 9 分钟，下载 60–70 篇。每 1 万篇约需 180–300 个短效 IP；使用 30 分钟时效的 IP 时约为其三分之一。

## 开发

```bash
uv sync
uv run pytest                                                   # 跳过依赖 Redis 的用例
PAPERLITE_TEST_REDIS=redis://127.0.0.1:6379/0 uv run pytest     # 包含 Redis 用例
PAPERLITE_TEST_AMQP=amqp://guest:guest@127.0.0.1:5672/%2F uv run pytest tests/test_monitor.py
```

项目结构：

```
src/paperlite/
  cli.py, config.py   命令行；配置（[proxy]、[limits]、[paths]、[run]）
  browser_pool.py     浏览器模式：每个 worker 复用浏览器与 IP
  browser.py          单次过盾（经代理启动独立的 CloakBrowser 进程）
  turnstile.py        Turnstile 定位、点击与 token 服务
  producer.py         curl 模式：按需生产身份，控制过盾并发
  store.py            curl 模式：Redis 身份池（Lua 原子领用、租约、生产者锁）
  consumer.py         curl 模式：下载线程、流式下载、身份健康判定
  tasks.py            SQLite 任务库、卷期停止
  inspect.py          重复标题规则归纳
  classify.py         non-PDF 分类（not_found、paywall、landing_page）
  proxy.py            代理提取与分层体检（TCP/TLS/TTFB/总耗时）
  monitor/            指标上报（未启用时为 no-op）与 paperlite-monitor 面板
  config.example.toml 配置模板
tests/                pytest 测试（unittest 风格）
docs/MONITOR.md       监控部署指南
```

`config.toml` 含代理密码，已排除在版本控制之外，并通过 `pyproject.toml` 中的显式白名单排除在 sdist 之外。

## 许可证

[MIT](LICENSE)
