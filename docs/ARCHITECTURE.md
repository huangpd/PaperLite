# PaperLite 系统架构与数据流转设计全景

本文档全面梳理 PaperLite 系统的软硬件拓扑、分层架构、核心子系统协作逻辑以及全生命周期数据流转模型。

---

## 1. 系统全景架构拓扑图

PaperLite 采用 **“预处理离线裁决 + 在线自适应混合三态调度 + 响应式背压生产”** 的三层复合架构体系：

```mermaid
flowchart TD
    subgraph DataIngestion ["一、数据接入与离线预处理层 (Offline Preprocessing)"]
        RawData["千万/亿级原始数据 (CSV / TSV / Parquet / URL)"]
        Clusterer["DuckDB 向量化聚类器 (DuckDBClusterer)\n- 规则提取出版商与二级期刊 Pattern"]
        Resolver["轻量级落地嗅探器 (DomainResolver)\n- 跟随 302 / Meta-Refresh / LinkingHub 解包"]
        Auditor["双视角差分体检器 (DomainAuditor)\n- CloakBrowser 真值 vs cURL 差分\n- 识破 SPA 空壳 / 识别 WAF 拦截"]
        RoutesDB[("SQLite 映射库\ndoi_domain_routes")]
        RulesTOML["只读路由快照\ndomain_rules.toml"]
        
        RawData --> Clusterer
        Clusterer -->|"代表性样本 (DoiSample)"| Resolver
        Clusterer -.->|"写入聚类统计"| RoutesDB
        Resolver -->|"真实域名 (real_host)"| RoutesDB
        RoutesDB -->|"独立域名列表"| Auditor
        Auditor -->|"模式裁决结果"| RulesTOML
    end

    subgraph CoreStorage ["二、中心持久化与状态存储 (Storage & State Layer)"]
        TaskDB[("任务数据库 (SQLite: TaskDB)\n- paper_pdf_resources (亿级任务表)\n- volume_stops (卷期限流表)\n- 复合索引: (real_host, challenge_count, id)")]
        RedisPool[("身份池与背压协调 (Redis: IdentityStore)\n- ZSet: ready / inuse\n- Hash: identity:*\n- Key: demand:* (下游需求水位)\n- Key: breaker:* (熔断暂停器)")]
    end

    subgraph SchedulerLayer ["三、在线自适应调度器 (AutoScheduler Engine)"]
        Scheduler["AutoScheduler 核心调度控制器\n- 读取 domain_rules.toml\n- 按 real_host 分片调度\n- 运行时健康监测与动态熔断"]
    end

    subgraph ExecutionEngines ["四、三态动态执行引擎 (Hybrid Execution Engines)"]
        subgraph DirectMode ["模式 1: curl_direct (极速直通)"]
            DirectPool["AsyncIO 异步直连下载池\n- 静态无盾/直通站点\n- 代理租约复用与秒级落盘\n- 校验 %PDF- 魔数与原子重命名"]
        end

        subgraph PoolMode ["模式 2: curl_pool (按需身份池复用)"]
            Downloader["下载器 (Downloader)\n- 多线程 curl_cffi\n- 认领 Redis Identity\n- 沿用 Session / 429 退避\n- 实时上报 demand 水位"]
            Producer["生产者 (Producer)\n- 纯 demand 需求驱动\n- 无头浏览器并发过盾\n- 提炼 cf_clearance / UA"]
        end

        subgraph BrowserModule ["模式 3: browser_only (兜底渲染池)"]
            BrowserPool["BrowserPool (真实浏览器池)\n- CloakBrowser 防检测无头环境\n- WebRTC 伪装 + GeoIP 对齐\n- Turnstile 动态交互过盾\n- DOM 快照与原子落盘"]
        end
    end

    subgraph OutputAndMonitor ["五、产出与监控运维层 (Output & Observability)"]
        DiskStorage["本地/分布式存储 (PDFs & HTMLs)\n- .part 临时落盘\n- atomic rename 最终文件"]
        RabbitMQ["RabbitMQ 消息队列\n- paperlite.mon 遥测指标"]
        Monitor["paperlite-monitor 监控控制台\n- 吞吐率 / 成功率 / 熔断告警"]
    end

    %% 关联连线
    RulesTOML --> Scheduler
    TaskDB <-->|"批量认领 (claim_chunk) / 状态完结 (finish)"| Scheduler
    
    Scheduler -->|"路由: curl_direct"| DirectPool
    Scheduler -->|"路由: curl_pool"| Downloader
    Scheduler -->|"路由: browser_only / 熔断降级"| BrowserPool
    
    Downloader <-->|"双向背压 (set_demand / claim)"| RedisPool
    Producer <-->|"根据 demand 生成身份 / 写入"| RedisPool
    
    DirectPool -->|"异常连续质询 (consecutive_blocks>=3)"| Scheduler
    Downloader -->|"连续拒绝触发熔断 (circuit_broken)"| Scheduler
    
    DirectPool --> DiskStorage
    Downloader --> DiskStorage
    BrowserPool --> DiskStorage
    
    DirectPool -.-> RabbitMQ
    Downloader -.-> RabbitMQ
    BrowserPool -.-> RabbitMQ
    RabbitMQ --> Monitor
```

