# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

PaperLite 是从 `paper_downloader/cookie_pool/` 拆出的独立项目：大批量 PDF/网页下载（过 Cloudflare）。**一份 config = 一个批次**，各种主机的 URL 混进一个任务库，默认用**浏览器模式**统一跑（每浏览器+IP 过盾后连续下、跟跳转、整页存 `.html`，不用 Redis）。另有 **curl 模式**（单主机 cookie 池：浏览器过盾存 cookie、curl_cffi 领用批量下，用 Redis），日常少用。位置在 `[paths]` 定义：`db`（任务库，本地盘）、`storage`（输出，可挂载），两者相互独立。

`README.md` 是面向用户的完整文档（命令、配置项、`tasks.db` 表结构、设计取舍），`POSTMORTEMS.md` 记录踩过的坑。改动涉及过盾/熔断/卷期停止/巡检的行为语义时，先读 README 对应小节——那里写明了"为什么这样设计"，别无意中改回被否决的方案。

## 常用命令

```bash
uv sync                                   # 装依赖（开发）
uv run pytest                             # 跑测试；连不上 Redis 的用例自动跳过
PAPERLITE_TEST_REDIS=redis://127.0.0.1:6379/0 uv run pytest   # 连真实 Redis，全跑
uv run pytest tests/test_paperlite.py::ConfigTest::test_from_dict   # 单个用例（unittest 风格，pytest 直接跑）
uv run paperlite <命令>                   # 运行；子命令见 README「命令一览」
```

测试是 `unittest.TestCase` 子类，用 pytest 跑。Redis 相关用例用 `@unittest.skipUnless(os.environ.get("PAPERLITE_TEST_REDIS"))` 门控——离线改代码时它们会被跳过，提交前若动了 `store.py`/`producer.py`/`consumer.py` 的 Redis 逻辑，务必起本机 Redis 全跑一遍。

## 架构要点（跨文件才看得清的部分）

**两种模式**（`run` 默认 browser，`--curl` 切 curl）：

- **浏览器模式**（主，`browser_pool.py`）：N 个 worker 各独占一个浏览器+IP，过盾后串行领任务下载。**不碰 Redis**。各种主机混在一个库里，每页各自过盾、跟跳转。
- **curl 模式**（`consumer.py` + `producer.py` + `store.py`，经 Redis 握手，无直接调用）：`producer` 按"健康身份数 vs 在线下载线程数"开浏览器过盾攒身份进 Redis 池；`consumer` 下载线程从 `tasks.py` 领任务、从 `store.py`（Lua 原子领用、`lease:{id}` TTL、`producer` 锁）领身份。**Redis 不存代理密码**，只存 `ip:port`，消费者用本地 `config.toml` 补鉴权。须单主机。

**配置结构**（`config.py`）：`[proxy]`=代理；`[limits]`(dataclass `Limits`)=并发与行为；`[paths]`(dataclass `Paths`)=`db`/`storage`/`log_dir`；`[run]`=启动参数（mode 默认 browser）。db 与 storage 相互独立、不互相推导；命令行 `--db`/`--storage` 覆盖。没有 `--out`/`--host`/站点概念。

**身份坏不坏的判定是核心陷阱**（见 POSTMORTEMS 与 README 设计节）：带有效 cookie 仍被挑战时，**先用落地页验身份**——落地页正常说明只是这条 URL 被站点单独盯上（`challenge_count+1` 排后面），落地页也被挑战才算身份坏、丢弃。不要用"下一条也被挑战"来判身份，被盯上的论文成批相邻会误杀好身份。

**易改错的行为语义**，动之前先对齐 README/POSTMORTEMS：

- 熔断：连续 `max_fresh_rejects` 个新身份一用就被拒 → 生产者暂停 `breaker_pause_s`；网络错误不计入。
- 卷期停止（`tasks.py`）：同卷期未下到 PDF 时最多放 `volume_stop_after` 篇，都 non_pdf 就把该卷期其余 pending 记 `non_pdf`+`inferred=1`（没实际请求）；下到过一篇 PDF 的卷期永不停。
- 巡检（`inspect.py` + `tasks.py` 的 `flag`/`skip_rules`）：浏览器模式下，任何**真加载出来**的页（成功页 + 404 等失效页，`browser_pool._observe` 在成功和 404 分支都喂、不喂 `r.ok=False` 的过盾超时页）标题重复到 `title_dup_trigger` 次就归纳规则。候选取 doi/url 最长公共前缀及其 `_coarsen`（限 `_MAX_COARSEN` 位，防退化成 `/pii/S`），选规则**偏最具体**（最长前缀）而非命中最多，用已采数据验 precision/support 达标才标 pending 跳过。只看内存滑动窗口，按 `(doi_prefix, url_prefix)` 去重——详见 POSTMORTEMS。

## 约束

- **src 布局**：测试跑的是安装好的包，不会误用源码目录。
- **`[paths].db` 必须本地盘**（SQLite 要随机写/文件锁/mmap，FUSE/goofys 不支持）；`[paths].storage` 可放对象存储挂载（顺序写）。两者各写各的，不互相推导。
- **打包排除敏感文件**：`pyproject.toml` 的 `sdist only-include` 白名单确保 `config.toml`（含代理密码）、`data/`、`db/`、`pdfs/` 绝不进包。新增需打包的路径时改这里。
- 代理体检在 `proxy.py`：用 curl_cffi **低层 `Curl`** 拿 TCP/TLS/TTFB/总耗时四段，判 2xx + 核对回显出口 IP == proxy_ip + 延迟闸；两个判定目标任一通过即合格，每 IP 记一条 `proxy_check` 结构化日志。
