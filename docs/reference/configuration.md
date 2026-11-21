# 配置参考

`paperlite init` 生成的模板 [`config.example.toml`](../../src/paperlite/config.example.toml) 带有每个字段的注释，本页按段整理成表。下面的"默认值"指配置里不写该字段时程序使用的值；模板里个别字段填的是推荐值，与默认值不同时会注明。

## 查找顺序

1. 命令行 `--config FILE`
2. 环境变量 `PAPERLITE_CONFIG`
3. 当前目录的 `config.toml`

写了不认识的段或字段会直接报错，防止拼写错误被静默忽略。

命令行覆盖：`--db` 优先于 `[paths].db`，`--storage`（`run`、`download` 可用）优先于 `[paths].storage`。给 `run` 传入配置文件时以 `[run]` 段为准，命令行上的其他 `run` 参数会被忽略。

**`config.toml` 含代理密码，不要提交到 git，也不要外传。**

## `[proxy]` 代理

| 字段 | 默认值 | 说明 |
|---|---|---|
| `api_url` | （必填） | 短效代理提取接口，每次调用返回一个 IP（JSON 或纯文本） |
| `scheme` | `http` | 代理协议 |
| `username`、`password` | 空 | 代理鉴权 |
| `lease_s` | 600 | 单个 IP 最长使用秒数，与接口返回的 deadline 取较早者 |
| `check_url` | Cloudflare `cdn-cgi/trace` | 新 IP 体检的首选目标，要能回显出口 IP |
| `check_url2` | `https://httpbin.org/ip` | 备用体检目标；两者任一通过即合格 |
| `connect_timeout_s` | 3 | 先 TCP 连代理端口，连不上快速放弃 |
| `check_timeout_s` | 10 | 单次体检 HTTP 请求超时 |
| `check_max_latency_ms` | 3000 | 体检往返延迟上限；0 关闭 |
| `verify_exit_ip` | true | 要求回显的出口 IP 等于代理 IP（证明真转发） |
| `check_max_tries` | 3 | 体检重试次数 |

体检只验连通性、延迟和出口 IP。`check_url` 应保持为回显出口 IP 的地址，不要改成目标站：能否过目标站的防护由过盾环节判断。

## `[paths]` 位置

| 字段 | 默认值 | 说明 |
|---|---|---|
| `db` | `tasks.db` | SQLite 任务库。**必须本地盘**（随机写、文件锁、mmap），goofys / s3fs 等 FUSE 挂载不支持 |
| `storage` | `output` | PDF / HTML 输出目录。只有顺序写，可放对象存储挂载 |
| `log_dir` | `log` | 日志目录；非空时同时输出到终端并写 `.jsonl`，留空只输出到终端 |

`db` 与 `storage` 相互独立，不互相推导。

## `[run]` 启动参数

`paperlite run config.toml` 时使用。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `command` | 空 | 填 `"run"` 时，不带子命令执行 `paperlite` 也按此启动；留空则只打印帮助 |
| `mode` | `browser` | `browser`（多主机混跑）或 `curl`（单主机 cookie 池） |
| `proxy` | true | false 等同于 `--no-proxy`，仅浏览器模式 |
| `workers` | 20 | 浏览器模式为并发浏览器数，curl 模式为下载线程数 |
| `urls` | 空 | 启动时先导入的 URL 文件 |
| `url_type` | `pdf` | `pdf`（期望拿到 PDF）或 `html`（保存网页本身）；CSV 的 `url_type` 列逐行覆盖 |
| `headed` | false | 仅浏览器模式：显示浏览器窗口（调试） |

## `[limits]` 并发与行为

### 浏览器与过盾

| 字段 | 默认值 | 说明 |
|---|---|---|
| `max_browsers` | 20 | 同时运行的浏览器上限 |
| `pass_timeout_s` | 90 | 单次过盾最长等待 |
| `hedge_after_s` | 45 | 过盾超过该时长仍未通过时，另开一个浏览器补位（成功的过盾大多在 30 秒内） |
| `page_interval_s` | [10, 30] | 浏览器模式：同一浏览器两个页面之间的随机间隔（秒） |
| `webrtc_spoof` | true | 把 WebRTC 出口伪造成代理出口 IP，防止泄露真实 IP |
| `geoip` | true | 按出口 IP 对齐浏览器时区和语言。需要约 70MB 的 GeoIP 库，首次启动时下载 |
| `debug_dir` | 空 | 过盾失败时截图的目录；空则不截图 |
| `headed` | false | 显示浏览器窗口 |

### 下载

