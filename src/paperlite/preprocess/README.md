# PaperLite 预处理与自动化路由识别子系统 (`paperlite.preprocess`)

> **定位**：在千万至亿级 DOI 数据正式进入高并发下载之前，通过 **“先映射建档 ➜ 再体检定级 ➜ 最后确定性大生产”** 的前置预处理，彻底消除“边跑边改、动态猜测”的高风险不确定性。

---

## 目录
- [一、 为什么需要前置预处理？](#一-为什么需要前置预处理)
- [二、 整体架构全景图](#二-整体架构全景图)
- [三、 模块组织结构](#三-模块组织结构)
- [四、 命令行使用指南](#四-命令行使用指南)
  - [1. 一键全自动流水线 (`paperlite preprocess`)](#1-一键全自动流水线-paperlite-preprocess)
  - [2. 阶段一：仅映射建档 (`paperlite resolve`)](#2-阶段一仅映射建档-paperlite-resolve)
  - [3. 阶段二：仅反爬体检与规则生成 (`paperlite audit`)](#3-阶段二仅反爬体检与规则生成-paperlite-audit)
- [五、 `domain_rules.toml` 规范说明](#五-domain_rulestoml-规范说明)
- [六、 常见疑问与解答 (FAQ)](#六-常见疑问与解答-faq)

---

## 一、 为什么需要前置预处理？

在大规模文献采集中，直接拿海量 DOI 盲目启动浏览器会遭遇三大痛点：
1. **耗时过巨**：如果对 1000 万篇逐篇启动无头浏览器探查落地域名，即便 30 浏览器并发也需耗时数月；
2. **合作期刊误判**：虽然都是 `10.1016`（Elsevier），但约 5% 的学会期刊会跳转到独立合作域名（如 `jandonline.org`、`ajog.org`），盲目按主站处理会导致大规模抓取失败；
3. **假空壳陷阱 (SPA)**：部分站点返回 `HTTP 200`，但 HTML 内容仅为 `<div id="app"></div>` 骨架屏，纯 cURL 下载会把空壳误判为成功，污染数据盘。

**预处理子系统的核心原则**：
- **DuckDB 极速聚合**：千万级数据在 1 秒内收敛为数千个代表族群；
- **轻量 302 嗅探**：仅对代表样本发 HTTP 重定向探测（只读 Location 头），几十秒跑出真实出版商；
- **双视角差分核验**：浏览器提取真值对比 cURL，识破假空壳并判定抓取模式，导出不可变的 `domain_rules.toml`。

---

## 二、 整体架构全景图

```mermaid
flowchart TD
    Input["海量 DOI 输入源<br/>(CSV / TSV / Parquet)"] --> Step1

    subgraph 阶段一: 映射建档 (Resolve)
        Step1["DuckDB 向量化聚类<br/>(秒级收敛为数千个代表族群)"] --> Step2["抽取代表性样本 (sample_doi)"]
        Step2 --> Step3["异步并发 302 重定向嗅探<br/>(读取 Location 与 Meta-Refresh)"]
        Step3 --> Step4[("SQLite 主任务库<br/>· doi_domain_routes (统一映射表)")]
    end

    Step4 --> Step5

    subgraph 阶段二: 模式体检与规则导出 (Audit)
        Step5["提取收敛后的独立真实域名<br/>(大幅收敛至数十至数百个)"] --> Step6["双视角差分核验 (Differential Judge)<br/>· CloakBrowser 真值基准<br/>· cURL 静态与 Cookie 复用比对"]
        Step6 --> Step7["三级漏斗定级判定<br/>· curl_direct<br/>· curl_pool<br/>· browser_only"]
        Step7 --> Step8["固化导出不可变规则快照<br/>domain_rules.toml"]
    end

    Step8 --> Step9

    subgraph 阶段三: 大规模确定性生产 (Run)
        Step9["正式下载工作池 (BrowserPool / cURL)"]
        Step9 --> Action["内存 0 微秒路由分流<br/>生命周期切片包工连续吞吐"]
    end
```

---

## 三、 模块组织结构

| 文件 | 核心类 / 函数 | 职责说明 |
| :--- | :--- | :--- |
| `cluster.py` | `DuckDBClusterer`, `PatternExtractor` | 基于 DuckDB OLAP 引擎的极速向量化聚类与样本抽取器 |
| `resolver.py` | `DomainResolver` | 轻量异步 HTTP 302 跟随与 Elsevier Meta-Refresh 解密嗅探器 |
| `auditor.py` | `DomainAuditor`, `DifferentialJudge` | 浏览器真值基准对比、SPA 客户端假空壳识破器与模式定级裁决器 |
| `store.py` | `PreprocessStore` | SQLite 映射表持久化与只读 `domain_rules.toml` 格式化导出器 |
| `manager.py` | `PreprocessManager` | 顶级编排器，提供高层分阶段与一键流水线调用接口 |
| `models.py` | `DoiSample`, `ResolvedTarget`, `RouteMode` | 基础领域数据模型与路由模式枚举 |

---

## 四、 命令行使用指南

### 1. 一键全自动流水线 (`paperlite preprocess`)
适合新批次从零开始，全自动跑完整套预处理流水线并导出规则文件：
```bash
paperlite preprocess --urls my_dois.csv --out-map db/my_tasks.db --out-rules domain_rules.toml --concurrency 6
```
**参数说明**：
* `--urls`：输入的 CSV 文件路径（含 `doi` 列或 `url` 列）；
* `--out-map`：输出的目标 SQLite 任务库文件，默认 `doi_routes.db`；
* `--out-rules`：最终导出的规则配置文件名，默认 `domain_rules.toml`；
* `--concurrency`：轻量嗅探并发数（直连推荐 4~6，带代理池推荐 30~50）；
* `--proxy`：可选，HTTP/SOCKS5 代理地址。

---

### 2. 阶段一：仅映射建档 (`paperlite resolve`)
适合先跑完族群聚类与落地域名嗅探，稍后再做体检：
```bash
paperlite resolve --urls my_dois.csv --out-map db/my_tasks.db --concurrency 6
```
* **特点**：支持断点续跑，已经嗅探过的族群模式自动 0 毫秒跳过。
* **参数 `--limit N`**：可选，仅嗅探前 N 个代表族群，用于小批量快速试跑。

---

### 3. 阶段二：仅反爬体检与规则生成 (`paperlite audit`)
从已经嗅探建档的 SQLite 库中提取去重后的独立域名，逐一进行真值比对并生成 `domain_rules.toml`：
```bash
paperlite audit --map db/my_tasks.db --out-rules domain_rules.toml
```
* **参数 `--limit N`**：可选，仅体检前 N 个最高频的独立出版商域名；
* **为什么执行很快**：即使输入有数百万条 DOI、数千个期刊族群，去重后的独立主站通常只有几个到几十个，体检通常在几分钟内即可完成。

---

## 五、 `domain_rules.toml` 规范说明

生成的 `domain_rules.toml` 是纯文本、人类可读的不可变配置：

```toml
# PaperLite Auto-Scheduler 自动生成的不可变路由快照
# 生成时间: 2026-11-14 16:30:00

[metadata]
total_domains = 5
curl_direct_count = 1
curl_pool_count = 1
browser_only_count = 3
generated_from = "10_1016_high_priority.db"

# 纯 cURL 直通模式（无盾、页面完全静态直出，性能极高）
[modes.curl_direct]
domains = [
    "static-academic.org",
]

# cURL 身份池复用模式（有盾，但 cf_clearance 可被 cURL 复用）
[modes.curl_pool]
domains = [
    "dl.acm.org",
]

# 强制无头浏览器模式（动态行为 WAF、不发静态 clearance、或前端 SPA 渲染假空壳）
[modes.browser_only]
domains = [
    "www.sciencedirect.com",
    "cell.com",
    "celltherapyjournal.org",
]

# 手动覆盖区（允许用户人工强制指定某个站点的行为，优先级最高）
[overrides]
# "special-domain.com" = "browser_only"
```

---

## 六、 常见疑问与解答 (FAQ)

### Q1: 为什么刚跑完 `resolve` 时，`domain_rules.toml` 里面是空的或只有 0 个域名？
* **原因**：`paperlite resolve` 是**阶段一**命令，它的工作是把 DOI 聚类并嗅探落地域名，结果写在数据库的 `doi_domain_routes` 表里；
* **解决**：`domain_rules.toml` 是由**阶段二**的 `paperlite audit` 命令（或一键命令 `paperlite preprocess`）生成的。只要运行 `paperlite audit --map <你的db>`，就会立刻对收敛出的域名进行体检并输出完整的规则表。

### Q2: 为什么 3,500 个期刊族群，体检时不需要开 3,500 个浏览器？
* **原因**：3,547 个是期刊族群小类代码（如 `j.cej`, `j.memsci`），经过阶段一的 302 跳转嗅探后，**98% 以上的族群全部汇聚到了同一个域名（如 `www.sciencedirect.com`）**；
* 阶段二提取的是**去重后的唯一独立域名**，因此只需体检不到 10 个独立域名即可覆盖 95% 以上的论文量。

### Q3: 生成的 `domain_rules.toml` 包含代理密码等敏感信息吗？
* **完全不包含**：`domain_rules.toml` 仅记录域名到抓取模式的映射关系（`curl_direct` / `curl_pool` / `browser_only`），不包含任何代理账号、密码、Token 或连接签名，可放心归档。
