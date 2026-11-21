# 快速上手

[English](getting-started.md) | **简体中文**

本文从安装一直走到跑完一个批次，以默认的浏览器模式为例。curl 模式见[运行模式](guide/modes.md)。

## 1. 准备

- Python 3.11 及以上，Linux 或 macOS。
- 一个 PostgreSQL：存任务队列，一台就能服务任意多个批次和机器。
- 一个短效代理提取接口：每次调用返回一个新的 `ip:port`（JSON 或纯文本均可），以及代理账号密码。没有代理时可以加 `--no-proxy`（仅浏览器模式）试用，但多数有防护的站点会直接挑战本机 IP。

## 2. 安装

```bash
pip install paperlite
# 或
uv tool install paperlite
```

国内可加镜像源，例如 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。装好后用 `paperlite --version` 确认。

## 3. 启动 PostgreSQL

本机试用：

```bash
docker run -d --name paperlite-pg -e POSTGRES_USER=paperlite -e POSTGRES_PASSWORD=pl \
  -e POSTGRES_DB=paperlite -p 5432:5432 postgres:16
```

服务器上用 [`deploy/pg/docker-compose.yml`](../deploy/pg/docker-compose.yml)，见[部署 PostgreSQL](guide/postgres.md)。表在第一次导入时自动建好。

## 4. 生成配置文件

```bash
mkdir work && cd work
paperlite init                 # 生成 ./config.toml，权限 600
```

`config.toml` 里有数据库和代理密码，不要提交到 git，也不要外传。

改下面几段，其他字段都有可用的默认值，需要时查[配置参考](reference/configuration.md)：

```toml
[pg]
dsn   = "postgresql://paperlite:pl@127.0.0.1:5432/paperlite"
batch = "my-batch"             # dsn + batch 相同的机器共享同一个批次

[proxy]
api_url  = "https://proxy.example.com/extract?..."
username = "..."
password = "..."

[paths]
storage = "/mnt/obs/papers"    # PDF 与网页：本地盘或挂载都行
log_dir = "log"                # JSON Lines 日志；留空只输出到终端

[run]
command  = "run"               # 不带子命令执行 paperlite 时也按此启动
mode     = "browser"           # browser | curl
workers  = 20                  # 并发浏览器数
urls     = "urls.csv"          # 启动时先导入
url_type = "pdf"               # pdf = 期望拿到 PDF；html = 保存网页本身
```

## 5. 准备 URL 列表

可以是一行一个 URL 的文本文件，也可以是带 `url` 列的 CSV。CSV 可选列：`doi`、`source`、`batch_no`、`url_type`。

```csv
doi,url
10.1145/3779295,https://dl.acm.org/doi/pdf/10.1145/3779295
10.1016/j.cell.2024.01.001,https://www.sciencedirect.com/science/article/pii/S0092867424000011
```

不同主机的 URL 可以混在一起。导入时按 (主机, DOI) 排序、切成约 1000 条一块，同一期刊的论文挨在一起下载。同一文件重复导入不会重置已有任务。

## 6. 运行

```bash
paperlite run config.toml
```

PaperLite 会导入 URL 文件、下载全部任务，队列清空后自动退出，每 30 秒输出一行进度。

- **停止**：`Ctrl+C` 或 `SIGTERM`，结果写回、没做完的块交还。
- **续跑**：再执行同一命令。
- **加速**：在更多机器上用同一份配置执行同一命令，见[多机部署](guide/multi-machine.md)。
- **后台运行**：`setsid nohup paperlite run config.toml > run.log 2>&1 < /dev/null &`

文件保存为 `<storage>/<h[0:2]>/<h[2:4]>/<doi>.pdf`（网页为 `.html`），其中 `h = sha256(doi)`。

## 7. 看结果

```bash
paperlite status                                  # 各状态计数、失败分类、块进度
paperlite export --status failed --file failed.csv
paperlite retry --status failed --kind network    # 把网络错误的任务放回队列
```

`non_pdf` 是正常结果，不是错误：页面加载出来了，但没有 PDF（`not_found`、`paywall`、`landing_page`）。全部状态和错误分类见[任务库](reference/task-database.md)。

## 下一步

- [日常运维](guide/operations.md)：重试、巡检规则、新增站点、日志。
- [运行模式](guide/modes.md)：什么时候值得为 curl 模式多搭一个 Redis。
- [监控](guide/monitoring.md)：多台机器一个面板。
- [故障排查](guide/troubleshooting.md)。
