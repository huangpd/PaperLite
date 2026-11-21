# 故障排查

先看 `paperlite status` 的失败分类，再对照下表。`error_kind` 的完整含义见[任务库](../reference/task-database.md#error_kind)。

## 安装与启动

| 现象 | 原因与处理 |
|---|---|
| `uv run paperlite` 卡住不动 | `uv run` 会按 `uv.lock` 从默认源重新同步依赖。受限网络下直接调用 `.venv/bin/paperlite` |
| `uv pip install` 不走国内镜像 | uv 不读 `pip.conf`，要用 `--index-url https://pypi.tuna.tsinghua.edu.cn/simple` 指定 |
| 首次启动卡在下载 GeoIP 库 | `geoip = true` 需要约 70MB 的 GeoLite2 库。连不上时按报错提示手动放到 `~/.cloakbrowser/geoip/GeoLite2-City.mmdb`，或设 `geoip = false`（WebRTC 伪造仍由 `webrtc_spoof` 保留） |
| 启动报任务库被占用 | 同一个任务库同时只允许一个下载进程。检查是否有残留的 `paperlite run` |
| `database is locked` / 随机 I/O 错误 | `[paths].db` 放在了 goofys、s3fs 等 FUSE 挂载上。任务库必须放本地盘 |
| 配置项报"未知字段" | 字段名写错或放错了段，对照[配置参考](../reference/configuration.md) |

## 导入

| 现象 | 原因与处理 |
|---|---|
| 报"不是有效链接: …"并中止 | 这一行的 `url` 列没有主机名（空值、表头错位、未加引号的逗号把列挤歪）。按报出的内容在 CSV 里找到并修正或删除 |
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
| 某个卷期整批 `non_pdf` 且 `inferred=1` | curl 模式的卷期停止推断出来的，没有实际请求。怀疑误判时 `retry --stopped-volumes` |
| 很多 pending 不再被领取 | 巡检规则把它们标记为跳过了。`inspect --list` 查看，`inspect --reset` 放回 |
| 大 PDF 下载失败 `extract_failed` | 超过 `max_file_size_mb`，或文件太小、前 1024 字节里没有 `%PDF-` |

## curl 模式

| 现象 | 原因与处理 |
|---|---|
| 下载线程一直等身份 | 生产者没在跑，或 Redis 地址不一致。`paperlite status` 看身份池；单独部署时要有一个 `paperlite produce` |
| 身份入池后马上被拒 | 浏览器能过盾不代表 curl 能下。把 `[landing]` 配成真实文章页，让验收更接近实际下载 |
| 升级后 cookie 异常 | 生产者和下载线程要同时重启 |

还没解决时，把 `log_dir` 下对应时间段的 JSON 日志和 `paperlite status` 输出一起提 issue。