| 字段 | 默认值 | 说明 |
|---|---|---|
| `request_gap_s` | [3, 5]（模板为 [10, 30]） | curl 模式：同一 IP 两次请求之间的随机间隔（秒） |
| `connect_timeout_s` | 15 | 下载连接超时 |
| `download_timeout_s` | 120 | 流式下载的卡顿超时：连续这么久收不到数据才算超时，不限总时长 |
| `min_file_size` | 1024 | PDF 小于该字节数视为不完整，计一次重试 |
| `max_file_size_mb` | 200 | 单个文件上限，超过即中止，记 `failed / extract_failed` |
| `max_tries` | 3 | 网络错误的重试次数（cookie 被拒不计） |
| `max_url_challenges` | 3 | 单条 URL 带有效 cookie 仍被挑战满这么多次，记 `failed / challenge_failed` |

### curl 模式身份池

| 字段 | 默认值 | 说明 |
|---|---|---|
| `redis_url` | `redis://127.0.0.1:6379/0` | 身份池所在的 Redis；浏览器模式不用 |
| `key_prefix` | `paperlite` | Redis key 前缀 |
| `max_identities` | 20 | 健康身份上限；实际目标 = min(本值, 在线下载线程数, 可领取任务数) |
| `refill_before_s` | 90 | 身份剩余寿命低于此值即不算健康，提前生产替补 |
| `min_left_s` | 20 | 身份剩余寿命低于此值不再发请求 |
| `lease_ttl_s` | 120 | 下载线程占用身份的心跳 TTL；进程崩溃后到期自动归还 |
| `max_fresh_rejects` | 5 | 连续这么多个新身份一用就被拒即熔断，暂停生产 |
| `breaker_pause_s` | 600 | 熔断暂停秒数，之后放一个身份试探 |
| `min_identities` | 0 | **已废弃**，不再生效；非 0 时打警告 |

### 跳过规则

| 字段 | 默认值 | 说明 |
|---|---|---|
| `volume_stop_after` | 3 | curl 模式卷期停止：同一卷期还没下到 PDF 时最多放出这么多篇，全部无 PDF 即停掉该卷期其余任务；0 关闭 |
| `title_dup_trigger` | 10 | 浏览器模式巡检：同一标题重复到该次数时归纳规则；0 关闭 |
| `title_window_size` | 500 | 巡检只看本次运行最近这么多条已加载页面；不能小于 `title_dup_trigger` |
| `rule_min_precision` | 0.98 | 规则在已采数据上的最低精确率 |
| `rule_min_support` | 10 | 规则在已采数据上的最低命中数 |

两者的原理见[架构与设计说明](../development/architecture.md)。

## `[landing]` 过盾落地页

可选。主机名 → 用来热身过盾的页面，未配置的主机用 `https://{host}/`。

```toml
[landing]
"dl.acm.org" = "https://dl.acm.org/doi/10.1145/3779295"
```

- 浏览器模式：每个新浏览器在第一个导入的主机的落地页上热身。
- curl 模式：生产者用落地页验收身份；遇到挑战时也用它复验身份。建议配成有代表性的真实文章页。

## `[monitor]` 监控

可选，未启用时完全不影响下载。部署方法见[监控](../guide/monitoring.md)。

**终端（`paperlite run`）用：**

| 字段 | 默认值 | 说明 |
|---|---|---|
| `enabled` | false | true 才上报 |
| `amqp_url` | `amqp://guest:guest@127.0.0.1:5672/%2F` | RabbitMQ 地址（含密码） |
| `exchange` | `paperlite.monitor` | topic exchange |
| `node` | 主机名 | 终端名 |
| `vendor` | 空（记为 unknown） | 代理厂商标签，面板按"厂商 × 域名"统计 |
| `heartbeat_s` | 15 | 心跳间隔 |
| `buffer_size` | 10000 | 本地发送缓冲；RabbitMQ 不通时超出就丢最旧的 |
| `forward_logs` | true | 把 WARNING 及以上日志也作为实时事件上报 |

**面板（`paperlite-monitor serve`）用：**

| 字段 | 默认值 | 说明 |
|---|---|---|
| `queue` | `paperlite.monitor.dashboard` | 面板的持久队列 |
| `queue_max_length` | 2000000 | 队列上限，超出丢最旧的 |
| `db` | `monitor.db` | 面板 SQLite（本地盘） |
| `bind`、`port` | `127.0.0.1`、8700 | 监听地址 |
| `username`、`password` | `admin`、空 | 网页登录；监听非本机地址时必须设密码或 token |
| `token` | 空 | 给脚本调 `/api/summary` 用的只读令牌 |
| `retention_days` | 30 | 小时汇总保留天数 |
| `raw_retention_hours` | 48 | 原始事件保留小时数 |
