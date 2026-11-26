# PaperLite

[![PyPI](https://img.shields.io/pypi/v/paperlite.svg)](https://pypi.org/project/paperlite/)
[![Python](https://img.shields.io/pypi/pyversions/paperlite.svg)](https://pypi.org/project/paperlite/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/huangpd/PaperLite/blob/main/LICENSE)

**English** | [简体中文](https://github.com/huangpd/PaperLite/blob/main/README.zh-CN.md)

**PaperLite** is a high-concurrency, distributed crawling cluster built for collecting tens of millions of papers and web pages. A fleet of fingerprinted real browsers, driven through rotating short-lived proxies, passes Cloudflare-class bot protection natively. Task scheduling, the identity pool and telemetry all live in a single PostgreSQL — no Redis, no message queue, no extra middleware — so one machine and a hundred machines run the same architecture.

## 🚀 Highlights

- **Fingerprinted browser fleet.** Built on CloakBrowser's fingerprinted Chromium + Playwright. Each worker owns one browser and one exit IP, with timezone, locale and WebRTC aligned to that IP; it passes the challenge once, then downloads continuously and follows redirects.
- **Streaming PDF capture.** PDFs are intercepted at the network layer over CDP and streamed to disk — no rendering, no clicking. HTML pages are saved whole.
- **Distributed scheduling.** Work is handed out in leased chunks; machines join or leave at any time, and chunks from dead nodes are reclaimed by others. A single batch scales to tens of millions of tasks.
- **Zero middleware.** Task queue, identity pool and telemetry all run on PostgreSQL (`SKIP LOCKED` claims + advisory-lock coordination). One database is the whole deployment.
- **Two engines.** Browser mode (default) for mixed-host batches; curl mode turns browser-solved sessions into a cookie identity pool and downloads single-site batches at high speed with `curl_cffi` TLS impersonation.
- **Self-healing.** Four-phase proxy probes (TCP / TLS / TTFB / total) with exit-IP verification, automatic IP rotation on mid-lease failure, a circuit breaker that stops burning proxy quota when fresh identities keep getting rejected, and title-duplicate rules that skip dead or duplicate content.
- **Resumable.** `Ctrl+C` hands in-flight work back immediately; restarting picks up where it stopped. Results are written at least once, and duplicates are harmless.
- **Observable end to end.** Per-task error classes and timings in the database, plus a real-time multi-machine dashboard (success rate, proxy quality, WAF mix, browser health).

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
