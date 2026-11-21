"""任务队列 worker 侧与巡检（paperlite.taskq.source / title_dup）。队列 SQL 本身见 test_taskq.py。"""
import unittest

from paperlite.taskq.title_dup import WindowInspector
from paperlite.rules.tasks import Status, UrlType
from tests.pgutil import execute, make_source, needs_pg, rows as pg_rows




@needs_pg
class InspectTest(unittest.TestCase):
    """巡检（全批次，paperlite inspect）：同标题重复触发 → doi/url 规则 → 已采之外的 pending 记跳过、不再领取。"""

    def setUp(self):
        self.src = make_source(self, chunk_size=100)
        self.q = self.src.q

    def _add(self, doi, url, status=None, title=None):
        self.q.import_urls([{"url": url, "doi": doi}])
        if status:                              # 直接写成最终结果（模拟已采）
            execute(self.src, "UPDATE pl_tasks SET status = %s, title = %s WHERE url = %s", (status, title, url))

    def test_dup_title_induces_rule_and_flags_pending(self):
        host = "https://ex.org/pii/"
        for i in range(1, 13):                   # 12 篇封面（同标题），doi/url 连续
            self._add(f"10.1/cov.{i:03d}", f"{host}COV{i:03d}", "success", "Outside Front Cover")
        for i in range(1, 6):                    # 5 篇真文章，不同标题
            self._add(f"10.1/art.{i}", f"{host}ART{i}", "success", f"Real paper {i}")
        for i in range(13, 21):                  # 8 篇未采封面（doi/url 同前缀）
            self._add(f"10.1/cov.{i:03d}", f"{host}COV{i:03d}")
        for i in range(6, 9):                    # 3 篇未采真文章
            self._add(f"10.1/art.{i}", f"{host}ART{i}")
        self.q.build_chunks(100)

        from paperlite.taskq.title_dup import inspect
        rules, flagged = inspect(self.q, trigger=10, min_precision=0.98, min_support=10)
        self.assertEqual((rules, flagged), (1, 8))           # 一条规则，跳过 8 篇未采封面
        self.assertEqual(self.q.skip_counts(), {"title_dup": 8})
        # 领取只拿得到真文章，封面被跳过
        got = set()
        while (t := self.src.claim("w")) is not None:
            got.add(t.doi)
            self.src.finish(t, Status.SUCCESS)
        self.assertEqual(got, {"10.1/art.6", "10.1/art.7", "10.1/art.8"})
        # 规则落库，可清除；清除后放回的任务重新可领
        rules = self.q.skip_rules()
        self.assertEqual([(r["scope"], r["flagged"]) for r in rules], [("batch", 8)])
        self.assertEqual(self.q.clear_skips("title_dup"), 8)
        self.assertEqual((self.q.skip_counts(), self.q.skip_rules()), ({}, []))
        self.assertTrue(self.q.has_work())

    def test_scattered_covers_not_flagged_by_precision_guard(self):
        # 封面和真文章 doi/url 交错，唯一公共前缀会命中真文章 → precision 不达标 → 不标记
        for i in range(1, 13):
            self._add(f"10.1/j.{2*i:03d}", f"https://ex.org/p/{2*i:03d}", "success", "Outside Front Cover")
            self._add(f"10.1/j.{2*i+1:03d}", f"https://ex.org/p/{2*i+1:03d}", "success", f"Real {i}")
        for i in range(100, 110):               # 未采，和上面同刊前缀
            self._add(f"10.1/j.{i}", f"https://ex.org/p/{i}")
        from paperlite.taskq.title_dup import inspect
        rules, flagged = inspect(self.q, trigger=10, min_precision=0.98, min_support=10)
        self.assertEqual((rules, flagged), (0, 0))           # 护栏生效，不误标
        self.assertEqual(self.q.skip_counts(), {})


