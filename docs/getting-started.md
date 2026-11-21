# Getting started

**English** | [简体中文](getting-started.zh-CN.md)

This guide takes you from installation to a finished batch. It covers the default browser mode; for curl mode see [Execution modes](guide/modes.md) (Chinese).

## 1. Prerequisites

- Python 3.11 or later on Linux or macOS.
- A short-lived proxy extraction API that returns a fresh `ip:port` per call (JSON or plain text), plus the proxy credentials. To try PaperLite without one, run with `--no-proxy` (browser mode only); most protected sites will then challenge your own IP.
- A local disk for the task database. Output files may go to an object-storage mount.

## 2. Install

```bash
pip install paperlite
# or
uv tool install paperlite
```

Mirrors work as usual, for example `-i https://pypi.tuna.tsinghua.edu.cn/simple`. Check the install with `paperlite --version`.

## 3. Create a config file

```bash
mkdir work && cd work
paperlite init                 # writes ./config.toml with mode 600
```

`config.toml` holds proxy credentials. Do not commit or share it.

Edit three sections. Everything else has working defaults; see the [Configuration reference](reference/configuration.md) when you need them.

```toml
[proxy]
api_url  = "https://proxy.example.com/extract?..."
username = "..."
password = "..."

[paths]
db      = "/data/db/tasks.db"  # SQLite task database: must be on a local disk
storage = "/mnt/obs/papers"    # PDFs and HTML pages: any disk or mount
log_dir = "log"                # JSON Lines logs; empty = terminal only

[run]
command  = "run"               # a bare `paperlite` starts this run
mode     = "browser"           # browser | curl
workers  = 20                  # concurrent browsers
urls     = "urls.csv"          # imported at start-up
url_type = "pdf"               # pdf = expect a PDF; html = save the page itself
```

## 4. Prepare the URL list

Either a text file with one URL per line, or a CSV with a `url` column. Optional CSV columns: `doi`, `source`, `batch_no`, `url_type`.

```csv
doi,url
10.1145/3779295,https://dl.acm.org/doi/pdf/10.1145/3779295
10.1016/j.cell.2024.01.001,https://www.sciencedirect.com/science/article/pii/S0092867424000011
```

URLs from different hosts can be mixed. CSV rows are sorted by DOI before import so related papers sit next to each other. Importing the same file again does not reset existing tasks.

## 5. Run

```bash
paperlite run config.toml
```

PaperLite imports the URL file, downloads every task, and exits when the queue is empty. A progress line is printed every 30 seconds.

- **Stop** with `Ctrl+C` or `SIGTERM`. In-flight tasks go back to `pending`.
- **Resume** by running the same command again.
- **Background:** `setsid nohup paperlite run config.toml > run.log 2>&1 < /dev/null &`

Files are saved as `<storage>/<h[0:2]>/<h[2:4]>/<doi>.pdf` (or `.html`), where `h = sha256(doi)`.

## 6. Check the results

```bash
paperlite status                                  # counts by status and error class
paperlite export --status failed --file failed.csv
paperlite retry --status failed --kind network    # put network failures back in the queue
```

`non_pdf` is a normal outcome, not an error: the page loaded but held no PDF (`not_found`, `paywall`, `landing_page`). See [Task database](reference/task-database.md) for every status and error class.

## Next steps

- [Operations](guide/operations.md): retries, inspection rules, adding sites, logs.
- [Execution modes](guide/modes.md): when curl mode is worth the extra Redis.
- [Monitoring](guide/monitoring.md): a dashboard across many machines.
- [Troubleshooting](guide/troubleshooting.md).
