import tempfile
import unittest
from pathlib import Path

from paperlite.preprocess.auditor import DifferentialJudge
from paperlite.preprocess.cluster import DuckDBClusterer, PatternExtractor
from paperlite.preprocess.models import (
    DomainAuditResult,
    GroundTruth,
    ResolvedTarget,
    RouteMode,
)
from paperlite.preprocess.store import PreprocessStore


class TestPreprocess(unittest.TestCase):

    def test_pattern_extractor(self):
        # 1. Elsevier 新式与旧式
        p1, k1 = PatternExtractor.extract("10.1016/j.ajog.2022.01.235")
        self.assertEqual(p1, "10.1016")
        self.assertEqual(k1, "10.1016/j.ajog")

        p2, k2 = PatternExtractor.extract("10.1016/0002-9378(78)90501-x")
        self.assertEqual(p2, "10.1016")
        self.assertEqual(k2, "10.1016/0002-9378")

        # 2. Wiley
        p3, k3 = PatternExtractor.extract("10.1002/adma.202301234")
        self.assertEqual(p3, "10.1002")
        self.assertEqual(k3, "10.1002/adma")

        # 3. ACM
        p4, k4 = PatternExtractor.extract("10.1145/100348.100354")
        self.assertEqual(p4, "10.1145")
        self.assertEqual(k4, "10.1145")

    def test_differential_judge_spa_detection(self):
        # 模拟浏览器真实基准
        gt = GroundTruth(
            url="https://example.com/article/1",
            host="example.com",
            title="Real Paper Title About Deep Learning",
            visible_text_len=15000,
            abstract_anchor="deep learning approaches demonstrated superior performance",
        )

        # 场景 A: SPA 假空壳 (状态码 200，但标题是 Loading，正文空)
        spa_body = "<html><head><title>Loading...</title></head><body><div id='app'></div></body></html>"
        ok, score, reason = DifferentialJudge.evaluate(gt, 200, {}, spa_body)
        self.assertFalse(ok)
        self.assertIn("占位符", reason)

        # 场景 B: SPA 假空壳 (状态码 200，正文字数过少且缺少核心摘要)
        sparse_body = "<html><head><title>Example Journal</title></head><body>Welcome to the journal</body></html>"
        ok, score, reason = DifferentialJudge.evaluate(gt, 200, {}, sparse_body)
        self.assertFalse(ok)

        # 场景 C: 真实页面 (状态码 200，有真标题，包含摘要关键词，文本量充实)
        real_body = (
            "<html><head><title>Real Paper Title About Deep Learning - Science</title></head>"
            "<body><h1>Real Paper Title</h1><p>Our deep learning approaches demonstrated superior performance across all benchmarks...</p>"
            + "<p>More content...</p>" * 100
            + "</body></html>"
        )
        ok, score, reason = DifferentialJudge.evaluate(gt, 200, {}, real_body)
        self.assertTrue(ok)
        self.assertEqual(score, 1.0)

    def test_preprocess_store_and_toml_export(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_p = Path(tmpdir) / "test_routes.db"
            toml_p = Path(tmpdir) / "test_rules.toml"

            store = PreprocessStore(str(db_p))

            # 1. 保存解析目标
            t1 = ResolvedTarget(
                pattern_key="10.1016/j.ajog",
                sample_doi="10.1016/j.ajog.1",
                real_host="www.ajog.org",
                final_url="https://www.ajog.org/article/1",
            )
            t2 = ResolvedTarget(
                pattern_key="10.1145",
                sample_doi="10.1145/1",
                real_host="dl.acm.org",
                final_url="https://dl.acm.org/doi/1",
            )
            store.save_resolved_targets([t1, t2])

            existing = store.get_existing_patterns()
            self.assertIn("10.1016/j.ajog", existing)
            self.assertIn("10.1145", existing)

            # 2. 提取独立域名
            domains = store.get_unique_domains()
            domain_names = [d[0] for d in domains]
            self.assertIn("www.ajog.org", domain_names)
            self.assertIn("dl.acm.org", domain_names)

            # 3. 保存体检结果
            r1 = DomainAuditResult(domain="www.ajog.org", mode=RouteMode.BROWSER_ONLY, reason="Cloudflare")
            r2 = DomainAuditResult(domain="dl.acm.org", mode=RouteMode.CURL_POOL, reason="cf_clearance复用")
            store.save_audit_results([r1, r2])

            # 4. 导出 TOML
            exported = store.export_to_toml(str(toml_p))
            self.assertTrue(exported.exists())

            # 5. 加载 TOML 路由缓存
            cache = PreprocessStore.load_route_cache(str(toml_p))
            self.assertEqual(cache.get("www.ajog.org"), "browser_only")
            self.assertEqual(cache.get("dl.acm.org"), "curl_pool")

    def test_duckdb_clusterer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "test_dois.csv"
            out_sqlite = Path(tmpdir) / "test_out.db"
            csv_path.write_text(
                "doi\n"
                "10.1016/j.cej.2025.1001\n"
                "10.1016/j.cej.2025.1002\n"
                "10.1016/s2212-2672(24)00001-1\n"
                "10.1002/adma.202300001\n"
                "10.1109/TPAMI.2024.00001\n"
            )
            clusterer = DuckDBClusterer(str(csv_path))
            samples = clusterer.cluster_and_sample(out_sqlite=str(out_sqlite))

            self.assertIn("10.1016/j.cej", samples)
            self.assertIn("10.1016/s2212-2672", samples)
            self.assertIn("10.1002/adma", samples)
            self.assertIn("10.1109", samples)

            # 验证回写 SQLite 统一表 doi_domain_routes
            store = PreprocessStore(str(out_sqlite))
            clusters = store.get_cluster_samples()
            self.assertEqual(len(clusters), 4)
            self.assertEqual(clusters["10.1016/j.cej"].doi, "10.1016/j.cej.2025.1001")

            # 初始状态下尚未嗅探 real_host，已有 pattern 集合应为空
            self.assertEqual(len(store.get_existing_patterns()), 0)

            # 嗅探解析后回填真实落地域名
            target = ResolvedTarget(
                pattern_key="10.1016/j.cej",
                sample_doi="10.1016/j.cej.2025.1001",
                real_host="www.sciencedirect.com",
                final_url="https://www.sciencedirect.com/science/article/pii/S138589472500001X",
            )
            store.save_resolved_targets([target])

            # 验证解析后的集合与字段完整性 (既有聚类 paper_count，又有解析后的 real_host)
            existing = store.get_existing_patterns()
            self.assertIn("10.1016/j.cej", existing)

            with store._get_conn() as conn:
                row = conn.execute(
                    "SELECT paper_count, real_host, sample_url FROM doi_domain_routes WHERE pattern_key = ?",
                    ("10.1016/j.cej",),
                ).fetchone()
                self.assertEqual(row["paper_count"], 2)  # 2 条 10.1016/j.cej
                self.assertEqual(row["real_host"], "www.sciencedirect.com")
                self.assertTrue(row["sample_url"].startswith("https://doi.org/"))

    def test_differential_judge_case_insensitive_waf(self):
        gt = GroundTruth(
            url="https://example.com/article/1",
            host="example.com",
            title="Real Paper Title",
            visible_text_len=15000,
            abstract_anchor="real abstract content",
        )
        ok, score, reason = DifferentialJudge.evaluate(gt, 200, {"Cf-Mitigated": "challenge"}, "content")
        self.assertFalse(ok)
        self.assertIn("WAF", reason)


if __name__ == "__main__":
    unittest.main()
