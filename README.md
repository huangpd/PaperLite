# PaperLite

[![PyPI](https://img.shields.io/pypi/v/paperlite.svg)](https://pypi.org/project/paperlite/)
[![Python](https://img.shields.io/pypi/pyversions/paperlite.svg)](https://pypi.org/project/paperlite/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**English** | [简体中文](README.zh-CN.md)

PaperLite is a high-volume downloader for PDFs and web pages served behind bot-protection layers such as Cloudflare. It drives real browsers through rotating short-lived proxies, persists every task in a local SQLite database, and resumes cleanly after interruption.

- **One config file, one batch.** URLs from any number of hosts go into a single task database and are processed together.
- **Adaptive Hybrid Mode (Recommended).** Guided by `domain_rules.toml`, lightweight cURL handles static publisher sites in milliseconds while CloakBrowser seamlessly challenges Cloudflare for protected targets.
- **Browser mode by default.** Each worker owns one browser and one IP, solves the challenge once, then downloads continuously, following redirects and saving PDFs or full HTML pages.
- **Resumable and inspectable.** Task state, error classes and per-attempt metrics live in SQLite; `Ctrl+C` returns in-flight tasks to the queue.
- **Lifecycle-driven Chunk Leasing.** Workers atomically claim task chunks sized dynamically to the proxy IP's remaining lifespan, rolling back unconsumed items with zero lag upon IP rotation.
- **Self-protecting.** Proxy health checks, IP rotation on mid-lease failure, a circuit breaker for exhausted proxy quotas, and heuristics that skip dead or duplicate content.
- **Optional fleet monitoring.** Terminals publish metrics to RabbitMQ; a bundled dashboard aggregates them across machines.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Adaptive Mode & Preprocessing Pipeline](#adaptive-mode--preprocessing-pipeline)
- [Configuration](#configuration)
- [Command reference](#command-reference)
- [Operations](#operations)
- [Monitoring](#monitoring)
- [Turnstile tokens](#turnstile-tokens)
- [Task database](#task-database)
- [Design notes](#design-notes)
- [Development](#development)
- [License](#license)

## How it works

PaperLite offers three execution modes sharing the same task database, storage layout and command set.

| | Adaptive mode (`auto`, recommended) | Browser mode (`browser`, default) | curl mode (`--curl`) |
|---|---|---|---|
| Model | Auto-routes: cURL direct for static sites, real headless browsers for WAF sites | N workers, each with its own browser and IP | Browsers solve challenges and pool the cookies; `curl_cffi` threads download with them |
| Hosts per batch | Any mix (dynamically dispatched) | Any mix | Exactly one |
| Redirects / HTML pages | Followed; saved as `.html` / `.pdf` | Followed; pages saved as `.html` | PDF-oriented |
| External services | None (Zero Redis required) | None | Redis (identity pool) |
| Throughput | Peak efficiency: 0.2s for direct, 100% bypass for WAF | High bypass, 2–20 concurrent browsers | Very large single-site jobs where throughput per IP matters |

Two storage locations are configured independently under `[paths]`:

- **`db`**: the SQLite task database. It **must be on a local disk**, because SQLite needs random writes, file locks and mmap, which FUSE mounts such as goofys or s3fs do not provide.
- **`storage`**: the output directory for PDF and HTML files. Writes are sequential, so an object-storage mount is fine.

## Requirements

- Python 3.11 or later; Linux or macOS.
- A short-lived proxy extraction API that returns a fresh `ip:port` per call (JSON or plain text), plus proxy credentials. `--no-proxy` runs without one for local testing.
- Redis, only for curl mode.
- RabbitMQ, only if you enable monitoring.

## Installation

```bash
pip install paperlite
# or
uv tool install paperlite
```

Mirrors such as `-i https://pypi.tuna.tsinghua.edu.cn/simple` work as usual.

From source:

```bash
git clone https://github.com/huangpd/PaperLite.git
cd PaperLite
uv sync
uv run paperlite --version
```

## Quick start

```bash
mkdir work && cd work
paperlite init                 # writes ./config.toml with mode 600
```

Edit `config.toml`:

1. `[proxy]`: extraction API URL and proxy credentials.
2. `[paths]`: `db` and `storage`.
3. `[run]`: the job itself (mode, worker count, URL file).

Then start the batch:

```bash
paperlite run config.toml
```

The command imports the URL file, downloads everything, and exits when the queue is empty. It logs one progress line every 30 seconds. `Ctrl+C` or `SIGTERM` shuts down gracefully: in-flight tasks return to `pending`, and running the same command again resumes where it stopped.

> **Tip:** keep every run in a config file rather than on the command line. The file records which database, mode and output location a batch uses, which makes reruns, migrations and hand-overs reproducible.

## Adaptive Mode & Preprocessing Pipeline

When handling multi-million paper datasets spanning dozens of publisher domains, PaperLite features a two-stage preprocessor and an adaptive execution engine:

```
[Raw Tasks (e.g. 1.95M DOIs)] 
             │ 
             ▼ (DuckDB Vectorized SQL: 0.22s pattern clustering)
   [Stage 1: resolve]  ───Concurrent redirect probing───► [doi_domain_routes (DB)]
             │ 
             ▼ 
   [Stage 2: audit]    ───Differential WAF diagnosis────► [domain_rules.toml (Rules)]
             │ 
             ▼ 
   [Adaptive Run: paperlite run (mode="auto")]
     ├─ curl_direct (Springer, etc.) ──► 0.2s lightweight cURL fetch
     └─ browser_only (Cell, SD, etc.) ──► CloakBrowser auto-solving WAF
```

### 1. Run Preprocessing Pipeline

```bash
# End-to-end audit in one command:
paperlite preprocess --urls raw_dois.csv --out-rules domain_rules.toml

# Or split into individual stages:
# Stage 1: Fast pattern clustering & redirect domain probing via DuckDB (2M DOIs in minutes)
paperlite resolve --urls raw_dois.csv --out-map doi_routes.db --concurrency 6

# Stage 2: Audit only the unique target domains
paperlite audit --map doi_routes.db --out-rules domain_rules.toml
```

### 2. Configure & Run Adaptive Batch

In your `config.toml`, set `mode = "auto"` to activate dynamic hybrid routing:

```toml
[paths]
db      = "db/tasks.db"
storage = "file/tasks"
log_dir = "log"

[limits]
max_browsers = 3
page_interval_s = [2, 5]
pass_timeout_s = 45

[run]
command = "run"
mode    = "auto"                # Adaptive hybrid mode
rules   = "domain_rules.toml"   # Domain rules snapshot
proxy   = true                  # False for local direct test
workers = 3
url_type = "html"
```

Start the run:

```bash
paperlite run config.toml
# Or override via CLI:
paperlite run --auto --rules domain_rules.toml --workers 3
```

### 3. Lifecycle-Driven Chunk Leasing

In adaptive and browser modes, PaperLite adopts a robust chunk lease mechanism:
- **Dynamic Lease Sizing:** Workers compute batch sizes from remaining proxy lease seconds (`min(20, max(1, left_s / 8))`), eliminating lock contention on large SQLite databases.
- **Ordered Atomic Claims:** Tasks are pre-ordered by `(pattern_key, doi)` and claimed in single SQLite atomic transactions.
- **Zero-Drop Safe Rollback:** Unconsumed tasks return immediately to `pending` upon proxy expiration, errors, or graceful shutdown.

## Configuration

PaperLite looks for its config in this order: `--config`, then the `PAPERLITE_CONFIG` environment variable, then `./config.toml`. The template generated by `paperlite init` ([`src/paperlite/config.example.toml`](src/paperlite/config.example.toml)) documents every option.

### `[run]`: launch parameters

```toml
[run]
command  = "run"        # makes a bare `paperlite` start this run; leave empty to print help instead
mode     = "auto"       # auto (recommended) | browser | curl
rules    = "domain_rules.toml" # routing snapshot for auto mode
proxy    = true         # false = --no-proxy (browser/auto mode only)
workers  = 20           # browser: concurrent browsers; curl: download threads
urls     = "mixed.csv"  # optional; imported on start-up
url_type = "html"       # pdf | html
headed   = false        # browser/auto mode: display browser window for debugging
```
headed   = false        # browser mode only; show browser windows for debugging
```

When a config file is passed to `run`, the `[run]` section is authoritative and other `run` flags on the command line are ignored. Other subcommands (`import`, `status`, `retry`, `export`, …) keep their own flags.

### Key options

| Option | Default | Description |
|---|---|---|
| `[proxy] api_url`, `username`, `password` | (required) | Extraction API and proxy authentication |
| `[proxy] check_url`, `check_url2` | Cloudflare, httpbin | Health-check targets for each new IP; passing either is sufficient. Keep them as IP-echo endpoints, not the target site: the check verifies connectivity, latency and exit IP, while passing the site's protection is handled by the challenge step |
| `[proxy] check_max_latency_ms` | 3000 | Latency ceiling for the health check |
| `[proxy] verify_exit_ip` | true | Require the echoed exit IP to match the proxy IP |
| `[paths] db` | `tasks.db` | Task database (local disk only) |
| `[paths] storage` | `output` | Output directory (mountable) |
| `[paths] log_dir` | `log` | Log directory; empty logs to the terminal only |
| `[limits] max_browsers` | 20 | Upper bound on concurrent browsers |
| `[limits] pass_timeout_s` | 90 | Hard limit for one challenge attempt |
| `[limits] hedge_after_s` | 45 | Start a backup browser if a challenge is still unsolved after this long |
| `[limits] request_gap_s` | [3, 5] | Delay between requests on the same IP |
| `[limits] page_interval_s` | [10, 30] | Delay between pages in one browser |
| `[limits] max_file_size_mb` | 200 | Per-file size limit |
| `[limits] max_tries` | 3 | Retries for network errors |
| `[limits] volume_stop_after` | 3 | curl mode: stop a journal volume after this many consecutive non-PDF results; 0 disables |
| `[limits] title_dup_trigger` | 10 | Duplicate-title count that triggers rule induction; 0 disables |
| `[limits] max_identities` | 20 | curl mode: healthy identity cap |
| `[limits] max_fresh_rejects` | 5 | curl mode: circuit-breaker threshold |

Command-line overrides: `--db` takes precedence over `[paths].db`, and `--storage` (on `run` and `download`) over `[paths].storage`.

## Command reference

| Command | Description |
|---|---|
| `init [--force]` | Generate a `config.toml` template |
| `import --urls FILE [--type pdf\|html]` | Import tasks from a text file (one URL per line) or a CSV with a `url` column and optional `doi`, `source`, `batch_no` and `url_type` columns. Re-importing never resets existing tasks |
| `run [CONFIG] [--urls FILE] [--workers N]` | Download until the queue is empty. Browser mode by default; `--auto` enables adaptive routing; `--curl` switches to curl mode; `--no-proxy` uses the local IP |
| `preprocess --urls FILE [--out-rules TOML]` | End-to-end preprocessing: DuckDB pattern clustering, redirect domain probing, differential WAF diagnosis and rule export |
| `resolve --urls FILE [--out-map DB]` | Stage 1 preprocessor: Cluster patterns and probe landing domains into SQLite map |
| `audit [--map DB] [--out-rules TOML]` | Stage 2 preprocessor: Audit only unique landing domains for WAF resilience and export rules |
| `produce [--keep N]` | curl mode: run only the identity producer (long-running) |
| `download [--workers N]` | curl mode: run only the downloaders; needs a producer running |
| `status` | Counts by status, failures by `error_kind`, stopped volumes and, in curl mode, the identity pool |
| `retry --status failed [--kind KIND]` | Reset failed (or `non_pdf`) tasks to `pending` |
| `retry --stopped-volumes` | Re-sample volumes stopped by the volume heuristic |
| `export [--status S] [--file out.csv]` | Export the task table as CSV |
| `inspect [--list] [--reset]` | Show, rebuild or clear the duplicate-title skip rules |
| `turnstile --url URL --sitekey KEY` | Obtain a Cloudflare Turnstile token (see [below](#turnstile-tokens)) |

Global flags: `--config`, `--log-level`, `--log-format auto|text|json`, `--version`.

**Logging.** Log files under `[paths].log_dir` are always JSON Lines. Terminal output defaults to `auto`: human-readable, colourised text on an interactive TTY (respects `NO_COLOR`), and JSON when stdout is redirected, piped or collected by a container runtime.

## Operations

**Run in the background:**

```bash
setsid nohup paperlite run config.toml > run.log 2>&1 < /dev/null &
pkill -f "paperlite run"        # graceful stop (SIGTERM)
```

**Local database, mounted output:**

```toml
[paths]
db      = "/data/db/tasks.db"   # local disk
storage = "/mnt/obs/papers"     # object-storage mount
```

**Adding sites.** No per-site configuration is needed; hosts are taken from the URLs in the task database.

- Before taking tasks, each new browser warms up by solving the challenge on the first imported host, at `https://{host}/` by default.
- Pages on other hosts solve their own challenge the first time they are rendered.
- If that host's home page is unsuitable (for example, it does not trigger the challenge or redirects elsewhere), override the warm-up URL in `[landing]`, e.g. `"www.example.org" = "https://www.example.org/journal/abc"`.
- After the browser solves a challenge, curl mode checks the warm-up URL with `curl_cffi` using the same IP, user agent, and Cookie Jar before storing the identity in Redis. Set `[landing]` to a representative article page: a successful home-page check does not prove article pages are accessible. Also review the non-PDF heuristics in `classify.py`, which were written for ACM.

**Cold start in curl mode.** The first identity takes about 20 seconds to solve. `produce --keep 20` pre-warms the pool, at the cost of consuming IPs while no downloader is running.

Curl mode converts browser cookies into a `CookieJar`, retaining duplicate names with distinct domains/paths, host-only scope, secure, expires, HttpOnly and SameSite attributes. Redis keeps a JSON list in `cookies` with a `cookies_format=cookiejar-v1` marker; existing browser-cookie lists remain readable without clearing Redis. Admission checks and downloads use the identity's live jar, so response cookie updates and deletions are saved on insertion, consumer heartbeats and release. Saving never extends the identity or proxy lifetime. Restart both producers and consumers when upgrading.

> When running from a source checkout installed through a mirror, call `.venv/bin/paperlite` directly. `uv run` re-syncs against `uv.lock` and the default index, which can stall on restricted networks.

## Monitoring

Each `paperlite run` can publish metrics to a shared RabbitMQ broker. `paperlite-monitor serve` consumes them into its own SQLite database (30-day retention) and serves a web dashboard with 24-hour, 7-day and 30-day views. Monitoring is strictly out-of-band: if it is disabled, the broker is unreachable, or the client library is missing, downloads continue unaffected.

Enable it on each terminal:

```toml
[monitor]
enabled  = true
amqp_url = "amqp://pl_terminal:PASSWORD@BROKER_IP:5672/paperlite"
vendor   = "qingguo"            # proxy vendor label, used for vendor × domain breakdowns
```

```bash
paperlite-monitor test          # verify broker connectivity from a terminal
paperlite-monitor serve         # dashboard on http://HOST:8700/
paperlite-monitor demo          # dashboard with synthetic data, no broker required
```

The dashboard covers:

| Panel | Metrics |
|---|---|
| Proxy health | Checks, pass rate, mean latency, rejection reasons |
| Browsers | Online vs. target, start-up success rate, exceptions, mean challenge time |
| Page fetches | Successes, failures, success rate, challenge pass rate, mean and P95 latency |
| Domains | Per-domain success, pass rate, latency and throughput; server-side search, sort and paging; optional merge by registrable domain |
| Proxy vendors | Success by vendor × domain; IP health by vendor |
| WAF | Pass rate and latency for Cloudflare, AWS WAF and DataDome, identified from response headers |
| Live | Pending and running tasks, request rate by domain, event stream, terminal list |

Metric semantics:

- Page metrics count **attempts**. A task retried after a challenge timeout counts once per attempt.
- *Passed* means the page actually loaded. *Success* means it passed and returned HTTP < 400.
- Latency includes loaded pages only.
- Domains with fewer than 20 samples are dimmed.
- A domain is flagged when its success or pass rate falls below 70%.

Messages carry statistics and exit IPs only, never proxy credentials or page content. Each terminal buffers events in a bounded local queue and publishes them from a background thread. The dashboard persists events to disk before acknowledging them.

Deployment, broker accounts, security and capacity planning are covered in [`docs/MONITOR.md`](docs/MONITOR.md).

## Turnstile tokens

PaperLite can solve a Cloudflare Turnstile widget for any page, independent of the task database and Redis:

```bash
paperlite turnstile --url https://example.com/login --sitekey 0x4AAAA... --json
# {"ok": true, "token": "1.xxxx…", "user_agent": "...", "seconds": 15.3, ...}

paperlite turnstile --serve 8191 --concurrency 4
curl localhost:8191/solve -d '{"url":"...","sitekey":"0x4AAAA...","proxy":"http://u:p@1.2.3.4:8000"}'
```

How the solver works:

1. It opens the target URL in CloakBrowser but intercepts the document and substitutes a minimal page that renders only the requested sitekey. The origin stays the target site, so hostname validation passes.
2. It clicks the checkbox inside the Turnstile iframe and reads the token from the callback.

Tokens are single-use and valid for about 300 seconds. Some sites bind them to the submitting IP and user agent, so submit through the same proxy and with the returned `user_agent`.

## Task database

Tasks live in the table `paper_pdf_resources`, whose schema mirrors the upstream PostgreSQL table of the same name.

| Column | Description |
|---|---|
| `id` | Auto-increment key; tasks are claimed in this order |
| `url_key` | SHA-256 of the normalised URL; unique |
| `doi` | Imported value (lower-cased) or derived from the URL; determines the file name |
| `url`, `source`, `batch_no` | Imported values |
| `url_type` | `pdf` (expects a PDF) or `html` (saves the page itself) |
| `volume` | DOI volume key (`10.1145/1012326.1012328` → `1012326`), used by volume stopping |
| `status` | `pending` → `running` → `success` / `non_pdf` / `failed` |
| `error_kind` | `not_found`, `paywall`, `landing_page` (non-PDF); `network`, `server_error`, `extract_failed`, `challenge_failed` |
| `title`, `final_url` | Page title and the final URL after redirects |
| `http_status`, `file_size`, `elapsed_ms`, `exit_ip` | Metrics from the latest attempt |
| `retry_count`, `challenge_count` | Network/5xx retries; challenges despite a valid cookie |
| `inferred` | 1 if the outcome was inferred by volume stopping rather than requested |
| `file_path` | `<storage>/<h[0:2]>/<h[2:4]>/<doi>.pdf` (or `.html`), where `h = sha256(doi)` |

Auxiliary tables: `meta`, `volume_stops` and `skip_rules`.

```bash
sqlite3 tasks.db "SELECT status, error_kind, count(*) FROM paper_pdf_resources GROUP BY 1, 2"
```

## Design notes

These behaviours are deliberate. See [`POSTMORTEMS.md`](POSTMORTEMS.md) for the incidents behind them.

- **An identity is IP + cookie + user agent.** `cf_clearance` is bound to both the IP and the user agent, so all three are stored and used together; changing any one yields 403. An identity expires with whichever comes first, the IP lease or the cookie, so identities are produced continuously.
- **Curl identity acceptance.** A browser pass does not guarantee that `curl_cffi` can access the site. The producer checks `[landing]` with the same identity before adding it to Redis. A later Cloudflare `Access denied` / `Attention Required` response (403/429) discards the identity and returns the task to `pending` instead of permanently failing the paper.
- **Challenges either pass quickly or hang.** Successful solves typically take 12–30 s, while failures stall on "Verifying…" until timeout. A backup browser is launched after `hedge_after_s` (45 s), and the original runs until `pass_timeout_s` (90 s). Datacenter IPs are often given an interactive challenge, so a proxy is effectively required.
- **A challenged URL does not imply a bad identity.** Sites flag individual URLs, often in contiguous runs. On a challenge, PaperLite re-tests the identity against the landing page:
  - If the landing page loads, only that URL is deprioritised (`challenge_count + 1`).
  - If the landing page is also challenged, the identity is discarded.

  Judging the identity by whether the next URL is challenged would discard healthy identities in bulk.
- **Mid-lease IP failure.** Short-lived IPs often die before their lease ends. The symptoms are `ERR_TUNNEL_CONNECTION_FAILED`, `ERR_PROXY_*`, `ERR_CONNECTION_CLOSED/RESET` and `ERR_EMPTY_RESPONSE`. These are treated as IP faults, not task faults:
  - The task returns to `pending` with `error_kind=network`, without incrementing its retry or challenge counters.
  - After two consecutive failures, the browser rotates to a new IP.
- **Circuit breaker.** If `max_fresh_rejects` new identities in a row are rejected on first use, the producer pauses for `breaker_pause_s`, raises an alert, then admits a single probe. This keeps an exhausted proxy quota from burning hundreds of IPs a minute. Network errors do not count toward the threshold.
- **Volume stopping** (curl mode). Missing PDFs tend to come in whole journal volumes. For a volume with no PDF yet, at most `volume_stop_after` tasks are in flight. If all of them come back non-PDF, the rest of the volume is marked `non_pdf` with `inferred=1`. A volume that has ever produced a PDF is never stopped.
- **Streaming downloads.** Timeouts measure stalls, not total duration, so large files on slow proxies complete. A valid PDF must contain `%PDF-` within its first 1024 bytes.
- **Duplicate-title inspection** (browser mode). DOIs are unique, so identical titles across many pages usually mean an error page or a cover page.
  1. When any loaded page's title (success or 404, excluding challenge timeouts) repeats `title_dup_trigger` times, PaperLite induces the most specific DOI/URL prefix that explains the repeats.
  2. It validates the rule against data already collected, requiring precision ≥ 0.98.
  3. Only then does it mark matching pending tasks as skipped.

  The heuristic deliberately prefers missing a skip over skipping a live task.

**Reference throughput** (ACM Digital Library, 20 workers): 120–160 tasks per minute. One identity lasts about 9 minutes and downloads 60–70 papers. Expect 180–300 short-lived IPs per 10,000 papers; 30-minute IPs need about a third as many.

## Development

```bash
uv sync
uv run pytest                                                   # Redis-backed tests are skipped
PAPERLITE_TEST_REDIS=redis://127.0.0.1:6379/0 uv run pytest     # include Redis tests
PAPERLITE_TEST_AMQP=amqp://guest:guest@127.0.0.1:5672/%2F uv run pytest tests/test_monitor.py
```

Project layout:

```
src/paperlite/
  cli.py, config.py   command line; configuration ([proxy], [limits], [paths], [run])
  browser_pool.py     browser mode: per-worker browser + IP reuse
  browser.py          one challenge solve (isolated CloakBrowser process via proxy)
  turnstile.py        Turnstile detection, clicking, token service
  producer.py         curl mode: on-demand identity production, solve concurrency
  cookies.py          CookieJar conversion for browser/Redis cookie records
  store.py            curl mode: Redis identity pool (atomic Lua claims, leases, producer lock)
  consumer.py         curl mode: download threads, streaming, identity health
  tasks.py            SQLite task store, volume stopping
  inspect.py          duplicate-title rule induction
  classify.py         non-PDF classification (not_found, paywall, landing_page)
  proxy.py            proxy extraction and layered health checks (TCP/TLS/TTFB/total)
  monitor/            metrics reporter (no-op when disabled) and the paperlite-monitor dashboard
  config.example.toml configuration template
tests/                pytest suite (unittest style)
docs/MONITOR.md       monitoring deployment guide
```

`config.toml` contains proxy credentials. It is excluded from version control and from the sdist through an explicit allow-list in `pyproject.toml`.

## License

[MIT](LICENSE)