---

## 2. 数据生命周期与状态机转换图

每条论文资源从导入、调度分发、多分支执行到最终落盘的全链路状态流转：

```mermaid
flowchart TD
    Start(["📥 任务导入 (import_urls / backfill_real_hosts)"]) --> PENDING

    subgraph SubPending ["一、待办阶段 (PENDING)"]
        PENDING["待下载任务 (status = 'pending')"]
        VolCheck{"卷期阈值探测\n(_volume_open)"}
        VolBlocked["卷期超限熔断\n(批量标记 inferred=1)"]
        Claimable["可认领任务\n(idx_host_claim 复合索引快速寻址)"]
        
        PENDING --> VolCheck
        VolCheck -->|"连续无 PDF 达到上限"| VolBlocked
        VolCheck -->|"放行"| Claimable
    end

    VolBlocked --> FinalNonPdf

    Claimable -->|"claim_chunk 批量切片认领\n(status = 'running', 写入 claimed_by)"| RouteFork

    subgraph SubRunning ["二、运行与三态执行阶段 (RUNNING)"]
        RouteFork{"根据 real_host\n三态路由分流"}
        
        subgraph ExecDirect ["curl_direct 直连执行"]
            D1["curl_cffi 异步并发 GET"] --> D2{"校验 %PDF- 魔数\n与 HTTP 状态码"}
        end

        subgraph ExecPool ["curl_pool 身份池执行"]
            P1["认领 Redis Identity\n上报 demand 水位"] --> P2["携带 cf_clearance 下载\n处理 429 Retry-After"]
        end

        subgraph ExecBrowser ["browser_only 浏览器执行"]
            B1["CloakBrowser 打开落地页"] --> B2["TurnstileClicker 过盾\n等待 DOM 水合渲染"]
        end

        RouteFork -->|"无盾/纯静态"| D1
        RouteFork -->|"有盾/可复用 clearance"| P1
        RouteFork -->|"高防/SPA/动态风控"| B1
    end

    subgraph ErrorHandling ["三、重试与自愈降级分支 (RETRY & HEAL)"]
        RetryLater["retry_later 放回队列\n(释放 claimed_by, count=True/False)"]
        Downgrade["自愈降级为 browser_only\n(连续质询/拒绝触发熔断)"]
    end

    subgraph SubFinal ["四、终态归宿 (TERMINAL STATUS)"]
        FinalSuccess(["✅ SUCCESS\n.part 原子落盘，保存 PDF / HTML"])
        FinalNonPdf(["⚠️ NON_PDF\n付费墙/摘要页/404/卷期推断 (inferred=1)"])
        FinalFailed(["❌ FAILED\n超过最大挑战次数 / 致命 4xx/5xx"])
    end

    %% 成功路径
    D2 -->|"HTTP 200 + 真实 PDF"| FinalSuccess
    P2 -->|"下载成功"| FinalSuccess
    B2 -->|"渲染完成"| FinalSuccess

    %% Non-PDF 路径
    D2 -->|"HTTP 200 但非 PDF (classify_html)"| FinalNonPdf
    B2 -->|"无全文 / 付费摘要页"| FinalNonPdf

    %% 异常重试与降级
    D2 -->|"连续遇到 WAF 质询 (403/429)"| Downgrade
    P2 -->|"cf_clearance 失效 / 封禁"| Downgrade
    Downgrade --> RetryLater
    RetryLater -->|"重新排队等待"| PENDING

    D2 -->|"网络抖动 / 临时超时"| RetryLater
    P2 -->|"网络断开 (code 56)"| RetryLater
    B2 -->|"渲染超时 / 临时异常"| RetryLater

    %% 失败路径
    D2 -->|"HTTP 404 / 410"| FinalFailed
    P2 -->|"挑战次数耗尽"| FinalFailed
    B2 -->|"达到 max_challenges 上限"| FinalFailed

    %% 样式美化
    classDef success fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px,color:#1b5e20;
    classDef warning fill:#fff3e0,stroke:#ef6c00,stroke-width:2px,color:#e65100;
    classDef danger fill:#ffebee,stroke:#c62828,stroke-width:2px,color:#b71c1c;
    classDef pending fill:#e3f2fd,stroke:#1565c0,stroke-width:2px,color:#0d47a1;

    class FinalSuccess success;
    class FinalNonPdf warning;
    class FinalFailed danger;
    class PENDING,Claimable pending;
```

