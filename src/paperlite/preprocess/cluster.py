#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite.preprocess.cluster
────────────────────────────
亿级 DOI 数据的 DuckDB 极速向量化聚类与代表性样本抽样器。
基于 DuckDB OLAP 列式引擎与纯 SQL 聚合，秒级完成千万至亿级数据的族群大类收敛与抽样。
"""

import datetime
import logging
import os
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

import duckdb

from paperlite.preprocess.models import DoiSample

logger = logging.getLogger("paperlite.preprocess.cluster")


class PatternExtractor:
    """学术 DOI 族群特征规则定义（提供单条 DOI 快速提取能力）"""

    _RE_ELSEVIER_JOURNAL = re.compile(r"^(j\.[a-z0-9_]+)\.", re.IGNORECASE)
    _RE_ELSEVIER_S_ISSN  = re.compile(r"^(s[0-9]{4}-?[0-9]{3}[0-9x])", re.IGNORECASE)
    _RE_ELSEVIER_OLD_ISSN= re.compile(r"^([0-9]{4}-?[0-9]{3}[0-9x])", re.IGNORECASE)
    _RE_WILEY_JOURNAL    = re.compile(r"^([a-z0-9_]+)\.", re.IGNORECASE)

    @classmethod
    def extract(cls, doi_or_url: str) -> Tuple[str, str]:
        raw = doi_or_url.strip().lower()
        doi = cls._clean_doi_from_url(raw)
        if not doi or "/" not in doi:
            return "generic", raw[:64]

        prefix, _, suffix = doi.partition("/")

        if prefix == "10.1016":
            m_j = cls._RE_ELSEVIER_JOURNAL.match(suffix)
            if m_j:
                return prefix, f"{prefix}/{m_j.group(1)}"
            m_s = cls._RE_ELSEVIER_S_ISSN.match(suffix)
            if m_s:
                return prefix, f"{prefix}/{m_s.group(1)}"
            m_old = cls._RE_ELSEVIER_OLD_ISSN.match(suffix)
            if m_old:
                return prefix, f"{prefix}/{m_old.group(1)}"
            first_token = suffix.split(".")[0]
            if first_token:
                return prefix, f"{prefix}/{first_token}"
            return prefix, f"{prefix}/generic"

        if prefix == "10.1002":
            m_w = cls._RE_WILEY_JOURNAL.match(suffix)
            if m_w:
                return prefix, f"{prefix}/{m_w.group(1)}"

        return prefix, prefix

    @staticmethod
    def _clean_doi_from_url(raw: str) -> str:
        if "doi.org/" in raw:
            return raw.split("doi.org/", 1)[1].strip()
        if raw.startswith("10."):
            return raw
        m = re.search(r"\b(10\.\d{4,9}/[^\s?#]+)", raw)
        return m.group(1) if m else raw


class DuckDBClusterer:
    """
    DuckDB 极速向量化聚类抽样器。
    支持直接处理千万至亿级 CSV、TSV、Parquet 或现有 SQLite 库。
    """

    def __init__(self, source_path: str, doi_column: str = "doi", url_column: str = "url"):
        self.source_path = str(Path(source_path).resolve())
        self.doi_column = doi_column
        self.url_column = url_column

    def cluster_and_sample(self, out_sqlite: Optional[str] = None) -> Dict[str, DoiSample]:
        """
        利用 DuckDB 向量化 SQL 极速聚合聚类并抽取种子样本。
        若指定 out_sqlite，则直接原子回写至 SQLite 的 doi_clusters 表。
        返回 Dict[pattern_key, DoiSample]。
        """
        import time
        t0 = time.time()
        logger.info(f"⚡️ 启动 DuckDB 极速聚类引擎: {self.source_path}")

        con = duckdb.connect()

        # 1. 确定数据源与列名表达式
        src_lower = self.source_path.lower()
        if src_lower.endswith(".db") or src_lower.endswith(".sqlite"):
            con.execute(f"ATTACH '{self.source_path}' AS in_sqlite (TYPE SQLITE);")
            source_table = "in_sqlite.paper_pdf_resources"
            col_expr = "doi"
        elif src_lower.endswith(".parquet"):
            source_table = f"read_parquet('{self.source_path}')"
            cols = [r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {source_table} LIMIT 0").fetchall()]
            col_expr = self.doi_column if self.doi_column.lower() in cols else (
                self.url_column if self.url_column.lower() in cols else cols[0]
            )
        else:
            # 默认视为 CSV / TSV，开启容错与全部字符串解析
            source_table = f"read_csv('{self.source_path}', header=true, all_varchar=true, ignore_errors=true)"
            cols = [r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {source_table} LIMIT 0").fetchall()]
            if self.doi_column.lower() in cols:
                col_expr = self.doi_column
            elif self.url_column.lower() in cols:
                col_expr = self.url_column
            elif "pdf_url" in cols:
                col_expr = "pdf_url"
            else:
                col_expr = cols[0]

        logger.info(f"DuckDB 选定聚类列表达式: {col_expr}")

        # 2. 向量化聚类与抽样 SQL（支持多出版商大类与二级期刊）
        cluster_sql = f"""
        WITH cleaned AS (
            SELECT 
                coalesce(nullif(regexp_extract(lower({col_expr}), '(10\\.[0-9]{{4,9}}/[^\\s?#]+)', 1), ''), lower({col_expr})) AS doi
            FROM {source_table}
            WHERE {col_expr} IS NOT NULL AND {col_expr} != ''
        ),
        classified AS (
            SELECT 
                doi,
                CASE
                    -- 1. Elsevier (10.1016) 二级期刊/ISSN 细粒度归纳
                    WHEN doi LIKE '10.1016/%' THEN
                        regexp_extract(doi, '^(10\\.1016/(?:j\\.[a-z0-9_-]+|s[0-9a-z_-]+|[a-z0-9_-]+))', 1)
                    -- 2. Wiley (10.1002) 二级期刊归纳
                    WHEN doi LIKE '10.1002/%' THEN
                        coalesce(nullif(regexp_extract(doi, '^(10\\.1002/[a-z0-9_-]+)\\.', 1), ''), regexp_extract(doi, '^(10\\.1002/[^/]+)', 1))
                    -- 3. 其他所有标准出版商 (10.1109, 10.1038, 10.1145 等)
                    ELSE
                        regexp_extract(doi, '^(10\\.[0-9]+)', 1)
                END AS pattern_key,
                split_part(doi, '/', 1) AS publisher_doi
            FROM cleaned
            WHERE doi LIKE '10.%'
        )
        SELECT 
            pattern_key,
            publisher_doi,
            count(*) AS paper_count,
            first(doi) AS sample_doi,
            'https://doi.org/' || first(doi) AS sample_url
        FROM classified
        WHERE pattern_key IS NOT NULL AND pattern_key != ''
        GROUP BY 1, 2
        ORDER BY paper_count DESC;
        """

        rows = con.execute(cluster_sql).fetchall()
        t1 = time.time()
        logger.info(f"🎉 DuckDB 聚类抽样完成！耗时: {t1 - t0:.2f} 秒，共提炼出 {len(rows)} 个独立族群分类")

        # 3. 若指定了输出 SQLite 库，直接批量写入 doi_clusters 表
        if out_sqlite:
            out_p = str(Path(out_sqlite).resolve())
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                con.execute(f"ATTACH '{out_p}' AS out_sqlite (TYPE SQLITE);")
                con.execute("""
                    CREATE TABLE IF NOT EXISTS out_sqlite.doi_clusters (
                        pattern_key TEXT PRIMARY KEY,
                        publisher_doi TEXT,
                        paper_count BIGINT,
                        sample_doi TEXT,
                        sample_url TEXT,
                        updated_at TEXT
                    );
                """)
                # 写入聚合结果
                con.executemany("""
                    INSERT OR REPLACE INTO out_sqlite.doi_clusters 
                    (pattern_key, publisher_doi, paper_count, sample_doi, sample_url, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?);
                """, [(r[0], r[1], r[2], r[3], r[4], now_str) for r in rows])
                logger.info(f"✅ 族群分类已成功持久化至 SQLite: {out_p} (共 {len(rows)} 行)")
            except Exception as e:
                logger.warning(f"写入 SQLite doi_clusters 表时触发警告: {e}")

        # 4. 组装为高层 DoiSample 对象供下游调度
        samples: Dict[str, DoiSample] = {}
        for r in rows:
            pattern_key = r[0]
            publisher_doi = r[1]
            sample_doi = r[3]
            sample_url = r[4]
            samples[pattern_key] = DoiSample(
                doi=sample_doi,
                pattern_key=pattern_key,
                publisher_doi=publisher_doi,
                raw_url=sample_url,
            )

        con.close()
        return samples


# 保持向后兼容性别名
StreamClusterer = DuckDBClusterer