class WindowInspectorTest(unittest.TestCase):
    """实时巡检只处理新结果和有限窗口，不读取历史成功表。"""

    class FakeDB:
        def __init__(self):
            self.count_calls = 0
            self.flag_calls = []

        def crawled_titles(self):
            raise AssertionError("实时巡检不应扫描历史任务")

        def count_pending_matching(self, doi_prefix, url_prefix):
            self.count_calls += 1
            return 2

        def flag_suspects(self, doi_prefix, url_prefix, title, support, precision):
            self.flag_calls.append((doi_prefix, url_prefix, title, support, precision))
            return 2

    def test_does_not_repeat_same_rule_without_historical_scan(self):
        db = self.FakeDB()
        watcher = WindowInspector(db, trigger=3, min_support=3, window_size=5)
        base = "https://example.org/article/"
        self.assertEqual(watcher.observe("10.1/cov.1", base + "1", "Outside Front Cover"), (0, 0))
        self.assertEqual(watcher.observe("10.1/cov.2", base + "2", "Outside Front Cover"), (0, 0))
        self.assertEqual(watcher.observe("10.1/cov.3", base + "3", "Outside Front Cover"), (1, 2))
        self.assertEqual(watcher.observe("10.1/cov.4", base + "4", "Outside Front Cover"), (0, 0))
        self.assertEqual(len(db.flag_calls), 1)               # 相同规则不会重复写入
        self.assertGreater(db.count_calls, 0)

    def test_new_sample_can_expand_rule(self):
        db = self.FakeDB()
        watcher = WindowInspector(db, trigger=3, min_support=3, window_size=10)
        base = "https://example.org/article/"
        watcher.observe("10.1/journal(23)001", base + "23001", "Cover")
        watcher.observe("10.1/journal(23)002", base + "23002", "Cover")
        self.assertEqual(watcher.observe("10.1/journal(23)003", base + "23003", "Cover"), (1, 2))
        # 新卷期样本让公共前缀从 (23) 扩到 (2；这是新规则，应允许再次标记。
        self.assertEqual(watcher.observe("10.1/journal(24)001", base + "24001", "Cover"), (1, 2))
        self.assertEqual(len(db.flag_calls), 2)

    def test_evicted_samples_do_not_count(self):
        db = self.FakeDB()
        watcher = WindowInspector(db, trigger=3, min_support=3, window_size=3)
        watcher.observe("10.1/a1", "https://x/a1", "Repeated")
        watcher.observe("10.1/a2", "https://x/a2", "Repeated")
        watcher.observe("10.1/b", "https://x/b", "Other B")
        watcher.observe("10.1/c", "https://x/c", "Other C")
        self.assertEqual(watcher.observe("10.1/a3", "https://x/a3", "Repeated"), (0, 0))
        self.assertEqual(db.count_calls, 0)                  # 窗口内从未同时达到 3 条

    def test_dead_404_titles_also_induce_rule(self):
        # DOI 唯一却全是 "Page not found" → 和封面同理：归纳规则、标记其余 pending（失效页也算）
        db = self.FakeDB()
        watcher = WindowInspector(db, trigger=3, min_support=3, window_size=10)
        base = "https://www.sciencedirect.com/science/article/abs/pii/S019566"
        watcher.observe("10.1/s(03)94113-3", base + "1", "Page not found | ScienceDirect")
        watcher.observe("10.1/s(03)94114-5", base + "2", "Page not found | ScienceDirect")
        self.assertEqual(watcher.observe("10.1/s(03)94115-7", base + "3", "Page not found | ScienceDirect"), (1, 2))
        self.assertEqual(len(db.flag_calls), 1)

    def test_coarsen_not_degenerate(self):
        from paperlite.taskq.title_dup import _coarsen
        pii = "https://www.sciencedirect.com/science/article/abs/pii/S0195668X0394"
        self.assertEqual(_coarsen(pii), pii)                 # 完整识别码（长数字尾）不粗化，不退化成 /pii/S
        self.assertEqual(_coarsen("10.1/j(23)0"), "10.1/j(23)")   # 短校验位仍粗化（卷期用）

    def test_prefers_specific_prefix_not_whole_site(self):
        flagged = []

        class DB:
            def count_pending_matching(self, dp, up):
                return 20
            def flag_suspects(self, dp, up, title, support, precision):
                flagged.append((dp, up))
                return 5

        w = WindowInspector(DB(), trigger=10, min_support=10, window_size=50)
        base = "https://www.sciencedirect.com/science/article/abs/pii/S0195668X0394"
        for i in range(10):     # 10 条 EHJ 404，前缀一致
            w.observe(f"10.1016/s0195-668x(03)941{i:02d}", f"{base}{i:03d}", "Page not found | ScienceDirect")
        self.assertTrue(flagged)
        dp, up = flagged[-1]
        self.assertIn("S0195668X", up)                       # 保留 EHJ 识别码
        self.assertNotEqual(up, "https://www.sciencedirect.com/science/article/abs/pii/S")  # 没退化成整站宽前缀