---

## 3. 三态混合调度与动态自愈熔断机制

PaperLite 创新的核心在于**兼顾极速吞吐与高防穿透**，调度器根据目标出版商的反爬强度自动选择最佳执行路径，并在运行时动态自愈：

| 模式 | 适用场景 | 关键机制与技术栈 | 资源消耗 | 故障与动态降级策略 |
| :--- | :--- | :--- | :--- | :--- |
| **`curl_direct`** | 无 WAF 防御、完全静态直出、公开获取的学术站点 | • `AsyncSession` 高并发协程池<br>• 单 IP 多任务并发分摊<br>• `%PDF-` 二进制魔数校验 | CPU/内存极低，带宽利用率 98%+ | 连续 3 次遇到 WAF 质询 (`consecutive_blocks >= 3`)：<br>立即断开当前批次，重置代理 IP，**动态自愈降级为 `browser_only`**。 |
| **`curl_pool`** | 有 Cloudflare 等 WAF 拦截，但 `cf_clearance` 可被脱机复用的站点 (如 ACM) | • **需求驱动反压机制** (`demand-driven`)<br>• 下游消费者通过 Redis 上报待办水位<br>• 生产者按需对冲启动无头浏览器过盾<br>• 沿用有效身份会话下载，遇 429 遵循 `Retry-After` | 动态弹性伸缩，无任务时 0 浏览器消耗 | 遇到硬性封禁 (`is_cf_denied`) 立即丢弃身份；<br>若 3 个新身份连续被拒 (`circuit_broken`)：<br>**动态自愈降级为 `browser_only`**。 |
| **`browser_only`** | 顶级高防站点 (如 ScienceDirect)、动态交互签名风控、或前端异步渲染 SPA 假空壳页 | • `CloakBrowser` 真实渲染管线<br>• WebRTC 防泄漏 + IP 地理位置对齐<br>• `TurnstileClicker` 自动检测与模拟点击<br>• DOM 结构监测与智能巡检 (`WindowInspector`) | 内存/GPU 消耗较高，单实例并发受限 | 严格限制并发数与标签页生命周期，任务异常时自动释放租约并平滑重启损坏的浏览器上下文。 |

---

## 4. 需求驱动型 Producer-Downloader 双向反压时序图

在 `curl_pool` 模式下，系统彻底摒弃了传统的“盲目预热身份”模式，转由下游实际消费需求驱动生产：

