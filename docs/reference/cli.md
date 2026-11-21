# 命令参考

每个命令的完整参数都可以用 `paperlite help <命令>` 或 `paperlite <命令> -h` 查看。所有操作任务的命令都作用于配置文件 `[pg]` 指定的批次（`dsn` + `batch`），在任一台机器上执行都一样。

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
paperlite import --urls FILE [--type pdf|html] [--source S] [--batch B]
```

| 参数 | 说明 |
|---|---|
| `--urls` | 一行一个 URL 的文本文件，或带 `url` 列的 CSV（可选 `doi`、`source`、`batch_no`、`url_type` 列） |
| `--type` | `pdf`（期望拿到 PDF）或 `html`（整页保存为 `.html`）。不写时取配置文件 `[run] url_type`，没有则 `pdf`；CSV 的 `url_type` 列逐行覆盖 |
| `--source`、`--batch` | 写入 `source`、`batch_no` 列（`batch_no` 只是标注，批次由 `[pg] batch` 决定） |

入库时按 (主机, DOI) 排序并切块。重复导入不会重置已有任务。详见[日常运维](../guide/operations.md#导入任务)。

## `run`

```
paperlite run [CONFIG]
paperlite run [--urls FILE] [--type pdf|html] [--workers N] [--browser | --curl]
              [--no-proxy] [--headed] [--storage DIR]
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

领不到新块、手里的块也做完后退出。`Ctrl+C` / `SIGTERM` 优雅退出：结果写回、没做完的块交还。多台机器各执行一次即可共同完成同一个批次。

## `produce` / `download`

```
paperlite produce
paperlite download [--workers N] [--urls FILE] [--type pdf|html] [--storage DIR]
```

curl 模式把生产者和下载线程拆成两个进程时使用。`produce` 常驻，只在有下载线程在线、且还有可领取任务时才过盾；`download` 需要另有生产者在运行。见[运行模式](../guide/modes.md#curl-模式)。

## `status`

```
paperlite status
```

各状态计数、块统计（queued / leased / done）、巡检跳过数、按 `error_kind` 分组的失败数；curl 模式下还显示身份池。`running` 只算本进程的在途任务，别的机器正在做的仍计为 `pending`。

## `retry`

```
paperlite retry --status failed|non_pdf [--kind KIND]
```

| 参数 | 说明 |
|---|---|
| `--status` | 把该状态的任务放回 `pending`，清零重试与挑战次数 |
| `--kind` | 只放回该 `error_kind`（取值见[任务库](task-database.md#error_kind)） |

放回的任务切成新一代块，下载机领完手头的块后自动接着做。

## `export`

```
paperlite export [--status pending|success|failed|non_pdf] [--file out.csv]
```

把批次的任务导出为 CSV，默认输出到标准输出；几百万行也是流式写出，不占满内存。列见[任务库](task-database.md#导出列)。

## `inspect`

```
paperlite inspect [--list] [--reset] [--trigger N] [--min-precision P] [--min-support N]
```

| 参数 | 说明 |
|---|---|
| （无） | 用全批次已采数据巡检一次，跳过命中规则的 pending 任务 |
| `--list` | 列出已生效的规则（全批次巡检和下载时块内巡检的都在）和跳过数 |
| `--reset` | 清除全部规则，放回所有被跳过的任务 |
| `--trigger`、`--min-precision`、`--min-support` | 覆盖配置中的 `title_dup_trigger`、`rule_min_precision`、`rule_min_support` |

## `turnstile`

```
paperlite turnstile --url URL --sitekey KEY [--action A] [--cdata C] [--proxy P]
                    [--timeout S] [--json] [--headed]
paperlite turnstile --serve PORT [--bind ADDR] [--concurrency N]
```

不需要 `[pg]`。见 [Turnstile](../guide/turnstile.md)。

## `paperlite-monitor`

```
paperlite-monitor [--config FILE] [--log-level LEVEL] <serve|test|demo>
```

| 子命令 | 说明 |
|---|---|
| `serve` | 从 `events` 表取走监控事件、汇总入库，并提供网页面板。可加 `--dsn`、`--schema`、`--bind`、`--port`、`--no-consume` |
| `test` | 在下载机上写一条测试心跳，验证能写进监控库、面板是否在消费。可加 `--dsn`、`--schema` |
| `demo` | 用模拟数据启动面板（写入 schema `paperlite_monitor_demo`） |

读取配置文件的 `[monitor]` 段，查找顺序与 `paperlite` 相同；`[monitor] dsn` 留空时用 `[pg] dsn`。见[监控](../guide/monitoring.md)。
