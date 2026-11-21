# 命令参考

每个命令的完整参数都可以用 `paperlite help <命令>` 或 `paperlite <命令> -h` 查看。

## 全局参数

```
paperlite [--config FILE] [--log-level LEVEL] [--log-format auto|text|json] [-V] <命令> ...
```

| 参数 | 说明 |
|---|---|
| `--config` | 配置文件；默认取环境变量 `PAPERLITE_CONFIG`，再取当前目录的 `config.toml` |
| `--log-level` | 日志级别，默认 `INFO` |
| `--log-format` | 终端日志格式：`auto`（默认，交互式终端输出文本，重定向输出 JSON）、`text`、`json`。日志文件始终为 JSON |
| `-V`, `--version` | 显示版本 |

多数命令都接受 `--db DB`，用于覆盖 `[paths].db`。

## 命令一览

| 命令 | 说明 |
|---|---|
| [`init`](#init) | 生成配置模板 |
| [`import`](#import) | 导入任务 |
| [`run`](#run) | 下载直至队列清空 |
| [`produce`](#produce--download) | curl 模式：只生产身份 |
| [`download`](#produce--download) | curl 模式：只下载 |
| [`status`](#status) | 任务统计 |
| [`retry`](#retry) | 把失败任务放回队列 |
| [`export`](#export) | 导出为 CSV |
| [`inspect`](#inspect) | 重复标题巡检 |
| [`turnstile`](#turnstile) | 获取 Turnstile token |
| [`paperlite-monitor`](#paperlite-monitor) | 监控面板 |

## `init`

```
paperlite init [--force]
```

在当前目录生成 `config.toml`（权限 600）。已存在时需加 `--force` 覆盖。

## `import`

```
paperlite import --urls FILE [--type pdf|html] [--source S] [--batch B] [--db DB]
```

| 参数 | 说明 |
|---|---|
| `--urls` | 一行一个 URL 的文本文件，或带 `url` 列的 CSV（可选 `doi`、`source`、`batch_no`、`url_type` 列） |
| `--type` | `pdf`（默认，期望拿到 PDF）或 `html`（整页保存为 `.html`）；CSV 的 `url_type` 列逐行覆盖 |
| `--source`、`--batch` | 写入 `source`、`batch_no` 列 |

CSV 先用 DuckDB 按 DOI 排序再入库，文本文件保持原顺序。重复导入不会重置已有任务。详见[日常运维](../guide/operations.md#导入任务)。

## `run`

```
paperlite run [CONFIG]
paperlite run [--urls FILE] [--type pdf|html] [--workers N] [--browser | --curl]
              [--no-proxy] [--headed] [--db DB] [--storage DIR]
```

| 参数 | 说明 |
|---|---|
| `CONFIG` | 给了配置文件就按它的 `[run]` 段启动，其余 `run` 参数被忽略 |
| `--urls`、`--type` | 启动前先导入 |
| `--workers` | 浏览器模式为并发浏览器数，curl 模式为下载线程数；默认 20 |
| `--browser` / `--curl` | 运行模式，默认浏览器模式 |
| `--no-proxy` | 仅浏览器模式：直连本机 IP，不取代理 |
| `--headed` | 仅浏览器模式：显示浏览器窗口 |
| `--storage` | 覆盖 `[paths].storage` |

队列清空后退出。`Ctrl+C` / `SIGTERM` 优雅退出，在途任务回到 `pending`。

## `produce` / `download`

```
paperlite produce [--db DB]
paperlite download [--workers N] [--urls FILE] [--type pdf|html] [--db DB] [--storage DIR]
```

curl 模式把生产者和下载线程拆成两个进程时使用。`produce` 常驻，只在有下载线程在线、且还有可领取任务时才过盾；`download` 需要另有生产者在运行。见[运行模式](../guide/modes.md#curl-模式)。

## `status`

```
paperlite status [--db DB]
```

各状态计数、按 `error_kind` 分组的失败数、已停的卷期；curl 模式下还显示身份池。

## `retry`

```
paperlite retry --status failed|non_pdf [--kind KIND] [--db DB]
paperlite retry --stopped-volumes [--db DB]
```

| 参数 | 说明 |
|---|---|
| `--status` | 把该状态的任务重置为 `pending` |
| `--kind` | 配合 `--status`，只重置该 `error_kind`（取值见[任务库](task-database.md#error_kind)） |
| `--stopped-volumes` | 被卷期停止的卷期，其 `non_pdf`（抽样的和推断的）全部放回 `pending`，重新核实 |

## `export`

```
paperlite export [--status pending|running|success|failed|non_pdf] [--file out.csv] [--db DB]
```

把任务表导出为 CSV，默认输出到标准输出。

## `inspect`

```
paperlite inspect [--list] [--reset] [--trigger N] [--min-precision P] [--min-support N] [--db DB]
```

| 参数 | 说明 |
|---|---|
| （无） | 用已采数据重新巡检，归纳并应用规则 |
| `--list` | 只列出已生效的规则 |
| `--reset` | 清除所有规则和标记，被跳过的任务重新可领 |
| `--trigger`、`--min-precision`、`--min-support` | 覆盖配置中的 `title_dup_trigger`、`rule_min_precision`、`rule_min_support` |

## `turnstile`

```
paperlite turnstile --url URL --sitekey KEY [--action A] [--cdata C] [--proxy P]
                    [--timeout S] [--json] [--headed]
paperlite turnstile --serve PORT [--bind ADDR] [--concurrency N]
```

见 [Turnstile](../guide/turnstile.md)。

## `paperlite-monitor`

```
paperlite-monitor [--config FILE] [--log-level LEVEL] <serve|test|demo>
```

| 子命令 | 说明 |
|---|---|
| `serve` | 消费 RabbitMQ、写入面板 SQLite，并提供网页面板 |
| `test` | 在终端机器上发一条测试心跳，验证到 RabbitMQ 的连通性 |
| `demo` | 用模拟数据启动面板，不需要 RabbitMQ |

读取配置文件的 `[monitor]` 段，查找顺序与 `paperlite` 相同。见[监控](../guide/monitoring.md)。