```mermaid
sequenceDiagram
    autonumber
    participant DB as SQLite (TaskDB)
    participant DL as Downloader (消费者)
    participant RD as Redis (IdentityStore)
    participant PR as Producer (生产者)
    participant BR as CloakBrowser (过盾环境)
    participant WAF as 目标站 WAF

    Note over DL,PR: 阶段一：需求上报与反压协同 (Demand-Driven Backpressure)
    DL->>DB: 探测待办数 count_pending_host(host)
    DB-->>DL: 返回待办任务数 (e.g. 1500)
    DL->>RD: 上报需求水位 set_demand(host, 1500)
    
    PR->>RD: 定时轮询需求水位 get_demand(host)
    RD-->>PR: 返回 demand = 1500 (目标 target = min(demand, workers, max))
    
    Note over PR,BR: 阶段二：受控过盾与身份入池 (Controlled Challenge Passing)
    PR->>BR: 并发启动无头浏览器 (不超过 max_browsers 限制)
    BR->>WAF: 访问落地页并触发 Cloudflare 质询
    WAF-->>BR: 弹出 Turnstile 验证码
    BR->>BR: TurnstileClicker 模拟点击通过
    BR-->>PR: 提取 cf_clearance, cookies, UA, fingerprint
    PR->>PR: 离线 cURL 验证探针 (_curl_usable)
    PR->>RD: 将合格 Identity 写入 Redis (ZADD ready, HSET identity:*)

    Note over DL,WAF: 阶段三：身份认领与流式下载 (Claim & Fetch)
    DL->>RD: 认领空闲身份 claim(host, holder)
    RD-->>DL: 租出有效 Identity
    DL->>DB: 认领下载任务 claim(host)
    DB-->>DL: 返回 Task (DOI, URL)
    DL->>WAF: curl_cffi 模拟浏览器真实请求 (携带 clearance)
    
    alt 下载成功 (HTTP 200 + PDF)
        WAF-->>DL: 返回二进制 PDF 流
        DL->>DL: 写入 .part 临时文件，校验无误后原子重命名
        DL->>DB: finish(task, Status.SUCCESS)
        DL->>RD: release(ident) 归还身份继续复用
    else 遇到 HTTP 429 (临时限流)
        WAF-->>DL: HTTP 429 + Retry-After: 30
        DL->>DL: rate_limit_wait 评估身份剩余寿命，单线程安全 sleep(30) 后原身份重试
    else 遇到 Cloudflare 封禁 (Access Denied)
        WAF-->>DL: HTTP 403/429 + Access Denied 页面
        DL->>RD: discard(ident) 永久销毁被拉黑身份
        DL->>DB: retry_later(task) 任务放回队列
        opt 连续被拒超限 (fresh_rejects 达到阈值且无成功)
            DL->>DL: 触发回路熔断 (circuit_broken = True)
            DL-->>DB: 动态将该 host 标记并降级为 browser_only
        end
    end

    Note over DL,PR: 阶段四：任务收尾与自动停机 (Graceful Quiescence)
    DL->>DB: count_pending_host(host) == 0 (所有任务完结)
    DL->>RD: set_demand(host, 0) (需求归零)
    PR->>RD: 探测到 demand 归零
    PR->>PR: target 归零，立刻停止新开浏览器，平滑挂起，资源完全释放
```

---

## 5. 核心模块与文件拓扑一览

```
PaperLite/src/paperlite/
├── cli.py                     # CLI 命令行总入口 (import, run, preprocess, resolve, audit, export, status)
├── config.py                  # TOML 配置解析器 (Paths, Limits, RunConfig, ProxySettings)
├── tasks.py                   # SQLite 核心任务库 (TaskDB: 复合索引认领、卷期限流、事务保证)
├── scheduler.py               # AutoScheduler 自动化调度引擎 (分流 direct, pool, browser 及自愈熔断)
├── consumer.py                # 消费者与下载执行器 (Downloader: 会话维护、PDF魔数校验、429退避)
├── producer.py                # 需求驱动型生产者 (Producer: 浏览器过盾、身份验收、背压控制)
├── store.py                   # Redis 分布式身份池 (IdentityStore: 租约续期、Lua 原子认领、需求水位)
├── browser_pool.py            # 纯浏览器池下载引擎 (BrowserPool: 协程池、标签页限速、DOM快照)
├── browser.py                 # 无头浏览器核心封装 (CloakBrowser 集成、反检测参数、过盾流程)
├── turnstile.py               # Cloudflare Turnstile 自动识别与模拟点击器
├── classify.py                # HTML 页面语义分类器 (精准区分 paywall, landing_page, not_found)
├── proxy.py                   # 代理 IP 轮换与健康体检提取器 (IpSource, Lease, Curl 探测探针)
├── cookies.py                 # CookieJar 序列化与跨组件还原工具
├── logutil.py                 # 结构化 JSON 日志格式化器
│
├── preprocess/                # 预处理离线裁决子系统
│   ├── cluster.py             # DuckDB 亿级 DOI 极速向量化聚类与样本抽样 (DuckDBClusterer)
│   ├── resolver.py            # 轻量级真实落地域名嗅探器 (DomainResolver, Meta-Refresh 解析)
│   ├── auditor.py             # 出版商反爬体检与模式裁决器 (DomainAuditor, DifferentialJudge)
│   ├── store.py               # 预处理 SQLite 中间库与 domain_rules.toml 导出器 (PreprocessStore)
│   ├── manager.py             # 预处理三阶段流水线总编排 (PreprocessManager)
│   └── models.py              # 数据传输对象定义 (DoiSample, ResolvedTarget, DomainAuditResult)
│
└── monitor/                   # 遥测监控与 Dashboard 子系统
    ├── reporter.py            # RabbitMQ 异步指标上报器 (线程隔离、无阻塞)
    ├── schema.py              # 遥测事件数据协议定义
    └── dashboard.py           # 命令行多域名实时监控面板 (paperlite-monitor)
```
