# 故障排查

先看 `paperlite status` 的失败分类，再对照下表。`error_kind` 的完整含义见[任务库](../reference/task-database.md#error_kind)。

## 安装与启动

| 现象 | 原因与处理 |
|---|---|
| `uv run paperlite` 卡住不动 | `uv run` 会按 `uv.lock` 从默认源重新同步依赖。受限网络下直接调用 `.venv/bin/paperlite` |
| `uv pip install` 不走国内镜像 | uv 不读 `pip.conf`，要用 `--index-url https://pypi.tuna.tsinghua.edu.cn/simple` 指定 |
| 首次启动卡在下载 GeoIP 库 | `geoip = true` 需要约 70MB 的 GeoLite2 库。连不上时按报错提示手动放到 `~/.cloakbrowser/geoip/GeoLite2-City.mmdb`，或设 `geoip = false`（WebRTC 伪造仍由 `webrtc_spoof` 保留） |
| 配置项报"未知字段" | 字段名写错或放错了段，对照[配置参考](../reference/configuration.md) |
| 报 `[paths] db` / `[queue]` / `[run] pg` / `volume_stop_after` 已移除 | 旧版配置。任务队列只用 PostgreSQL：删掉这些字段，填好 `[pg]` 的 `dsn` 和 `batch` |

## 导入

| 现象 | 原因与处理 |
|---|---|
| 日志"剔除明显下载不到的链接 N 条" | 内网 IP、主机名不合法等，没入库，明细在 `<文件名>.bad_url.csv`。这些大多是出版商在 DOI 系统里登记错的地址，换成 doi.org 链接也会跳到同一个坏地址。数量异常多时检查 CSV 是否表头错位、列被未加引号的逗号挤歪 |
| 报 CSV 第 N 行格式错误并中止 | 未加引号的逗号、引号不配对等。按行号修正 |
| 日志里"已存在 N" | 这些 URL 已经在库里，或在同一文件里重复出现（规范化后相同），不会被重置。要重下用 `retry` |

## 下载

| 现象 | 原因与处理 |
|---|---|
| 大量 `challenge_failed` | 代理 IP 质量差（机房 IP 常要求人工勾选），或站点对这批 URL 加强了防护。换代理厂商，或调小 `workers` |
| 大量 `network` | 短效 IP 在租期内断开。属 IP 问题，`retry --status failed --kind network` 后重跑即可 |
| 大量 `rate_limited`（HTTP 429） | 站点限流。调大 `page_interval_s`（浏览器模式）或 `request_gap_s`（curl 模式），减少 `workers` |
| 日志频繁出现"IP 被站点封禁"（`blocked`） | 站点封了出口 IP 段。身份会被丢弃、任务放回队列，但持续出现说明这批 IP 不可用，换代理厂商或地区 |
| 生产者"熔断"告警 | 连续多个新身份一用就被拒，通常是代理配额用完或代理整体被封。暂停 `breaker_pause_s` 后自动试探 |
| 浏览器过盾一直停在 "Verifying…" | 过盾要么很快、要么卡死。超过 `hedge_after_s` 会自动补位一个浏览器，超过 `pass_timeout_s` 放弃这个 IP |
| `non_pdf` 很多 | 先抽几条看 `error_kind`：`not_found` 是 DOI 不存在，`paywall` 是出口没有订阅，`landing_page` 是只有摘要页。不是程序错误 |
| 很多 pending 不再被领取 | 巡检规则把它们标记为跳过了（`status` 里的"巡检跳过"）。`inspect --list` 查看，`inspect --reset` 放回 |
| 大 PDF 下载失败 `extract_failed` | 超过 `max_file_size_mb`，或文件太小、前 1024 字节里没有 `%PDF-` |

## curl 模式

| 现象 | 原因与处理 |
|---|---|
| 下载线程一直等身份 | 生产者没在跑，或 Redis 地址不一致。`paperlite status` 看身份池；单独部署时要有一个 `paperlite produce` |
| 身份入池后马上被拒 | 浏览器能过盾不代表 curl 能下。验收用的是最近成功的真实链接，批次刚开始还没有成功链接时用的是队列里的待下载链接或首页，可能不够准；跑出第一批成功后会好转 |
| 浏览器模式不停换 IP（日志"新浏览器首篇失败，换 IP"） | 新 IP 的第一篇都打不开：先看是不是代理质量差；同一站点连续 3 次后会自动改为按站点问题计次，不会无限换 |
| 报 `[landing] 已移除` | 旧配置。现在不用固定落地页热身，删掉 `[landing]` 段即可 |
| 升级后 cookie 异常 | 生产者和下载线程要同时重启 |

## PostgreSQL 与任务队列

| 现象 | 原因与处理 |
|---|---|
| 报"连接 PostgreSQL 失败" | 检查 `[pg] dsn`、网络、安全组与 `pg_hba.conf`；`dsn` 必须指向主库。见[部署 PostgreSQL](postgres.md) |
| 报"没有 [pg] 段" | 配置文件缺 `[pg]` 的 `dsn` 和 `batch`；`paperlite init` 生成的模板里有 |
| `status` 显示的任务数不对 | `batch` 写错了：不同 `batch` 是不同批次，互不可见 |
| 报 too many connections | 每个下载进程占 1 条连接，加上面板 1 条。调大 `max_connections`，或加 pgbouncer（transaction 模式） |
| 机器 `kill -9` 后它的块没人接 | 等租约过期（默认 `lease_s = 1800` 秒）后自动回收；正常退出（Ctrl+C / SIGTERM）会立刻交还 |
| 同一块反复被领取（`attempts` 很高） | 可能是毒块：块里某条任务每次都让进程崩溃。查出后手动处理，见[多机部署](multi-machine.md#8-当前限制) |
| 下载机启动后很快退出 | 当前批次已没有可领的块。`paperlite status` 看是否还有 `queued`，或重试任务还没到 `retry_delay_s` |

还没解决时，把 `log_dir` 下对应时间段的 JSON 日志和 `paperlite status` 输出一起提 issue。
