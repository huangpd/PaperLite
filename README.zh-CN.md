# PaperLite

[![PyPI](https://img.shields.io/pypi/v/paperlite.svg)](https://pypi.org/project/paperlite/)
[![Python](https://img.shields.io/pypi/pyversions/paperlite.svg)](https://pypi.org/project/paperlite/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[English](README.md) | **简体中文**

PaperLite 是一个面向大批量场景的 PDF 与网页下载工具，适用于部署在 Cloudflare 等反爬防护之后的站点。它通过轮换的短效代理驱动真实浏览器，把每个任务存进 PostgreSQL 任务队列（一台或多台机器共享），中断后可从断点继续。

- **一份配置即一个批次**：任意多个主机的 URL 导入同一个任务队列，统一调度。
- **默认真实浏览器**：每个 worker 独占一个浏览器和一个 IP，过盾一次后连续下载；自动跟随跳转，保存 PDF 或完整 HTML 页面。
- **可续跑**：`Ctrl+C` 会把在途任务放回队列，再执行同一命令即从断点继续。
- **单机或多机**：连同一个 PostgreSQL 批次的机器按块领任务（带租约），机器随时加减。
- **自我保护**：代理体检、IP 中途失效自动换 IP、代理配额耗尽时熔断，以及跳过失效页或重复内容的启发式规则。
- **可观测**：每个任务的错误分类和指标都在 PostgreSQL 里，另有可选的多机监控面板。

## 安装

```bash
pip install paperlite          # 或：uv tool install paperlite
# 国内可加镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple
```

需要 Python 3.11 及以上（Linux 或 macOS）、一个 PostgreSQL，以及一个短效代理提取接口（本地试用可加 `--no-proxy` 不走代理）。

还没有 PostgreSQL？本机起一个：

```bash
docker run -d --name paperlite-pg -e POSTGRES_USER=paperlite -e POSTGRES_PASSWORD=pl \
  -e POSTGRES_DB=paperlite -p 5432:5432 postgres:16
```

## 快速开始

```bash
mkdir work && cd work
paperlite init                 # 生成 ./config.toml（权限 600）
```

填好最关键的几段：

```toml
[pg]
dsn   = "postgresql://paperlite:pl@127.0.0.1:5432/paperlite"
batch = "my-batch"             # dsn + batch 相同的机器共享同一个批次

[proxy]
api_url  = "https://proxy.example.com/extract?..."   # 每次调用返回一个新的 ip:port
username = "..."
password = "..."

[paths]
storage = "output"             # PDF / HTML 输出目录

[run]
command = "run"
workers = 20
urls    = "urls.csv"           # 一行一个 URL，或带 url 列的 CSV
```

然后启动批次：

```bash
paperlite run config.toml      # 导入、下载，队列清空后自动退出
                               # 想更快：在更多机器上执行同一条命令
paperlite status               # 各状态计数与失败分类
```

完整流程见[快速上手](docs/getting-started.zh-CN.md)。

## 文档

| 我想… | 看这里 |
|---|---|
| 第一次跑起来 | [快速上手](docs/getting-started.zh-CN.md) |
| 选浏览器模式还是 curl 模式 | [运行模式](docs/guide/modes.md) |
| 部署 PostgreSQL | [PostgreSQL](docs/guide/postgres.md) |
| 多台机器跑同一个批次 | [多机部署](docs/guide/multi-machine.md) |
| 续跑、重试、导出、后台运行 | [日常运维](docs/guide/operations.md) |
| 搭建多机监控面板 | [监控](docs/guide/monitoring.md) |
| 获取 Cloudflare Turnstile token | [Turnstile](docs/guide/turnstile.md) |
| 排查问题 | [故障排查](docs/guide/troubleshooting.md) |
| 查某个配置字段 | [配置参考](docs/reference/configuration.md) |
| 查某个命令 | [命令参考](docs/reference/cli.md) |
| 查任务库 | [任务库](docs/reference/task-database.md) |
| 了解为什么这样设计 | [架构与设计说明](docs/development/architecture.md) |
| 参与开发 | [CONTRIBUTING](CONTRIBUTING.md) · [更新日志](CHANGELOG.md) |

完整目录见 [`docs/`](docs/README.md)。

## 许可证

[MIT](LICENSE)
