# PaperLite

[![PyPI](https://img.shields.io/pypi/v/paperlite.svg)](https://pypi.org/project/paperlite/)
[![Python](https://img.shields.io/pypi/pyversions/paperlite.svg)](https://pypi.org/project/paperlite/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/huangpd/PaperLite/blob/main/LICENSE)

**English** | [简体中文](https://github.com/huangpd/PaperLite/blob/main/README.zh-CN.md)

PaperLite is a high-volume downloader for PDFs and web pages served behind bot-protection layers such as Cloudflare. It drives real browsers through rotating short-lived proxies, keeps every task in a PostgreSQL queue shared by one or many machines, and resumes cleanly after interruption.

- **One config file, one batch.** URLs from any number of hosts go into one queue and run together.
- **Real browsers by default.** Each worker owns one browser and one IP, passes the challenge once, then downloads continuously, following redirects and saving PDFs or full HTML pages.
- **Resumable.** `Ctrl+C` returns in-flight tasks to the queue; running the same command again picks up where it stopped.
- **One machine or many.** Every machine pointing at the same PostgreSQL batch takes chunks of work with a lease; add or remove machines at any time.
- **Self-protecting.** Proxy health checks, IP rotation on mid-lease failure, a circuit breaker for exhausted proxy quotas, and heuristics that skip dead or duplicate content.
- **Observable.** Per-task error classes and metrics in PostgreSQL, plus an optional fleet dashboard.

## Installation

```bash
pip install paperlite          # or: uv tool install paperlite
```

Requires Python 3.11+ on Linux or macOS, a PostgreSQL server, and a short-lived proxy extraction API (use `--no-proxy` to try it locally without one).

No PostgreSQL yet? Start one locally:

```bash
docker run -d --name paperlite-pg -e POSTGRES_USER=paperlite -e POSTGRES_PASSWORD=pl \
  -e POSTGRES_DB=paperlite -p 5432:5432 postgres:16
```

## Quick start

```bash
mkdir work && cd work
paperlite init                 # writes ./config.toml (mode 600)
```

Fill in the sections that matter:

```toml
[pg]
dsn   = "postgresql://paperlite:pl@127.0.0.1:5432/paperlite"
batch = "my-batch"             # machines with the same dsn + batch share the work

[proxy]
api_url  = "https://proxy.example.com/extract?..."   # returns a fresh ip:port per call
username = "..."
password = "..."

[paths]
storage = "output"             # where PDFs / HTML pages go

[run]
command = "run"
workers = 20
urls    = "urls.csv"           # one URL per line, or a CSV with a `url` column
```

Then run the batch:

```bash
paperlite run config.toml      # imports, downloads, exits when the queue is empty
                               # run the same command on more machines to go faster
paperlite status               # counts by status and error class
```

See the [Getting started guide](https://github.com/huangpd/PaperLite/blob/main/docs/getting-started.md) for a full walkthrough.

## Documentation

> The guides under `docs/` are written in Chinese; the README and Getting started are bilingual.

| I want to… | Read |
|---|---|
| Run my first batch | [Getting started](https://github.com/huangpd/PaperLite/blob/main/docs/getting-started.md) |
| Choose between browser and curl mode | [Execution modes](https://github.com/huangpd/PaperLite/blob/main/docs/guide/modes.md) |
| Deploy PostgreSQL | [PostgreSQL](https://github.com/huangpd/PaperLite/blob/main/docs/guide/postgres.md) |
| Run one batch on many machines | [Multi-machine deployment](https://github.com/huangpd/PaperLite/blob/main/docs/guide/multi-machine.md) |
| Resume, retry, export, run in the background | [Operations](https://github.com/huangpd/PaperLite/blob/main/docs/guide/operations.md) |
| Set up the fleet dashboard | [Monitoring](https://github.com/huangpd/PaperLite/blob/main/docs/guide/monitoring.md) |
| Get Cloudflare Turnstile tokens | [Turnstile](https://github.com/huangpd/PaperLite/blob/main/docs/guide/turnstile.md) |
| Fix a problem | [Troubleshooting](https://github.com/huangpd/PaperLite/blob/main/docs/guide/troubleshooting.md) |
| Look up a config field | [Configuration reference](https://github.com/huangpd/PaperLite/blob/main/docs/reference/configuration.md) |
| Look up a command | [CLI reference](https://github.com/huangpd/PaperLite/blob/main/docs/reference/cli.md) |
| Query the task database | [Task database](https://github.com/huangpd/PaperLite/blob/main/docs/reference/task-database.md) |
| Understand why it works this way | [Architecture & design notes](https://github.com/huangpd/PaperLite/blob/main/docs/development/architecture.md) |
| Contribute | [CONTRIBUTING](https://github.com/huangpd/PaperLite/blob/main/CONTRIBUTING.md) · [Changelog](https://github.com/huangpd/PaperLite/blob/main/CHANGELOG.md) |

The full index is at [`docs/`](https://github.com/huangpd/PaperLite/blob/main/docs/README.md).

## License

[MIT](https://github.com/huangpd/PaperLite/blob/main/LICENSE)
