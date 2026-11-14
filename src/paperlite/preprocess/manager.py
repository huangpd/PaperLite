#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite.preprocess.manager
────────────────────────────
预处理阶段的顶级编排管理器 (PreprocessManager)。
对外统一暴露高层 API，支持分步执行 (resolve, audit) 与一键全流水线执行。
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from paperlite.preprocess.auditor import DomainAuditor
from paperlite.preprocess.cluster import DuckDBClusterer
from paperlite.preprocess.resolver import DomainResolver
from paperlite.preprocess.store import PreprocessStore

logger = logging.getLogger("paperlite.preprocess.manager")


class PreprocessManager:
    """预处理流水线顶级编排控制器"""

    @classmethod
    async def run_resolve(
        cls,
        csv_path: str,
        out_db: str = "doi_routes.db",
        proxy: Optional[str] = None,
        concurrency: int = 6,
        limit_patterns: Optional[int] = None,
    ) -> PreprocessStore:
        """
        【阶段一：映射建档】
        从 CSV 流式抽取族群特征并抽样，通过代理温和嗅探真实落地域名，固化到 SQLite。
        支持幂等性：已解析过的族群自动 0 毫秒跳过。
        """
        store = PreprocessStore(out_db)
        existing = store.get_existing_patterns()

        # 1. DuckDB 向量化极速聚类抽样（自动持久化族群表至 SQLite）
        clusterer = DuckDBClusterer(csv_path)
        samples_dict = clusterer.cluster_and_sample(out_sqlite=out_db)

        # 2. 差量过滤已解析项
        unresolved_samples = [
            s for k, s in samples_dict.items() if k not in existing
        ]

        logger.info(
            f"族群聚类统计: 总族群 {len(samples_dict)} 个，已建档 {len(existing)} 个，"
            f"本次需实测嗅探 {len(unresolved_samples)} 个"
        )

        if limit_patterns:
            unresolved_samples = unresolved_samples[:limit_patterns]
            logger.info(f"由于参数限制，本次优先嗅探前 {limit_patterns} 个族群")

        if unresolved_samples:
            # 3. 异步并发温和嗅探
            resolver = DomainResolver(proxy=proxy, concurrency=concurrency)
            resolved_targets = await resolver.resolve_all(unresolved_samples)

            # 4. 持久化至 SQLite
            store.save_resolved_targets(resolved_targets)
        else:
            logger.info("全部族群模式均已在数据库中完成建档，无需新发请求！")

        return store

    @classmethod
    async def run_audit(
        cls,
        in_db: str = "doi_routes.db",
        out_toml: str = "domain_rules.toml",
        proxy: Optional[str] = None,
        limit_domains: Optional[int] = None,
    ) -> Path:
        """
        【阶段二：模式体检与裁决】
        从映射数据库中提取收敛后的独立真实域名，
        逐一执行双视角差分核验，识破 SPA 假空壳页，定级 curl vs browser，并导出 toml 规则。
        """
        store = PreprocessStore(in_db)
        domains_with_samples = store.get_unique_domains()

        logger.info(f"从映射数据库提取到独立真实出版商域名: {len(domains_with_samples)} 个")

        if limit_domains:
            domains_with_samples = domains_with_samples[:limit_domains]
            logger.info(f"由于参数限制，本次优先体检前 {limit_domains} 个域名")

        auditor = DomainAuditor(proxy=proxy)
        audit_results = []

        # 逐个对收敛出的独立域名进行实测体检
        for idx, (dom, sample_url) in enumerate(domains_with_samples, 1):
            logger.info(f"[{idx}/{len(domains_with_samples)}] 开始体检独立域名: {dom}")
            res = await auditor.audit_domain(dom, sample_url)
            audit_results.append(res)

        # 保存体检结果
        store.save_audit_results(audit_results)

        # 导出不可变 toml 规则快照
        toml_path = store.export_to_toml(out_toml)
        return toml_path

    @classmethod
    async def run_pipeline(
        cls,
        csv_path: str,
        out_db: str = "doi_routes.db",
        out_toml: str = "domain_rules.toml",
        proxy: Optional[str] = None,
        concurrency: int = 6,
    ) -> Path:
        """一键全自动执行阶段一与阶段二"""
        logger.info("==================================================")
        logger.info("启动 PaperLite 预处理全自动流水线")
        logger.info("==================================================")

        # 阶段一
        store = await cls.run_resolve(
            csv_path=csv_path, out_db=out_db, proxy=proxy, concurrency=concurrency
        )

        # 阶段二
        toml_p = await cls.run_audit(
            in_db=out_db, out_toml=out_toml, proxy=proxy
        )

        logger.info("==================================================")
        logger.info(f"🎉 预处理流水线全部执行完毕！规则表: {toml_p}")
        logger.info("==================================================")
        return toml_p
