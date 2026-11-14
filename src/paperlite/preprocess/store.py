#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite.preprocess.store
──────────────────────────
预处理中间态数据库 (SQLite) 与最终路由配置 (domain_rules.toml) 的读写仓储。
支持幂等断点续跑、极速跳过已完成项、以及导出不可变的只读规则文件。
"""

import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from paperlite.preprocess.models import DoiSample, DomainAuditResult, ResolvedTarget, RouteMode

logger = logging.getLogger("paperlite.preprocess.store")


class PreprocessStore:
    """预处理数据存储与 TOML 规则导出管理器"""

    def __init__(self, db_path: str = "doi_routes.db"):
        self.db_path = Path(db_path).resolve()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """初始化 SQLite 模式表"""
        with self._get_conn() as conn:
            conn.executescript(
                """
                -- 0. 经 DuckDB 聚合抽样的 DOI 族群大类与代表样本表
                CREATE TABLE IF NOT EXISTS doi_clusters (
                    pattern_key TEXT PRIMARY KEY,
                    publisher_doi TEXT,
                    paper_count BIGINT,
                    sample_doi TEXT,
                    sample_url TEXT,
                    updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_clusters_count ON doi_clusters (paper_count DESC);

                -- 1. DOI 族群模式与真实落地域名映射表
                CREATE TABLE IF NOT EXISTS doi_domain_routes (
                    pattern_key TEXT PRIMARY KEY,
                    publisher_doi TEXT,
                    sample_doi TEXT,
                    real_host TEXT,
                    final_url TEXT,
                    updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_routes_host ON doi_domain_routes (real_host);

                -- 2. 独立域名的反爬体检画像与模式裁决表
                CREATE TABLE IF NOT EXISTS domain_profiles (
                    domain TEXT PRIMARY KEY,
                    mode TEXT,
                    is_spa_shell INTEGER,
                    fidelity_score REAL,
                    sample_url TEXT,
                    reason TEXT,
                    updated_at TEXT
                );
                """
            )

    def save_resolved_targets(self, targets: List[ResolvedTarget]) -> None:
        """批量保存或更新已解析的落地域名"""
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_conn() as conn:
            conn.executemany(
                """
                INSERT INTO doi_domain_routes 
                (pattern_key, publisher_doi, sample_doi, real_host, final_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(pattern_key) DO UPDATE SET
                    real_host = excluded.real_host,
                    final_url = excluded.final_url,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        t.pattern_key,
                        t.pattern_key.split("/")[0],
                        t.sample_doi,
                        t.real_host,
                        t.final_url,
                        now,
                    )
                    for t in targets
                    if t.real_host != "error"
                ],
            )
        logger.info(f"成功持久化 {len(targets)} 条族群域名映射至 {self.db_path.name}")

    def get_existing_patterns(self) -> set[str]:
        """获取已经解析过的 pattern_key 集合 (支持 0 秒跳过)"""
        with self._get_conn() as conn:
            cur = conn.execute("SELECT pattern_key FROM doi_domain_routes WHERE real_host != 'error'")
            return {row["pattern_key"] for row in cur.fetchall()}

    def get_cluster_samples(self) -> Dict[str, DoiSample]:
        """从 doi_clusters 表中读取已持久化的代表性样本"""
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT pattern_key, publisher_doi, sample_doi, sample_url FROM doi_clusters"
            )
            return {
                row["pattern_key"]: DoiSample(
                    doi=row["sample_doi"],
                    pattern_key=row["pattern_key"],
                    publisher_doi=row["publisher_doi"],
                    raw_url=row["sample_url"],
                )
                for row in cur.fetchall()
            }

    def get_unique_domains(self) -> List[Tuple[str, str]]:
        """
        从解析成功的映射表中提取去重后的独立域名，以及该域名的代表性样本 URL。
        返回如: [('www.sciencedirect.com', 'https://...'), ('dl.acm.org', 'https://...')]
        """
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                SELECT real_host, final_url 
                FROM doi_domain_routes 
                WHERE real_host != 'error' AND real_host != ''
                GROUP BY real_host
                ORDER BY COUNT(*) DESC
                """
            )
            return [(row["real_host"], row["final_url"]) for row in cur.fetchall()]

    def save_audit_results(self, results: List[DomainAuditResult]) -> None:
        """保存独立域名的反爬体检与模式判定结果"""
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_conn() as conn:
            conn.executemany(
                """
                INSERT INTO domain_profiles
                (domain, mode, is_spa_shell, fidelity_score, sample_url, reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    mode = excluded.mode,
                    is_spa_shell = excluded.is_spa_shell,
                    fidelity_score = excluded.fidelity_score,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        r.domain,
                        r.mode.value,
                        1 if r.is_spa_shell else 0,
                        r.fidelity_score,
                        r.sample_url,
                        r.reason,
                        now,
                    )
                    for r in results
                ],
            )
        logger.info(f"成功持久化 {len(results)} 个域名的体检裁决结果至 {self.db_path.name}")

    def export_to_toml(self, toml_path: str = "domain_rules.toml") -> Path:
        """将体检完成的规则导出为纯文本、人类可读的 domain_rules.toml"""
        out_p = Path(toml_path).resolve()

        with self._get_conn() as conn:
            cur = conn.execute("SELECT domain, mode, reason FROM domain_profiles ORDER BY domain")
            rows = cur.fetchall()

        direct_domains = []
        pool_domains = []
        browser_domains = []

        for row in rows:
            dom = row["domain"]
            mode = row["mode"]
            if mode == RouteMode.CURL_DIRECT.value:
                direct_domains.append(dom)
            elif mode == RouteMode.CURL_POOL.value:
                pool_domains.append(dom)
            else:
                browser_domains.append(dom)

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        toml_content = f"""# PaperLite Auto-Scheduler 自动生成的不可变路由快照
# 生成时间: {now_str}
# 统计: 共体检 {len(rows)} 个出版商域名 (cURL直连: {len(direct_domains)}, cURL身份池: {len(pool_domains)}, 强制浏览器: {len(browser_domains)})

[metadata]
total_domains = {len(rows)}
curl_direct_count = {len(direct_domains)}
curl_pool_count = {len(pool_domains)}
browser_only_count = {len(browser_domains)}
generated_from = "{self.db_path.name}"

# 纯 cURL 直通模式（无盾、页面完全静态直出，性能极高）
[modes.curl_direct]
domains = [
{self._format_toml_list(direct_domains)}
]

# cURL 身份池复用模式（有盾，但 cf_clearance 可被 cURL 复用）
[modes.curl_pool]
domains = [
{self._format_toml_list(pool_domains)}
]

# 强制无头浏览器模式（动态行为 WAF、不发静态 clearance、或前端 SPA 渲染假空壳）
[modes.browser_only]
domains = [
{self._format_toml_list(browser_domains)}
]

# 手动覆盖区（允许用户人工强制指定某个站点的行为，优先级最高）
[overrides]
# "example-site.com" = "browser_only"
"""
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(toml_content)

        logger.info(f"🎉 规则表成功固化导出至: {out_p}")
        return out_p

    @staticmethod
    def _format_toml_list(items: list[str]) -> str:
        if not items:
            return ""
        return "\n".join(f'    "{item}",' for item in items)

    @classmethod
    def load_route_cache(cls, toml_path: str = "domain_rules.toml") -> Dict[str, str]:
        """供大生产阶段调度的快速只读加载器，返回 {domain: mode_string}"""
        import tomllib

        p = Path(toml_path).resolve()
        if not p.exists():
            return {}

        with open(p, "rb") as f:
            data = tomllib.load(f)

        cache = {}
        modes = data.get("modes", {})
        for dom in modes.get("curl_direct", {}).get("domains", []):
            cache[dom.lower()] = RouteMode.CURL_DIRECT.value
        for dom in modes.get("curl_pool", {}).get("domains", []):
            cache[dom.lower()] = RouteMode.CURL_POOL.value
        for dom in modes.get("browser_only", {}).get("domains", []):
            cache[dom.lower()] = RouteMode.BROWSER_ONLY.value

        # 用户手动覆盖项具有最高优先级
        overrides = data.get("overrides", {})
        for dom, mode in overrides.items():
            cache[dom.lower()] = mode

        return cache
