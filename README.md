# PaperLite：单站点大批量 PDF 下载

专门处理**单一站点、大量 URL**：用浏览器持续生产过 Cloudflare 的 cookie，curl_cffi 领用下载。生产与消费只通过 Redis 交互，可以分开跑在不同进程上；任务与结果存在站点输出目录的 SQLite 库 `tasks.db` 里。多站点、通用的路由和兜底由 `paper_downloader` 负责。

独立项目（原 `paper_downloader/cookie_pool/`，2026-10-25 拆出）：自己的依赖和 venv，配置是项目根目录的 `config.toml`，不依赖 `paper_downloader` 的任何代码，也不用 PostgreSQL。

## 为什么这样设计

- **cf_clearance 绑定 IP + UA**：同 IP 带 cookie → 200 PDF；同 IP 不带 cookie → 403；cookie 换到别的 IP → 403。所以身份 = IP + cookie + UA，三者一起存、一起用。
- **身份寿命 ≈ IP 寿命**：青果短效 IP 只有 10 分钟（接口 `deadline`），cookie 本身更长，身份过期取两者较早者。每个身份约 9 分钟后就要换新，必须持续生产。
- **cookie 在有效期内一直可用**：同一 IP 每 3–5 秒一次请求，下载 900+ 篇期间 cookie 被拒只有 3 次（都是复用上一轮的旧身份）。
- **过盾要么很快、要么卡死**：成功的多在 12–30 秒（个别 70 秒）；失败的停在 Turnstile "Verifying…" 一直转圈，等到 120 秒也不会过（不是要手动勾选）。所以默认 `pass_timeout_s = 90`，并做**对冲**：过盾超过 `hedge_after_s = 45` 秒还没成功，就另开一个浏览器补位，旧的继续跑到超时。没有对冲时，卡住的过盾占着生产名额 2 分钟，6 个下载线程只等到 2 个身份（20 篇/分钟）。
- **机房 IP 会要求手动勾选**：hw1 直连出口拿到的是要手动勾选的 Turnstile 复选框，必须走代理。
- **站点会单独盯上某些 URL**（2026-10-25 实测）：同一个新 cookie、同一 IP，`1010832.*` 正常拿到 PDF，`1005062.*`（14 篇，连摘要页）一律 403 挑战，多半是前一晚在这几条上反复请求了几百次。原先把"被挑战"一律当成 cookie 坏了：丢身份、任务放回队首，于是每个新身份第一次请求都撞上它们，一篇也下不了。现在遇到挑战先用落地页验身份：落地页正常 → 只是这条 URL 被盯上，`challenge_count+1` 排到后面；落地页也被挑战 → 身份坏了才丢。不用"下一条也被挑战"来判身份，因为被盯上的论文往往成批相邻，会把好身份接连误杀。
- **代理偶尔成批断连**：2026-10-25 00:28 有 13 个不同 IP 在 28 秒内先后报 `curl: (56) Connection closed abruptly`，原先一律丢身份，健康身份从 17 掉到 7、补了约 1.5 分钟。现在 (56) 先用同一身份重试一次。
- **收尾不再白补身份**：只剩别的线程的在途任务时，空等的下载线程注销需求并把身份还回池里（之前最后 1 条重试时生产者仍按 20 个线程补身份，停止时池里空闲 20 个）。
- **冷启动**：第一个身份要等一次过盾（实测约 20 秒）。想省掉就在下载前一两分钟先 `paperlite produce --out <目录> --keep 20`，代价是没人下载时也在消耗 IP。
- **熔断**：2026-10-24 22:38 实测，新过盾的身份全部一篇没下成就被 403 挑战（原因待查，同时代理配额耗尽），没有保护时 1 分钟烧掉 16 个 IP。现在连续 `max_fresh_rejects`（5）个新身份一用就被拒，生产者暂停 `breaker_pause_s`（600s）并告警，之后放一个身份试探，再被拒立刻重新暂停。网络错误不计入。
- **过盾失败直接换 IP**：同一 IP 重试很少成功，换 IP 更快。

## 目录

```
PaperLite/
  pyproject.toml  uv.lock  README.md  .gitignore
  config.example.toml      # 配置模板；复制为 config.toml（含代理密码，不提交）
  src/paperlite/           # 代码（src 布局：测试跑的是安装好的包，不会误用源码目录）
  tests/
  data/                    # URL 清单等输入数据（不提交）
```

## 结构

