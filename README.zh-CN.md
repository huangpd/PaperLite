# PaperLite

[![PyPI](https://img.shields.io/pypi/v/paperlite.svg)](https://pypi.org/project/paperlite/)
[![Python](https://img.shields.io/pypi/pyversions/paperlite.svg)](https://pypi.org/project/paperlite/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[English](README.md) | **简体中文**

**PaperLite** 是一个为千万级论文采集打造的高并发、分布式文献与网页抓取集群。它以指纹浏览器集群配合轮换短效代理，原生穿透 Cloudflare 等高强度反爬防护；任务调度、身份池与监控数据全部收敛到一个 PostgreSQL——无需 Redis、消息队列或任何额外中间件，一台机器与一百台机器是同一套架构。

## 🚀 核心特性

- **指纹浏览器集群，原生过盾**：基于 CloakBrowser 指纹 Chromium + Playwright，每个 worker 独占一个浏览器与出口 IP，时区、语言、WebRTC 与出口 IP 自动对齐；过盾一次后连续下载，自动跟随跳转。
- **PDF 流式截获**：通过 CDP 在网络层直接截获 PDF 响应并流式落盘，不渲染、不点击；HTML 页面完整保存。
- **分布式任务调度**：任务按块分发、租约保护，机器随时加入或退出，宕机节点的任务自动回收、由其他节点接手；单批次可承载千万级任务。
- **零中间件架构**：任务队列、身份池、监控遥测全部运行在 PostgreSQL 上（`SKIP LOCKED` 领取 + advisory lock 协调），部署只需要一个数据库。
- **双引擎**：默认浏览器模式适合多站点混跑；curl 模式由浏览器过盾生成 Cookie 身份池，`curl_cffi` 以 TLS 指纹仿真高速批量下载单站点。
- **智能自愈**：代理四段测速（TCP / TLS / 首字节 / 总耗时）并核验出口 IP；IP 中途失效自动轮换；新身份接连被拒时熔断止损，避免烧光代理配额；基于标题重复归纳规则，自动跳过失效页与重复内容。
- **断点续跑**：`Ctrl+C` 即时交还在途任务，重启从断点继续；结果至少写入一次、重复写入无害。
- **全链路可观测**：每个任务的错误分类与耗时指标落库，配套多机实时监控大屏（成功率、代理质量、WAF 分布、浏览器状态）。

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
