# 日常运维

任务都在 `[pg]` 指向的 PostgreSQL 里，本文的命令在任一台机器上执行都行。多台机器一起跑见[多机部署](multi-machine.md)。

## 启动与停止

```bash
paperlite run config.toml                                         # 前台
setsid nohup paperlite run config.toml > run.log 2>&1 < /dev/null &   # 后台
pkill -f "paperlite run"                                          # 优雅停止（SIGTERM）
```

- `Ctrl+C` / `SIGTERM`：已有结果写回，没做完的块立刻交还，再次执行同一命令即从断点继续。
- 异常退出（`kill -9`、宕机）：这个进程领着的块等租约到期（默认 30 分钟，`[pg] lease_s`）后回到队列，由任何一个下载进程接手；已写回的结果保留。
- 同一个批次可以同时跑多个下载进程（同一台或多台机器），各领各的块。
- 给 `run` 传入配置文件时以 `[run]` 段为准，命令行上的其他 `run` 参数会被忽略。

> 通过镜像源装的源码环境，请直接调用 `.venv/bin/paperlite`。`uv run` 会按 `uv.lock` 从默认源重新同步，受限网络下可能卡住。

## 导入任务

```bash
paperlite import --urls batch.csv                 # 导入到 [pg] 的批次
paperlite import --urls pages.txt --type html     # 要网页本身而不是 PDF
```

- 文本文件一行一个 URL，保持原顺序。
- CSV 需要 `url` 列，可选 `doi`、`source`、`batch_no`、`url_type`（逐行覆盖 `--type`）。
- 入库时按 (主机, DOI) 排序、切成块（默认 1000 条，`[pg] chunk_size`）：同一出版商、同一期刊的 DOI 前缀相同，排序后连在一起，一块只打一个站点。没有 `doi` 列时从 URL 里认 DOI，认不出的用 URL 哈希。
- CSV 格式错误（如未加引号的逗号）会报出行号并中止导入。
- 重复导入不会重置已有任务，日志里的"已存在"就是这些行。

## 看进度

```bash
paperlite status
```

输出各状态计数（`running` 是本进程在途数，别的机器在做的仍计为 `pending`）、块统计（queued 待领 / leased 领取中 / done 已完成）、巡检跳过数、按 `error_kind` 分组的失败数，curl 模式下还有身份池。直接查库见[任务库](../reference/task-database.md)。

## 重试

```bash
paperlite retry --status failed                    # 所有失败任务放回 pending
paperlite retry --status failed --kind network     # 只放回网络错误
paperlite retry --status non_pdf --kind paywall    # 换了有订阅的出口后重下订阅页
```

放回的任务会切成新一代块。正在跑的下载机领完手头的块后会自动接着做；都已退出的话，再执行 `paperlite run config.toml`。

## 导出

```bash
paperlite export --file all.csv
paperlite export --status success --file done.csv
```

## 巡检（重复标题跳过）

若大量页面标题相同（通常是错误页或封面页），PaperLite 会归纳出一条 DOI/URL 前缀规则，在已采数据上验证精确率 ≥ 0.98 后，把命中的 pending 任务标记为跳过（保持 pending、不再领取）。原理见[架构与设计说明](../development/architecture.md#重复标题巡检)。

巡检有两种：

| | 什么时候 | 范围 |
|---|---|---|
| 下载时实时巡检 | 浏览器模式自动进行 | 只看本进程最近加载的页面，只跳本进程领到的块里的任务 |
| `paperlite inspect` | 手动执行，任一台机器 | 全批次已采数据归纳规则，跳过全批次命中的 pending；已经在别的机器本地队列里的任务这一轮照常下载 |

```bash
paperlite inspect --list      # 查看已生效的规则（两种巡检的都在）和跳过数
paperlite inspect             # 用全批次已采数据巡检一次
paperlite inspect --reset     # 清除所有规则，放回全部被跳过的任务
```

相关配置：`title_dup_trigger`（0 关闭）、`title_window_size`、`rule_min_precision`、`rule_min_support`。

## 新增站点

不需要按站点配置，主机直接取自任务队列里的 URL。

- 不需要配置热身页：浏览器直接拿队列里的真实链接过盾，curl 模式用真实链接验收身份。
- curl 模式还要检查 `classify.py` 里的 non-PDF 判定规则（按 ACM 编写）是否适用。

## 日志

- `[paths].log_dir` 下的日志文件始终为 JSON Lines，按启动时间命名。
- 终端输出默认 `--log-format auto`：交互式终端输出带颜色的文本（遵循 `NO_COLOR`），重定向、管道或容器采集时输出 JSON。
- 每 30 秒一行进度；每个新 IP 记一条 `proxy_check` 结构化日志，含 TCP / TLS / TTFB / 总耗时。

## 参考吞吐

ACM Digital Library，20 个浏览器 worker：

- 每分钟处理 120–160 个任务。
- 每个身份约可使用 9 分钟，下载 60–70 篇。
- 每 1 万篇约需 180–300 个短效 IP；使用 30 分钟时效的 IP 时约为其三分之一。