| 模块 | 职责 |
|---|---|
| `store.py` | Redis 身份池：`add` / `claim`（Lua 原子领用，独占）/ `heartbeat` / `release` / `discard` / `reap` / `stats` |
| `producer.py` | 按需生产：目标健康身份数 = clamp(在线下载线程数, `min_identities`, `max_identities`)，最多 `max_browsers` 个浏览器同时过盾 |
| `browser.py` | 一次过盾：独立 CloakBrowser 进程（一 IP 一指纹）经代理打开落地页，等挑战消失并拿到 `cf_clearance` |
| `consumer.py` | 下载线程：从任务库领任务、从身份池领身份，同 IP 请求间隔 `request_gap_s`；cookie 被拒/IP 封禁/IP 掉线/寿命将尽就丢弃身份，任务放回不计重试 |
| `tasks.py` | SQLite 任务库（表结构见下） |
| `proxy.py` | 调短效代理提取接口拿新 IP 并体检（每个身份一个 IP，用完即弃） |
| `classify.py` | PDF 链接返回 HTML 时判 `error_kind`：`not_found` / `blocked` / `paywall` / `landing_page` |
| `config.py` | 读 `config.toml` 的 `[proxy]` / `[pool]` / `[landing]` |
| `cli.py` | 命令行（`paperlite` 命令 / `python -m paperlite`） |

Redis 键（`{prefix}:{host}:...`）：`id:{id}` 身份 hash（EXPIREAT 到期自动删）、`ready` / `inuse` 两个按过期时间排序的 zset、`lease:{id}` 占用锁（带 TTL，下载进程崩溃后由 `reap` 归还）、`workers` 在线下载线程心跳、`fresh_rejects` / `paused` 熔断计数与暂停标记、`producer` 生产者锁（同一主机同时只有一个生产者干活，多开的待命，前一个退出或崩溃后 15 秒内接手）。**Redis 里不存代理密码**，只存 `ip:port`，消费者用本地 `config.toml` 补鉴权。

生产者只看两个数：健康身份数（剩余寿命 > `refill_before_s` 的空闲+占用）和在线下载线程数。所以：
- 在用身份临近过期前就会开始生产替补，下载线程几乎不空等；
- 下载结束、线程注销后目标降为 `min_identities`，不再白白过盾。

## 用法

```bash
cd PaperLite
uv sync                                               # 或 pip install -e .（国内服务器可加 --index-url 清华源）
cp config.example.toml config.toml                    # 填 [proxy]，含密码，已在 .gitignore

# Redis（hw1 上已有容器 cf-redis，只监听 127.0.0.1:6379，不落盘）
docker run -d --name cf-redis -p 127.0.0.1:6379:6379 redis:latest redis-server --save "" --appendonly no

# 1. 导入任务：一个站点一个输出目录；txt 一行一个链接，或带 pdf_url 列（可选 paper_uid/source/batch_no）的 CSV
paperlite import --out pdfs/acm --urls data/acm.txt

# 2a. 分开跑：生产者常驻，下载进程随用随开
paperlite produce  --out pdfs/acm            # 或 --host dl.acm.org；--keep N 先养着 N 个身份
paperlite download --out pdfs/acm --workers 20

# 2b. 一起跑：同进程生产 + 消费，下载完退出（--urls 可顺便导入）
paperlite run --out pdfs/acm --workers 20

# 3. 查看、重跑、导出
paperlite status --out pdfs/acm              # 各状态数量、失败/非 PDF 按 error_kind 分组、身份池
paperlite retry  --out pdfs/acm --status failed [--kind network]   # 或 --stopped-volumes 重新核实被停卷期
paperlite export --out pdfs/acm --status non_pdf --file non_pdf.csv

# 测试（Redis 部分需设置 PAPERLITE_TEST_REDIS）
uv run pytest            # 或 uv run python -m unittest discover -s tests
```

- 下面的 `paperlite` 命令在项目 venv 里（`uv run paperlite ...` 或先 `source .venv/bin/activate`）；也可在项目根目录用 `python -m paperlite ...`。
- 配置查找顺序：`--config` > 环境变量 `PAPERLITE_CONFIG` > 当前目录的 `config.toml`。在项目根目录运行不用指定。
- 一个任务库只能有一个主机（第一次导入时记下）；重复导入同一链接不会重置已有任务。
- 同一个库同时只允许一个下载进程；启动时把上次中断残留的 `running` 放回 `pending`，随时 Ctrl+C 再重跑即可续上。
- 停止：前台 Ctrl+C；后台进程用 `kill <pid>` 或 `pkill -f "paperlite produce"`（SIGTERM，与 Ctrl+C 一样优雅退出：在途任务放回 pending，在用身份还回池里，生产者关掉浏览器并释放生产者锁）。注意 `pkill -f` 的匹配串若也出现在执行它的 shell 命令行里（如 `ssh host '... cookie_pool produce ...; pkill -f ...'`），会连那个 shell 一起杀掉。
- 换别的站点：在 `[landing]` 加一行该主机的落地页（任意会触发挑战的普通页面），`[proxy] check_url` 换成该站可达的地址。

## 任务库 tasks.db

