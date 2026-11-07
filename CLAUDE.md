# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

PaperLite 是从 `paper_downloader/cookie_pool/` 拆出的独立项目：单一站点、大批量 PDF/网页下载。浏览器持续生产过 Cloudflare 的 cookie（身份 = IP + cookie + UA），curl_cffi 领用批量下载，二者只通过 Redis 交互、可分进程跑；任务与结果存站点输出目录下的 SQLite 库 `tasks.db`。

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

**两条独立流水线经 Redis 握手**，没有直接函数调用：

- `producer.py` 只看两个数——健康身份数（剩余寿命 > `refill_before_s` 的空闲+占用）和在线下载线程数——按需开浏览器（`browser.py`，一 IP 一指纹经代理过盾）攒身份进池。
- `consumer.py` 的下载线程从 `tasks.py`（SQLite）领任务、从 `store.py`（Redis 身份池）领身份，按 `request_gap_s` 节流。
- 耦合点全在 `store.py` 的 Redis 键约定（`{prefix}:{host}:...`，README「结构」节列全）：`claim` 用 Lua 原子独占领用，`lease:{id}` 带 TTL 让崩溃的消费者由 `reap` 归还身份，`producer` 锁保证一主机只一个生产者干活。**Redis 不存代理密码**，只存 `ip:port`，消费者用本地 `config.toml` 补鉴权。

**两种运行模式**（`run` 的开关）：默认 **curl 模式**（浏览器只过盾存 cookie、curl 批量下，须单主机）；`--browser` **浏览器模式**（每浏览器+IP 过盾一次后连续下同域名、整页存 `.html`，可混多主机，浏览器自己跟跳转）。`browser_pool.py` 服务浏览器模式。

**身份坏不坏的判定是核心陷阱**（见 POSTMORTEMS 与 README 设计节）：带有效 cookie 仍被挑战时，**先用落地页验身份**——落地页正常说明只是这条 URL 被站点单独盯上（`challenge_count+1` 排后面），落地页也被挑战才算身份坏、丢弃。不要用"下一条也被挑战"来判身份，被盯上的论文成批相邻会误杀好身份。

**易改错的行为语义**，动之前先对齐 README/POSTMORTEMS：

- 熔断：连续 `max_fresh_rejects` 个新身份一用就被拒 → 生产者暂停 `breaker_pause_s`；网络错误不计入。
- 卷期停止（`tasks.py`）：同卷期未下到 PDF 时最多放 `volume_stop_after` 篇，都 non_pdf 就把该卷期其余 pending 记 `non_pdf`+`inferred=1`（没实际请求）；下到过一篇 PDF 的卷期永不停。
- 巡检（`inspect.py` + `tasks.py` 的 `flag`/`skip_rules`）：浏览器模式下同一标题重复到 `title_dup_trigger` 次，从 doi+url 两维各取最长公共前缀归纳规则，用已采数据验 precision/support，达标才把命中的 pending 标 `suspect_cover` 跳过。只观察本次运行的内存滑动窗口（不扫历史），按 `(doi_prefix, url_prefix)` 去重而非按标题去重——详见 POSTMORTEMS「实时标题巡检过早停止归纳」。

## 约束

- **src 布局**：测试跑的是安装好的包，不会误用源码目录。
- **SQLite 任务库必须放本地盘**（要随机写/文件锁/mmap，FUSE/goofys 不支持）；PDF 可放对象存储挂载（顺序写）。配置 `[pool] db_dir` 统一指定任务库目录后，日常只写 `--out`。
- **打包排除敏感文件**：`pyproject.toml` 的 `sdist only-include` 白名单确保 `config.toml`（含代理密码）、`data/`、`db/`、`pdfs/` 绝不进包。新增需打包的路径时改这里。
- 站点/任务库的自动定位规则（`--out` > `--host` > URL 主机 > 唯一站点）在 README「命令一览」末尾，多站点时未指定会列出来让选。