@needs_pg
class TaskSourceTest(unittest.TestCase):
    """worker 看到的任务队列语义（PgTaskSource）：导入、领取、重试、挑战排后、重置。"""

    def setUp(self):
        self.db = make_source(self)

    def urls(self, *ids):
        return [{"pdf_url": f"https://dl.acm.org/doi/pdf/10.1145/{i}"} for i in ids]

    def test_import_dedups_and_keeps_hosts(self):
        self.assertEqual(self.db.import_urls(self.urls(1, 2, 2)), (2, 1))
        t = self.db.claim("w")
        self.db.finish(t, Status.SUCCESS)
        self.assertEqual(self.db.import_urls(self.urls(1, 3)), (1, 1))   # 重复导入不重置
        self.assertEqual(self.db.counts()[Status.SUCCESS], 1)
        self.assertEqual(self.db.host(), "dl.acm.org")
        self.assertEqual(self.db.import_urls([{"url": "https://other.org/x.pdf"}]), (1, 0))   # 允许混合主机
        self.assertEqual(sorted(self.db.hosts()), ["dl.acm.org", "other.org"])

    def test_retry_and_reset(self):
        self.db.import_urls(self.urls(1))
        t = self.db.claim("w")
        self.db.retry_later(t, count=False)                             # cookie 被拒：不计重试
        t = self.db.claim("w")                                          # 本块做完 → 切下一代再领
        self.assertEqual(t.retry_count, 0)
        self.db.retry_later(t, count=True, error_kind="network", error_message="timeout")
        t = self.db.claim("w")
        self.assertEqual(t.retry_count, 1)
        self.db.finish(t, Status.FAILED, error_kind="network")
        self.assertIsNone(self.db.claim("w"))
        self.assertEqual(self.db.kind_counts(), [("failed", "network", 1)])
        self.assertEqual(self.db.q.reset(Status.FAILED, "network"), 1)
        self.assertEqual(self.db.claim("w").retry_count, 0)

    def test_challenged_task_goes_last_then_fails(self):
        self.db.import_urls(self.urls(1, 2))
        t1 = self.db.claim("w")
        self.assertFalse(self.db.challenged(t1, max_challenges=2, exit_ip="1.1.1.1", message="403"))
        t2 = self.db.claim("w")
        self.assertEqual(t2.url, self.urls(2)[0]["pdf_url"])               # 被挑战的排到后面
        self.db.finish(t2, Status.SUCCESS)
        t1 = self.db.claim("w")
        self.assertEqual(t1.challenge_count, 1)
        self.assertTrue(self.db.challenged(t1, max_challenges=2, exit_ip="2.2.2.2", message="403"))
        self.assertEqual(self.db.kind_counts(), [("failed", "challenge_failed", 1), ("success", None, 1)])
        self.db.q.reset(Status.FAILED, "challenge_failed")
        self.assertEqual(self.db.claim("w").challenge_count, 0)

    def test_url_type_imported_and_claimed(self):
        self.db.import_urls([{"pdf_url": "https://dl.acm.org/doi/10.1145/1", "url_type": "html"},
                             {"pdf_url": "https://dl.acm.org/doi/pdf/10.1145/2"}])
        self.assertEqual(sorted(self.db.claim("w").url_type for _ in range(2)), [UrlType.HTML, UrlType.PDF])
        self.assertEqual(sorted(r["url_type"] for r in pg_rows(self.db)), ["html", "pdf"])
        with self.assertRaises(ValueError):
            self.db.import_urls([{"pdf_url": "https://dl.acm.org/doi/10.1145/3", "url_type": "page"}])

    def test_doi_imported_from_column_else_derived(self):
        self.db.import_urls([{"url": "https://dl.acm.org/doi/10.1145/1", "doi": "10.9999/CUSTOM", "source": "s",
                              "batch_no": "b7"},
                             {"url": "https://dl.acm.org/doi/10.1145/2"}])      # 第二条无 doi → 回退推导
        got = sorted((r["doi"], r["source"], r["batch_no"]) for r in pg_rows(self.db))
        self.assertEqual(got, [("10.1145/2", None, None), ("10.9999/custom", "s", "b7")])   # doi 转小写

    def test_bad_urls_rejected_not_imported(self):
        rows = [{"url": "https://dl.acm.org/doi/pdf/10.1145/1", "doi": "10.1145/1"},
                {"url": "https://10.65.16.77/a", "doi": "10.13102/x"},
                {"url": "https://.asm.org/content/1", "doi": "10.1128/y"},
                {"url": "", "doi": "10.1/empty"}]
        self.assertEqual(self.db.import_urls(rows), (1, 0))                 # 坏的不算新增也不算已存在
        self.assertEqual([reason for _, reason in self.db.q.last_rejected], ["private_ip", "bad_host", "bad_scheme"])
        self.assertEqual([r["doi"] for r in pg_rows(self.db)], ["10.1145/1"])
        self.assertEqual(self.db.hosts(), ["dl.acm.org"])

    def test_check_constraints(self):
        import psycopg
        from paperlite.taskq import Result
        self.db.import_urls(self.urls(1))
        t = self.db.claim("w")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.db.q.report([Result(t.id, Status.NON_PDF, error_kind="subscription")])   # 不在类别表里

    def test_export_rows_stream(self):
        self.db.import_urls(self.urls(1, 2, 3))
        t = self.db.claim("w")
        self.db.finish(t, Status.NON_PDF, error_kind="paywall")
        self.assertEqual(len(list(self.db.q.rows())), 3)
        self.assertEqual([r[2] for r in self.db.q.rows(Status.NON_PDF)], [t.url])