表 `paper_pdf_resources`，结构参照 paper_downloader 的 PostgreSQL 的同名表，同名列含义与取值一致：

| 列 | 说明 |
|---|---|
| `id` | 自增主键，按它的顺序领取 |
| `url_key` | sha256(规范化 `pdf_url`)，32 字节，唯一：协议和域名转小写、去掉 `#片段` |
| `paper_uid` | 导入时给的，或从链接提取的 DOI（小写）；都没有时 `url-<url_key 前 16 位>`。决定文件名 |
| `pdf_url` / `source` / `batch_no` | 导入时的原值 |
| `volume` | DOI 后缀点号前的卷期号（`10.1145/1012326.1012328` → `1012326`），导入时提取；新式纯数字 DOI 为空，不参与卷期停止 |
| `status` | `pending` → `running` → `success` / `non_pdf` / `failed`（CHECK 约束） |
| `error_kind` | 最近一次没成功的类别：`not_found` / `paywall` / `landing_page`（non_pdf）；`network` / `server_error`（failed 或重试中）；`challenge_failed`（URL 被单独挑战满次数）；`blocked` 只用于丢弃身份，不落到最终结果 |
| `error_message` | non_pdf 时是页面标题；失败时是错误信息 |
| `http_status` / `file_size` / `elapsed_ms` | 最近一次尝试的 HTTP 状态、响应字节数、耗时（毫秒） |
| `exit_ip` | 最近一次尝试用的出口 IP（paper_downloader 没有这列） |
| `retry_count` | 网络错误、5xx 才计；cookie 被拒、IP 封禁不计 |
| `inferred` | 1 = 按卷期推断、没有实际请求（见下「卷期停止」） |
| `challenge_count` | 带有效 cookie 仍被站点挑战的次数（站点单独盯上了这条 URL）；领取按它从小到大排，被挑战过的排到后面；满 `max_url_challenges`（3）判 `failed / challenge_failed`（PG 版没有这列） |
| `file_path` | 成功后的 PDF 路径：`<out>/<h[0:2]>/<h[2:4]>/<paper_uid 的 / 换成 _>.pdf`，`h = sha256(paper_uid)`，与 paper_downloader 同一规则 |
| `claimed_by` | 领取它的 `主机名-进程号-线程号` |
| `created_at` / `updated_at` | 北京时间文本 `YYYY-MM-DD HH:MM:SS` |

另有 `meta` 表记录本库的主机；`volume_stops` 表记录被停掉的卷期（判停类别、作为依据的几篇、停掉的任务数、时间）。直接查库：`sqlite3 pdfs/acm/tasks.db "SELECT status, error_kind, count(*) FROM paper_pdf_resources GROUP BY 1, 2"`。

## 卷期停止

ACM 的 404 和"没有 PDF"基本按卷期整组出现（2671 条实测：9 个 404 卷期、11 个无 PDF 卷期，组内 100% 一致；有 PDF 的卷期里只有零星卷首语、"(abstract)" 条目）。所以：

- 卷期还没下到过 PDF 时，同时最多放 `volume_stop_after`（默认 3）篇出去，其余线程去领别的卷期（同卷期的任务在表里挨着，不限流的话 20 个线程会同时冲进同一卷期）。
- 这几篇都是 `not_found` / `paywall` / `landing_page` → 停掉该卷期其余 pending：记 `non_pdf`、`inferred = 1`，`error_kind` 取样本里最多的类别，`error_message` 写明"按卷期推断…未请求"。
- 下到过一篇 PDF 的卷期永远不停；网络错误、被挑战不计。
- `paperlite retry --out <目录> --stopped-volumes` 重新核实：被停卷期的 non_pdf（样本 + 推断）全部放回 pending，重新抽 3 篇。只放回推断的那部分没用，样本还在会立刻再停。
- `volume_stop_after = 0` 关闭。

70 万条 ACM 实测：导入 7.9 秒，63.7 万条有卷期号、22,926 个卷期；领取一次 0.5–5 ms。

## non_pdf 分类（ACM）

| error_kind | 判断依据 | 含义 |
|---|---|---|
| `not_found` | 标题含 "Error: 404" | DOI 在 ACM 上不存在 |
| `paywall` | 正文有 "Features requiring a subscription" 横幅 | 页面提示有订阅功能 |
| `landing_page` | 其余 | 普通摘要页 |

返回 HTML 的原始页面存在 `<out>/non_pdf/<文件名>.html`。

注意：
- ACM 每个页面的导航栏都有 "Basic Edition"（包括 404 页），不能据此判订阅。
- 实测 `paywall` 和 `landing_page` 两类页面上都**没有任何 PDF / 全文入口**。横幅只说明页面上有灰掉的订阅功能，不能证明订阅后就有 PDF。
- 标题含 "IP blocked" 时判 `blocked`：丢弃该身份、任务放回，不算 non_pdf。
