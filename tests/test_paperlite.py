import os
import json
import logging
import tempfile
import tomllib
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from paperlite.browser_pool import BrowserPool
from paperlite.classify import classify_html
from paperlite.config import Limits, RunConfig, proxy_settings
from paperlite.consumer import Downloader, impersonate_for
from paperlite.inspect import WindowInspector
from paperlite.logutil import JsonFormatter
from paperlite.producer import Producer
from paperlite.proxy import Lease, ProxySettings, parse_deadline
from paperlite.store import Identity
from paperlite.tasks import Status, TaskDB, UrlType, derive_doi, derive_volume, pdf_path, url_key

FAKE_PDF = b"%PDF-1.4\n" + b"0" * 2048          # 超过最小文件大小（1024 字节）
PROXY = ProxySettings(api_url="https://api.example/get?key=k", username="u", password="p@ss")


def ident(**kw) -> Identity:
    d = dict(host="dl.acm.org", exit_ip="1.2.3.4", proxy_server="1.2.3.4:8000", user_agent="Mozilla/5.0 Chrome/146.0",
             cookies=[{"name": "cf_clearance", "value": "v", "domain": ".acm.org", "path": "/"}],
             expires_at=time.time() + 600)
    d.update(kw)
    return Identity(**d)


class ConfigTest(unittest.TestCase):
    def test_from_dict(self):
        cfg = Limits.from_dict({"request_gap_s": [1, 2]}, landing={"DL.ACM.ORG": "https://x/"})
        self.assertEqual(cfg.request_gap_s, (1.0, 2.0))
        self.assertEqual(cfg.landing_for("dl.acm.org"), "https://x/")
        self.assertEqual(cfg.landing_for("other.org"), "https://other.org/")

    def test_rejects_unknown_and_bad_gap(self):
        with self.assertRaises(ValueError):
            Limits.from_dict({"max_browser": 3})
        with self.assertRaises(ValueError):
            Limits.from_dict({"request_gap_s": [5, 3]})

    def test_page_interval_parsed_and_validated(self):
        self.assertEqual(Limits().page_interval_s, (10.0, 30.0))
        self.assertEqual(Limits.from_dict({"page_interval_s": [15, 40]}).page_interval_s, (15.0, 40.0))
        with self.assertRaises(ValueError):
            Limits.from_dict({"page_interval_s": [30, 10]})

    def test_title_window_must_cover_trigger(self):
        self.assertEqual(Limits().title_window_size, 500)
        with self.assertRaises(ValueError):
            Limits.from_dict({"title_dup_trigger": 10, "title_window_size": 9})

    def test_default_run_config(self):
        run = RunConfig.from_dict({"command": "run", "mode": "browser", "proxy": False,
                                   "workers": 1, "urls": "x.csv", "url_type": "html"})
        self.assertEqual((run.command, run.mode, run.proxy, run.workers), ("run", "browser", False, 1))
        with self.assertRaises(ValueError):
            RunConfig.from_dict({"command": "run", "mode": "curl", "proxy": False})

    def test_configured_run_argument_mapping(self):
        import argparse
        from paperlite.cli import _configured_run_args
        base = argparse.Namespace(config="x.toml", log_level="INFO")
        args = _configured_run_args(base, RunConfig(command="run", mode="browser", proxy=False,
                                                     workers=2, urls="x.csv", url_type="html"))
        self.assertEqual((args.cmd, args.browser, args.no_proxy, args.workers, args.urls, args.type),
                         ("run", True, True, 2, "x.csv", "html"))

    def test_json_log_formatter(self):
        record = logging.LogRecord("paperlite.test", logging.INFO, __file__, 1,
                                   "页面保存成功", (), None)
        record.event = "page_saved"
        record.doi = "10.1/example"
        record.seconds = 3.2
        data = json.loads(JsonFormatter().format(record))
        self.assertEqual((data["event"], data["doi"], data["seconds"]),
                         ("page_saved", "10.1/example", 3.2))
        self.assertNotIn("bytes", data)

    def test_console_formatter_human_readable(self):
        from paperlite.logutil import ConsoleFormatter
        record = logging.LogRecord("paperlite", logging.INFO, __file__, 1,
                                   "待下载 100", (), None)
        record.event = "progress"           # 内建+隐藏字段不进文本
        record.pdf = 140
        line = ConsoleFormatter(color=False).format(record)
        self.assertNotIn("{", line)         # 不是 JSON
        self.assertIn("INFO", line)
        self.assertIn("待下载 100", line)
        self.assertIn("pdf=140", line)      # 业务字段附在后面
        self.assertNotIn("event=", line)    # progress 这类隐藏字段不打
        self.assertFalse(line.startswith("\033"))   # 无色时不含 ANSI

    def test_console_formatter_colors_only_when_enabled(self):
        from paperlite.logutil import ConsoleFormatter
        rec = logging.LogRecord("paperlite", logging.ERROR, __file__, 1, "过盾超时", (), None)
        self.assertTrue(ConsoleFormatter(color=True).format(rec).startswith("\033["))
        self.assertFalse(ConsoleFormatter(color=False).format(rec).startswith("\033["))

    def test_log_format_selection(self):
        import io
        from paperlite.logutil import _console_formatter, ConsoleFormatter, JsonFormatter
        nontty = io.StringIO()                      # isatty() -> False
        # auto：非 TTY（重定向/管道/采集）给 JSON，便于机器解析
        self.assertIsInstance(_console_formatter("auto", nontty), JsonFormatter)
        # 显式覆盖
        self.assertIsInstance(_console_formatter("json", nontty), JsonFormatter)
        self.assertIsInstance(_console_formatter("text", nontty), ConsoleFormatter)

    def test_proxy_settings(self):
        with self.assertRaises(ValueError):
            proxy_settings({"username": "u"})            # 缺 api_url
        with self.assertRaises(ValueError):
            proxy_settings({"api_url": "x", "proxy_url": "y"})
        self.assertEqual(PROXY.url_for("1.2.3.4:80"), "http://u:p%40ss@1.2.3.4:80")
        self.assertEqual(Lease("1.2.3.4:80", "1.2.3.4", "", 0).browser_proxy(PROXY),
                         {"server": "http://1.2.3.4:80", "username": "u", "password": "p@ss"})

    def test_load_file(self):
        from paperlite.config import load
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[proxy]\napi_url = "https://a/"\n[limits]\nmax_browsers = 3\n[landing]\n"h.org" = "https://h.org/x"\n')
        try:
            cfg, proxy = load(Path(f.name))
            self.assertEqual((cfg.max_browsers, cfg.landing_for("h.org"), proxy.api_url), (3, "https://h.org/x", "https://a/"))
        finally:
            os.unlink(f.name)

    def test_config_lookup_order(self):
        from paperlite.config import CONFIG_ENV, resolve_path
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CONFIG_ENV, None)
            self.assertEqual(resolve_path(), Path("config.toml"))            # 默认当前目录
            os.environ[CONFIG_ENV] = "/etc/pl.toml"
            self.assertEqual(resolve_path(), Path("/etc/pl.toml"))           # 环境变量
            self.assertEqual(resolve_path("x.toml"), Path("x.toml"))         # --config 优先
            os.environ.pop(CONFIG_ENV)

    def test_parse_deadline_and_impersonate(self):
        self.assertIsNotNone(parse_deadline("2026-10-24  20:52:15"))     # 接口偶有双空格
        self.assertIsNone(parse_deadline(""))
        self.assertTrue(impersonate_for("Mozilla/5.0 Chrome/146.0.0.0").startswith("chrome"))

    def test_parse_extract_json_and_text(self):
        from paperlite.proxy import parse_extract
        j = parse_extract('{"code": "SUCCESS", "data": [{"server": "1.2.3.4:8000", "proxy_ip": "1.2.3.4"}]}')
        self.assertEqual((j["server"], j["proxy_ip"]), ("1.2.3.4:8000", "1.2.3.4"))
        self.assertEqual(parse_extract("140.224.134.247:48747"), {"server": "140.224.134.247:48747"})
        self.assertEqual(parse_extract("1.2.3.4:80\r\n5.6.7.8:81\r\n")["server"], "1.2.3.4:80")   # 多行取第一个
        for bad in ('{"code": 1, "msg": "余额不足"}', "请求过于频繁", "", '{"code": "SUCCESS", "data": []}'):
            with self.assertRaises(RuntimeError):
                parse_extract(bad)


class ClassifyTest(unittest.TestCase):
    NAV = '<a href="/premium" title="Basic Edition, Upgrade">'

    def test_categories(self):
        self.assertEqual(classify_html(f"<title>ACM Error: 404</title>{self.NAV}"), "not_found")
        self.assertEqual(classify_html(
            f"<title>Paper</title>{self.NAV}You are using the Basic Edition. Features requiring a subscription"),
            "paywall")
        self.assertEqual(classify_html("<title>ACM Error: IP blocked</title>"), "blocked")
        # 导航栏的 Basic Edition 每页都有，不算订阅
        self.assertEqual(classify_html(f"<title>Paper</title>{self.NAV}"), "landing_page")


class IdentityRulesTest(unittest.TestCase):
    """与 paper_downloader downloader/identity.py、paths.py 规则一致。"""

    def test_paper_uid_and_path(self):
        url = "https://dl.acm.org/doi/pdf/10.1145/1005062.1005064"
        self.assertEqual(derive_doi(url), "10.1145/1005062.1005064")
        self.assertTrue(derive_doi("https://x.org/a.pdf").startswith("url-"))
        p = pdf_path(Path("out"), "10.1145/3779295")
        self.assertEqual(p.name, "10.1145_3779295.pdf")
        self.assertEqual(len(p.parent.name), 2)              # out/<h0:2>/<h2:4>/
        self.assertEqual(url_key("HTTPS://DL.ACM.org/a#x"), url_key("https://dl.acm.org/a"))
        self.assertNotEqual(url_key("https://dl.acm.org/A"), url_key("https://dl.acm.org/a"))


class InspectTest(unittest.TestCase):
    """巡检：同标题重复触发 → doi/url 规则 → 标记疑似封面的 pending。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TaskDB(Path(self.tmp.name) / "tasks.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _add(self, doi, url, status=None, title=None):
        self.db.import_urls([{"url": url, "doi": doi}])
        if status:                              # 领取后写成最终结果（模拟已采）
            t = self.db.claim("w")
            self.db.finish(t, status, title=title)

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

        from paperlite.inspect import inspect
        rules, flagged = inspect(self.db, trigger=10, min_precision=0.98, min_support=10)
        self.assertEqual((rules, flagged), (1, 8))           # 一条规则，标记 8 篇未采封面
        self.assertEqual(self.db.suspect_count(), 8)
        # 领取只拿得到真文章，封面被跳过
        claimed = [self.db.claim(f"w{i}") for i in range(5)]
        got = {t.doi for t in claimed if t}
        self.assertTrue(got <= {"10.1/art.6", "10.1/art.7", "10.1/art.8"})
        self.assertFalse(any((t and t.doi.startswith("10.1/cov")) for t in claimed))
        # 规则落库，可清除
        self.assertEqual(len(self.db.skip_rules()), 1)
        self.assertEqual(self.db.clear_suspects(), 8)
        self.assertEqual(self.db.suspect_count(), 0)

    def test_scattered_covers_not_flagged_by_precision_guard(self):
        # 封面和真文章 doi/url 交错，唯一公共前缀会命中真文章 → precision 不达标 → 不标记
        for i in range(1, 13):
            self._add(f"10.1/j.{2*i:03d}", f"https://ex.org/p/{2*i:03d}", "success", "Outside Front Cover")
            self._add(f"10.1/j.{2*i+1:03d}", f"https://ex.org/p/{2*i+1:03d}", "success", f"Real {i}")
        for i in range(100, 110):               # 未采，和上面同刊前缀
            self._add(f"10.1/j.{i}", f"https://ex.org/p/{i}")
        from paperlite.inspect import inspect
        rules, flagged = inspect(self.db, trigger=10, min_precision=0.98, min_support=10)
        self.assertEqual((rules, flagged), (0, 0))           # 护栏生效，不误标
        self.assertEqual(self.db.suspect_count(), 0)


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
        from paperlite.inspect import _coarsen
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


class BrowserObserveWiringTest(unittest.TestCase):
    """浏览器模式：不止成功页，404 等已加载的失效页也要喂给巡检器（不再只在成功分支调）。"""

    def test_404_branch_feeds_inspector(self):
        import asyncio
        from types import SimpleNamespace
        from paperlite.browser_pool import BrowserPool
        from paperlite.browser import RenderResult
        from paperlite.config import Limits
        from paperlite.proxy import Lease
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = TaskDB(Path(tmp.name) / "tasks.db"); self.addCleanup(db.close)
        db.import_urls([{"url": "https://www.sciencedirect.com/science/article/abs/pii/S1"}])
        pool = BrowserPool(Limits(), None, db, Path(tmp.name) / "out", workers=1, no_proxy=True)
        pool.inspector = mock.Mock()
        pool.inspector.observe = mock.Mock(return_value=(0, 0))
        task = db.claim("w")
        r = RenderResult(ok=True, seconds=1.0, status=404, title="Page not found | ScienceDirect",
                         url="https://www.sciencedirect.com/science/article/abs/pii/S1")
        session = SimpleNamespace(render=mock.AsyncMock(return_value=r))
        lease = Lease(server="x", exit_ip="1.2.3.4", area="", expires_at=time.time() + 600)
        asyncio.run(pool._do_task(task, session, lease, "t"))
        pool.inspector.observe.assert_called_once_with(task.doi, task.url, r.title)   # 404 分支喂了巡检
        self.assertEqual(db.counts()[Status.FAILED], 1)                               # 且仍记为 failed

    def test_challenge_timeout_not_fed(self):
        # r.ok=False（过盾超时，标题是瞬时的 "Just a moment"）绝不能进巡检，否则会误标正常 pending
        import asyncio
        from types import SimpleNamespace
        from paperlite.browser_pool import BrowserPool
        from paperlite.browser import RenderResult
        from paperlite.config import Limits
        from paperlite.proxy import Lease
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = TaskDB(Path(tmp.name) / "tasks.db"); self.addCleanup(db.close)
        db.import_urls([{"url": "https://www.sciencedirect.com/science/article/abs/pii/S2"}])
        pool = BrowserPool(Limits(), None, db, Path(tmp.name) / "out", workers=1, no_proxy=True)
        pool.inspector = mock.Mock()
        task = db.claim("w")
        r = RenderResult(ok=False, seconds=90.0, title="Just a moment...", error="TimeoutError")
        session = SimpleNamespace(render=mock.AsyncMock(return_value=r))
        lease = Lease(server="x", exit_ip="1.2.3.4", area="", expires_at=time.time() + 600)
        asyncio.run(pool._do_task(task, session, lease, "t"))
        pool.inspector.observe.assert_not_called()


class TaskDBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TaskDB(Path(self.tmp.name) / "tasks.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def urls(self, *ids):
        return [{"pdf_url": f"https://dl.acm.org/doi/pdf/10.1145/{i}"} for i in ids]

    def test_import_dedups_and_keeps_one_host(self):
        self.assertEqual(self.db.import_urls(self.urls(1, 2, 2)), (2, 1))
        t = self.db.claim("w")
        self.db.finish(t, Status.SUCCESS)
        self.assertEqual(self.db.import_urls(self.urls(1, 3)), (1, 1))   # 重复导入不重置
        self.assertEqual(self.db.counts()[Status.SUCCESS], 1)
        self.assertEqual(self.db.host(), "dl.acm.org")
        # 允许混合主机：再导入别的主机不报错，host() 仍是第一个，hosts() 两个都在
        self.assertEqual(self.db.import_urls([{"url": "https://other.org/x.pdf"}]), (1, 0))
        self.assertEqual(self.db.host(), "dl.acm.org")
        self.assertEqual(self.db.hosts(), ["dl.acm.org", "other.org"])

    def test_claim_retry_and_reset(self):
        self.db.import_urls(self.urls(1))
        t = self.db.claim("w")
        self.assertIsNone(self.db.claim("w2"))                          # 已被领走
        self.db.retry_later(t, count=False)                             # cookie 被拒：不计重试
        t = self.db.claim("w")
        self.assertEqual(t.retry_count, 0)
        self.db.retry_later(t, count=True, error_kind="network", error_message="timeout")
        t = self.db.claim("w")
        self.assertEqual(t.retry_count, 1)
        self.db.finish(t, Status.FAILED, error_kind="network")
        self.assertEqual(self.db.kind_counts(), [("failed", "network", 1, 0)])
        self.assertEqual(self.db.reset(Status.FAILED, "network"), 1)
        self.assertEqual(self.db.claim("w").retry_count, 0)

    def test_challenged_task_goes_last_then_fails(self):
        self.db.import_urls(self.urls(1, 2))
        t1 = self.db.claim("w")
        self.assertFalse(self.db.challenged(t1, max_challenges=2, exit_ip="1.1.1.1", message="403"))
        self.assertEqual(self.db.claim("w").url, self.urls(2)[0]["pdf_url"])   # 被挑战的排到后面
        t1 = self.db.claim("w")
        self.assertEqual(t1.challenge_count, 1)
        self.assertTrue(self.db.challenged(t1, max_challenges=2, exit_ip="2.2.2.2", message="403"))
        self.assertEqual(self.db.kind_counts(), [("failed", "challenge_failed", 1, 0)])
        self.db.reset(Status.FAILED, "challenge_failed")
        self.assertEqual(self.db.claim("w").challenge_count, 0)

    def test_old_schema_rejected(self):
        import sqlite3
        path = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE paper_pdf_resources (id INTEGER PRIMARY KEY, retry_count INTEGER)")
        conn.close()
        with self.assertRaises(RuntimeError):
            TaskDB(path)

    def test_url_type_imported_and_claimed(self):
        self.db.import_urls([{"pdf_url": "https://dl.acm.org/doi/10.1145/1", "url_type": "html"},
                             {"pdf_url": "https://dl.acm.org/doi/pdf/10.1145/2"}])
        self.assertEqual([self.db.claim("w").url_type for _ in range(2)], [UrlType.HTML, UrlType.PDF])
        self.assertEqual([r["url_type"] for r in self.db.rows()], ["html", "pdf"])
        self.assertEqual(self.db.type_counts()["html"][Status.RUNNING], 1)
        with self.assertRaises(ValueError):
            self.db.import_urls([{"pdf_url": "https://dl.acm.org/doi/10.1145/3", "url_type": "page"}])

    def test_old_db_gets_url_type_column(self):
        import sqlite3
        self.db.import_urls(self.urls(1))
        self.db.close()
        conn = sqlite3.connect(self.db.path)
        conn.execute("ALTER TABLE paper_pdf_resources DROP COLUMN url_type")      # 模拟 0.1.2 的库
        conn.commit()
        conn.close()
        self.db = TaskDB(self.db.path)
        self.assertEqual(self.db.claim("w").url_type, UrlType.PDF)
        self.db.import_urls([{"pdf_url": "https://dl.acm.org/doi/10.1145/2", "url_type": "html"}])
        self.assertEqual(self.db.claim("w").url_type, UrlType.HTML)

    def test_doi_imported_from_column_else_derived(self):
        self.db.import_urls([{"url": "https://dl.acm.org/doi/10.1145/1", "doi": "10.9999/CUSTOM"},
                             {"url": "https://dl.acm.org/doi/10.1145/2"}])      # 第二条无 doi → 回退推导
        dois = sorted(r["doi"] for r in self.db.rows())
        self.assertEqual(dois, ["10.1145/2", "10.9999/custom"])               # 导入的 doi 转小写；缺省从 URL 推

    def test_old_db_renames_pdf_url_and_paper_uid(self):
        import sqlite3
        self.db.import_urls(self.urls(1))
        self.db.close()
        conn = sqlite3.connect(self.db.path)                                  # 模拟 0.1.3 之前：列名还是 pdf_url / paper_uid，无 title
        conn.execute("ALTER TABLE paper_pdf_resources RENAME COLUMN url TO pdf_url")
        conn.execute("ALTER TABLE paper_pdf_resources RENAME COLUMN doi TO paper_uid")
        conn.execute("ALTER TABLE paper_pdf_resources DROP COLUMN title")
        conn.commit()
        conn.close()
        self.db = TaskDB(self.db.path)                                        # 打开时自动 RENAME 回来 + 补 title
        t = self.db.claim("w")
        self.assertTrue(t.url.startswith("https://dl.acm.org") and t.doi)
        self.assertIn("title", [d[0] for d in self.db._conn.execute("SELECT * FROM paper_pdf_resources").description])

    def test_check_constraints(self):
        import sqlite3
        self.db.import_urls(self.urls(1))
        t = self.db.claim("w")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.finish(t, Status.NON_PDF, error_kind="subscription")   # 不在类别表里

    def test_download_lock_reclaims_running(self):
        self.db.import_urls(self.urls(1, 2))
        self.db.claim("crashed-process")
        self.assertEqual(self.db.lock_for_download(), 1)
        other = TaskDB(self.db.path)
        with self.assertRaises(RuntimeError):
            other.lock_for_download()                                   # 同一个库只允许一个下载进程
        other.close()


class VolumeStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TaskDB(Path(self.tmp.name) / "tasks.db", volume_stop_after=3)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def load(self, vol, n, start=1):
        self.db.import_urls({"pdf_url": f"https://dl.acm.org/doi/pdf/10.1145/{vol}.{vol + i}"} for i in range(start, start + n))

    def test_derive_volume(self):
        self.assertEqual(derive_volume("10.1145/1012326.1012328"), "1012326")
        self.assertIsNone(derive_volume("10.1145/3779295"))
        self.assertIsNone(derive_volume("url-abcd"))

    def test_unverified_volume_limited_to_n_in_flight(self):
        self.load(1000, 6)
        self.load(2000, 2)
        got = [self.db.claim("w") for _ in range(6)]
        vols = [t.doi.split("/")[1].split(".")[0] for t in got if t]
        self.assertEqual(vols, ["1000", "1000", "1000", "2000", "2000"])   # 卷期 1000 最多放 3 篇，其余线程去别的卷期
        self.assertIsNone(got[5])
        self.db.finish(got[0], Status.SUCCESS)                               # 下到一篇 PDF → 卷期确认，放开
        self.assertIsNotNone(self.db.claim("w"))

    def test_three_no_pdf_stops_rest_of_volume(self):
        self.load(1000, 10)
        tasks = [self.db.claim("w") for _ in range(3)]
        self.assertIsNone(self.db.finish(tasks[0], Status.NON_PDF, error_kind="not_found"))
        self.assertIsNone(self.db.finish(tasks[1], Status.NON_PDF, error_kind="paywall"))
        self.assertEqual(self.db.finish(tasks[2], Status.NON_PDF, error_kind="not_found"), ("1000", 7))
        self.assertFalse(self.db.has_status(Status.PENDING))
        self.assertEqual(self.db.volume_summary(), (1, 7))
        r = [x for x in self.db.rows() if x["inferred"]][0]
        self.assertEqual((r["status"], r["error_kind"]), ("non_pdf", "not_found"))   # 取样本里最多的类别
        self.assertIn("按卷期推断", r["error_message"])
        self.assertEqual(self.db.reset_stopped_volumes(), (1, 10))                    # 重新核实：样本 + 推断全放回
        self.assertEqual(self.db.counts()[Status.PENDING], 10)

    def test_volume_with_pdf_never_stops(self):
        self.load(1000, 6)
        t = self.db.claim("w")
        self.db.finish(t, Status.SUCCESS)
        for _ in range(4):                                                    # 卷首语、(abstract) 之类
            self.assertIsNone(self.db.finish(self.db.claim("w"), Status.NON_PDF, error_kind="landing_page"))
        self.assertTrue(self.db.has_status(Status.PENDING))

    def test_network_failures_do_not_count(self):
        self.load(1000, 5)
        for _ in range(3):
            self.db.finish(self.db.claim("w"), Status.FAILED, error_kind="network")
        self.assertIsNotNone(self.db.claim("w"))

    def test_new_style_doi_not_grouped(self):
        self.db.import_urls({"pdf_url": f"https://dl.acm.org/doi/pdf/10.1145/{n}"} for n in (3779295, 3779296, 3779297, 3779298))
        self.assertEqual(len([self.db.claim("w") for _ in range(4)]), 4)


class CliDbOptionTest(unittest.TestCase):
    """--db 让任务库和输出目录分开（输出目录在 goofys 等对象存储挂载上时，任务库放本地盘）。"""

    def setUp(self):
        # 在空的临时目录里跑：不读项目根目录的 config.toml，也不会把那里的 pdfs/ 当成"唯一的站点"
        self._cwd = os.getcwd()
        self._tmp_cwd = tempfile.TemporaryDirectory()
        os.chdir(self._tmp_cwd.name)
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        os.environ.pop("PAPERLITE_CONFIG", None)

    def tearDown(self):
        self._env.stop()
        os.chdir(self._cwd)
        self._tmp_cwd.cleanup()

    def run_cli(self, *argv):
        from paperlite import cli
        with mock.patch("sys.argv", ["paperlite", *argv]):
            cli.main()

    def test_no_command_prints_help(self):
        import contextlib, io
        for argv in ((), ("help",), ("help", "run")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.run_cli(*argv)                             # 不抛 SystemExit
            self.assertIn("用法: paperlite", buf.getvalue())

    def test_init_writes_template(self):
        self.run_cli("init")
        cfg = Path("config.toml")
        self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)
        with open(cfg, "rb") as f:
            raw = tomllib.load(f)
        self.assertEqual(set(raw), {"proxy", "limits", "paths", "run", "landing", "monitor"})
        self.assertFalse(raw["monitor"]["enabled"])               # 模板默认不上报
        with self.assertRaises(SystemExit):                     # 已存在不覆盖
            self.run_cli("init")
        self.run_cli("init", "--force")

    def test_db_arg_locates_db(self):
        # 任务库由 --db 指定，与输出目录无关；import/retry/export 只认 --db
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            urls = tmp / "urls.txt"
            urls.write_text("https://dl.acm.org/doi/pdf/10.1145/1.2\nhttps://dl.acm.org/doi/pdf/10.1145/1.3\n")
            db = tmp / "local" / "acm.db"
            self.run_cli("import", "--db", str(db), "--urls", str(urls))
            self.assertTrue(db.exists())
            self.run_cli("retry", "--db", str(db), "--status", "failed")
            csv_path = tmp / "x.csv"
            self.run_cli("export", "--db", str(db), "--file", str(csv_path))
            self.assertEqual(len(csv_path.read_text().strip().splitlines()), 3)
            with self.assertRaises(SystemExit):                               # 库不存在
                self.run_cli("export", "--db", str(tmp / "nope.db"), "--file", str(csv_path))

    def test_mixed_hosts_go_into_one_db(self):
        # 弱化站点：各种主机混着导进同一个库，不按主机分目录
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            f = tmp / "mixed.txt"
            f.write_text("https://dl.acm.org/doi/pdf/10.1145/1.2\nhttps://example.org/a.pdf\n")
            db = tmp / "t.db"
            self.run_cli("import", "--db", str(db), "--urls", str(f))
            d = TaskDB(db)
            try:
                self.assertEqual(sorted(d.hosts()), ["dl.acm.org", "example.org"])   # 多主机同库
                self.assertEqual(sum(d.counts().values()), 2)
            finally:
                d.close()

    def test_db_from_config_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = tmp / "c.toml"
            dbfile = tmp / "local" / "t.db"
            cfg.write_text(f'[paths]\ndb = "{dbfile}"\n')         # import 只读 [paths]，不要求 [proxy]
            urls = tmp / "u.txt"
            urls.write_text("https://dl.acm.org/doi/pdf/10.1145/1.2\n")
            self.run_cli("--config", str(cfg), "import", "--urls", str(urls))
            self.assertTrue(dbfile.exists())                     # 用 [paths].db 的位置


class ProducerTargetTest(unittest.TestCase):
    def test_target_follows_workers(self):
        p = Producer(Limits(max_identities=20, min_identities=2), mock.Mock(), mock.Mock(), "dl.acm.org")
        self.assertEqual(p.target(0), 2)      # 没有下载线程：只保预热数
        self.assertEqual(p.target(8), 8)
        self.assertEqual(p.target(50), 20)


class ProducerHedgeTest(unittest.TestCase):
    def test_stuck_passes_get_hedged_within_browser_cap(self):
        import asyncio
        from paperlite import producer as producer_mod
        from paperlite.store import PoolStats

        store = mock.Mock()
        store.hold_producer.return_value = True
        store.stats.return_value = PoolStats(ready=0, inuse=0, healthy=0, workers=2)
        store.paused_for.return_value = 0
        store.fresh_rejects.return_value = 0
        ips = mock.Mock()
        ips.extract_checked.return_value = Lease("1.2.3.4:80", "1.2.3.4", "", time.time() + 600)
        ips.s = PROXY
        cfg = Limits(max_browsers=5, hedge_after_s=0.05, pass_timeout_s=10)
        p = Producer(cfg, store, ips, "dl.acm.org")
        launched = []

        async def stuck_pass(*a, **kw):          # 模拟 Turnstile 一直转圈
            launched.append(time.monotonic())
            await asyncio.sleep(10)

        async def scenario():
            stop = threading.Event()
            task = asyncio.create_task(p.run(stop))
            await asyncio.sleep(0.5)
            stop.set()
            await task

        with mock.patch.object(producer_mod, "pass_challenge", stuck_pass), \
                mock.patch.object(producer_mod, "_TICK_S", 0.02):
            asyncio.run(scenario())
        self.assertGreater(len(launched), 2)       # 目标 2 个，卡住后另开补位
        self.assertLessEqual(len(launched), 5)     # 但不超过浏览器上限


class ProducerBreakerTest(unittest.TestCase):
    def test_trips_and_stops_producing(self):
        store = mock.Mock()
        store.paused_for.return_value = 0
        store.fresh_rejects.return_value = 5
        p = Producer(Limits(max_fresh_rejects=5, breaker_pause_s=600), store, mock.Mock(), "dl.acm.org")
        self.assertTrue(p._breaker_open())
        store.trip.assert_called_once_with("dl.acm.org", 600, 5)
        store.paused_for.return_value = 590
        self.assertTrue(p._breaker_open())                 # 暂停中
        store.paused_for.return_value = 0
        store.fresh_rejects.return_value = 4
        self.assertFalse(p._breaker_open())                # 暂停结束、计数在阈值下：放一个试探


class Resp:
    """curl_cffi 响应替身：支持 stream=True 的 iter_content / close。fail_after 字节后模拟断流。"""

    def __init__(self, status, body, headers=None, fail_after=None):
        self.status_code, self.content, self.headers = status, body, headers or {}
        self.fail_after = fail_after
        self.closed = False

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self.content), 100):          # 小块，覆盖"首块不够 1024 字节"的情况
            if self.fail_after is not None and i >= self.fail_after:
                raise ConnectionError("curl: (56) Connection closed abruptly")
            yield self.content[i:i + 100]

    def close(self):
        self.closed = True


class FakeStore:
    """内存版 IdentityStore，只实现消费者用到的部分。"""

    def __init__(self, idents):
        self.ready = list(idents)
        self.discarded, self.released = [], []
        self.fresh_rejects = 0
        self.lock = threading.Lock()

    def note_success(self, host):
        self.fresh_rejects = 0

    def note_fresh_reject(self, host):
        self.fresh_rejects += 1
        return self.fresh_rejects

    def claim(self, host, holder, min_left_s, lease_ttl_s):
        with self.lock:
            return self.ready.pop(0) if self.ready else None

    def heartbeat(self, ident, ttl):
        pass

    def discard(self, ident):
        self.discarded.append(ident.exit_ip)

    def release(self, ident):
        self.released.append(ident.exit_ip)

    def worker_alive(self, host, worker):
        self.alive_calls = getattr(self, "alive_calls", 0) + 1

    def worker_gone(self, host, worker):
        pass


class DownloaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.db = TaskDB(self.out / "tasks.db")
        self.cfg = Limits(request_gap_s=(0, 0))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_with(self, store, responses_by_ip, urls, url_type="pdf"):
        self.db.import_urls({"pdf_url": u, "url_type": url_type} for u in urls)

        def fake_session(ident):
            s = mock.Mock()
            s.get.side_effect = lambda url, **kw: responses_by_ip[ident.exit_ip](url)
            return s

        dl = Downloader(self.cfg, store, PROXY, self.db, self.out, workers=1)
        with mock.patch.object(Downloader, "_session", side_effect=fake_session):
            dl.run()
        return dl

    def row(self, url):
        return next(r for r in self.db.rows() if r["url"] == url)

    def test_rejected_cookie_switches_identity_without_counting_retry(self):
        store = FakeStore([ident(exit_ip="1.1.1.1"), ident(exit_ip="2.2.2.2")])
        urls = ["https://dl.acm.org/doi/pdf/10.1145/1", "https://dl.acm.org/doi/pdf/10.1145/2"]
        dl = self.run_with(store, {
            "1.1.1.1": lambda u: Resp(403, b"<html>challenge-platform</html>", {"cf-mitigated": "challenge"}),
            "2.2.2.2": lambda u: Resp(200, FAKE_PDF),
        }, urls)
        self.assertEqual((dl.stats.success, dl.stats.failed, dl.stats.rejected), (2, 0, 1))
        self.assertEqual(store.discarded, ["1.1.1.1"])
        self.assertEqual(store.released, ["2.2.2.2"])      # 用完还回池里
        self.assertEqual(store.fresh_rejects, 0)            # 第二个身份成功，熔断计数清零
        r = self.row(urls[0])
        self.assertEqual((r["status"], r["retry_count"], r["exit_ip"]), ("success", 0, "2.2.2.2"))
        self.assertTrue(Path(r["file_path"]).exists())
        self.assertEqual(Path(r["file_path"]), pdf_path(self.out, "10.1145/1"))

    def test_non_pdf_classified_and_saved(self):
        url = "https://dl.acm.org/doi/pdf/10.1145/404"
        dl = self.run_with(FakeStore([ident()]), {"1.2.3.4": lambda u: Resp(200, b"<title>ACM Error: 404</title>")},
                           [url])
        self.assertEqual(dl.stats.non_pdf, 1)
        r = self.row(url)
        self.assertEqual((r["status"], r["error_kind"], r["title"]), ("non_pdf", "not_found", "ACM Error: 404"))
        self.assertTrue((self.out / "non_pdf" / "10.1145_404.html").exists())

    def test_blocked_ip_drops_identity(self):
        store = FakeStore([ident(exit_ip="1.1.1.1"), ident(exit_ip="2.2.2.2")])
        url = "https://dl.acm.org/doi/pdf/10.1145/1"
        dl = self.run_with(store, {"1.1.1.1": lambda u: Resp(403, b"<title>ACM Error: IP blocked</title>"),
                                   "2.2.2.2": lambda u: Resp(200, FAKE_PDF)}, [url])
        self.assertEqual((dl.stats.success, dl.stats.rejected, store.discarded), (1, 1, ["1.1.1.1"]))

    def test_fresh_rejects_counted_but_not_network_errors(self):
        store = FakeStore([ident(exit_ip="1.1.1.1"), ident(exit_ip="2.2.2.2"), ident(exit_ip="3.3.3.3")])
        challenge = lambda u: Resp(403, b"challenge-platform", {"cf-mitigated": "challenge"})

        def refused(u):
            raise ConnectionError("curl: (7) Failed to connect")
        self.cfg = Limits(request_gap_s=(0, 0), max_tries=1)
        self.run_with(store, {"1.1.1.1": challenge, "2.2.2.2": refused, "3.3.3.3": challenge},
                      ["https://dl.acm.org/doi/pdf/10.1145/1"])
        self.assertEqual(store.fresh_rejects, 1)            # 1.1.1.1 被拒计入；2.2.2.2 连不上不计，任务判失败后结束

    def test_url_challenge_keeps_identity(self):
        # 站点单独盯上 bad 这条：带同一 cookie 访问落地页和别的论文都正常
        def site(u):
            if "bad" in u:
                return Resp(403, b"challenge-platform", {"cf-mitigated": "challenge"})
            return Resp(200, b"<title>landing</title>") if "/doi/pdf/" not in u else Resp(200, FAKE_PDF)
        store = FakeStore([ident()])
        self.cfg = Limits(request_gap_s=(0, 0), max_url_challenges=2,
                              landing={"dl.acm.org": "https://dl.acm.org/doi/10.1145/landing"})
        bad, good = "https://dl.acm.org/doi/pdf/10.1145/bad", "https://dl.acm.org/doi/pdf/10.1145/good"
        dl = self.run_with(store, {"1.2.3.4": site}, [bad, good])
        self.assertEqual((dl.stats.success, dl.stats.failed, dl.stats.challenged, dl.stats.rejected), (1, 1, 2, 0))
        self.assertEqual(store.discarded, [])                # 身份没被误杀
        self.assertEqual(store.fresh_rejects, 0)             # 也不计入熔断
        r = self.row(bad)
        self.assertEqual((r["status"], r["error_kind"], r["challenge_count"]), ("failed", "challenge_failed", 2))

    def test_connection_reset_retried_with_same_identity(self):
        calls = []

        def flaky(u):
            calls.append(u)
            if len(calls) == 1:
                raise ConnectionError("curl: (56) Connection closed abruptly")
            return Resp(200, FAKE_PDF)
        store = FakeStore([ident()])
        with mock.patch("paperlite.consumer._RESET_RETRY_WAIT_S", 0):
            dl = self.run_with(store, {"1.2.3.4": flaky}, ["https://dl.acm.org/doi/pdf/10.1145/1"])
        self.assertEqual((dl.stats.success, len(calls), store.discarded), (1, 2, []))
        self.assertEqual(self.row("https://dl.acm.org/doi/pdf/10.1145/1")["retry_count"], 0)

    def test_idle_worker_returns_identity_and_stops_demand(self):
        # 只剩别的进程/线程的在途任务时：不登记需求、身份还回池里，等它结束后退出
        self.db.import_urls([{"pdf_url": "https://dl.acm.org/doi/pdf/10.1145/1"}])
        other = self.db.claim("other-worker")
        store = FakeStore([ident()])
        dl = Downloader(self.cfg, store, PROXY, self.db, self.out, workers=1)
        timer = threading.Timer(0.3, lambda: self.db.finish(other, Status.SUCCESS))
        timer.start()
        with mock.patch("paperlite.consumer._IDLE_WAIT_S", 0.05):
            dl.run()
        self.assertEqual(getattr(store, "alive_calls", 0), 0)
        self.assertEqual(store.ready and len(store.ready), 1)   # 身份没被领走

    def test_volume_stop_saves_requests(self):
        calls = []

        def site(u):
            calls.append(u)
            return Resp(404, b"<title>ACM Error: 404</title>")
        self.cfg = Limits(request_gap_s=(0, 0), volume_stop_after=3)
        urls = [f"https://dl.acm.org/doi/pdf/10.1145/1012326.{1012327 + i}" for i in range(11)]
        dl = self.run_with(FakeStore([ident()]), {"1.2.3.4": site}, urls)
        self.assertEqual(len(calls), 3)                     # 11 篇只请求了 3 篇
        self.assertEqual((dl.stats.non_pdf, dl.stats.volumes_stopped, dl.stats.inferred), (3, 1, 8))

    def test_streamed_pdf_strips_leading_junk(self):
        body = b"\r\n\xef\xbb\xbf%PDF-1.7\n" + b"x" * 5000
        url = "https://dl.acm.org/doi/pdf/10.1145/1"
        dl = self.run_with(FakeStore([ident()]), {"1.2.3.4": lambda u: Resp(200, body)}, [url])
        r = self.row(url)
        data = Path(r["file_path"]).read_bytes()
        self.assertTrue(data.startswith(b"%PDF-1.7"))          # 前导换行和 BOM 已去掉
        self.assertEqual((dl.stats.success, r["file_size"], len(data)), (1, len(body) - 5, len(body) - 5))
        self.assertFalse(list(self.out.rglob("*.part")))

    def test_too_large_pdf_aborted(self):
        self.cfg = Limits(request_gap_s=(0, 0), max_file_size_mb=1)
        url = "https://dl.acm.org/doi/pdf/10.1145/big"
        dl = self.run_with(FakeStore([ident()]), {"1.2.3.4": lambda u: Resp(200, b"%PDF-1.4" + b"x" * (1024 * 1024 + 10))}, [url])
        r = self.row(url)
        self.assertEqual((r["status"], r["error_kind"], dl.stats.failed), ("failed", "extract_failed", 1))
        self.assertFalse(list(self.out.rglob("*.pdf")) + list(self.out.rglob("*.part")))

    def test_mid_stream_reset_retried_and_no_partial_left(self):
        calls = []

        def site(u):
            calls.append(u)
            return Resp(200, b"%PDF-1.4" + b"x" * 5000, fail_after=2000 if len(calls) == 1 else None)
        with mock.patch("paperlite.consumer._RESET_RETRY_WAIT_S", 0):
            dl = self.run_with(FakeStore([ident()]), {"1.2.3.4": site}, ["https://dl.acm.org/doi/pdf/10.1145/1"])
        self.assertEqual((dl.stats.success, len(calls)), (1, 2))
        self.assertFalse(list(self.out.rglob("*.part")))

    def test_html_task_saves_full_page(self):
        url = "https://www.sciencedirect.com/science/article/abs/pii/0378436377901723?via%3Dihub"
        page = b"<html><title>Measurements of spin</title>" + b"a" * (600 * 1024) + b"</html>"
        dl = self.run_with(FakeStore([ident()]), {"1.2.3.4": lambda u: Resp(200, page)}, [url], url_type="html")
        self.assertEqual((dl.stats.success, dl.stats.non_pdf), (1, 0))
        r = self.row(url)
        self.assertEqual((r["status"], r["url_type"], r["file_size"], r["error_kind"]), ("success", "html", len(page), None))
        self.assertEqual((r["title"], r["error_message"]), ("Measurements of spin", None))   # 标题进 title，error_message 为空
        self.assertEqual(Path(r["file_path"]).suffix, ".html")
        self.assertTrue(Path(r["file_path"]).is_absolute())                  # file_path 存绝对路径
        self.assertEqual(Path(r["file_path"]).read_bytes(), page)            # 整页，不截断到 256KB
        self.assertFalse((self.out / "non_pdf").exists())

    def test_html_task_404_fails_and_challenge_drops_identity(self):
        store = FakeStore([ident(exit_ip="1.1.1.1"), ident(exit_ip="2.2.2.2")])
        ok, gone = "https://dl.acm.org/doi/10.1145/1", "https://dl.acm.org/doi/10.1145/404"
        dl = self.run_with(store, {
            "1.1.1.1": lambda u: Resp(403, b"<html>challenge-platform</html>", {"cf-mitigated": "challenge"}),
            "2.2.2.2": lambda u: Resp(404, b"<title>Not Found</title>") if u == gone else Resp(200, b"<title>ok</title>"),
        }, [ok, gone], url_type="html")
        self.assertEqual((dl.stats.success, dl.stats.failed, store.discarded), (1, 1, ["1.1.1.1"]))
        r = self.row(gone)
        self.assertEqual((r["status"], r["error_kind"], r["http_status"]), ("failed", "not_found", 404))

    def test_html_read_is_capped(self):
        url = "https://dl.acm.org/doi/pdf/10.1145/huge-html"
        dl = self.run_with(FakeStore([ident()]),
                           {"1.2.3.4": lambda u: Resp(200, b"<title>Paper</title>" + b"a" * (1024 * 1024))}, [url])
        self.assertEqual(dl.stats.non_pdf, 1)
        self.assertLessEqual(self.row(url)["file_size"], 256 * 1024)

    def test_network_error_retries_then_fails(self):
        def boom(u):
            raise TimeoutError("curl: (28) Operation timed out")
        self.cfg = Limits(request_gap_s=(0, 0), max_tries=2)
        url = "https://dl.acm.org/doi/pdf/10.1145/x"
        dl = self.run_with(FakeStore([ident()]), {"1.2.3.4": boom}, [url])
        self.assertEqual((dl.stats.success, dl.stats.failed), (0, 1))
        r = self.row(url)
        self.assertEqual((r["status"], r["error_kind"], r["retry_count"]), ("failed", "network", 1))


class BrowserPoolTest(unittest.TestCase):
    """浏览器下载模式：用假 BrowserSession / IpSource 驱动，不开真浏览器。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.db = TaskDB(self.out / "tasks.db")
        self.cfg = Limits(page_interval_s=(0, 0), pass_timeout_s=5, min_left_s=1, max_url_challenges=2)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_pool(self, responses, urls, workers=1, n_leases=3):
        from paperlite import browser_pool as bp
        from paperlite.browser import RenderResult
        from paperlite.proxy import ProxyUnavailable

        self.db.import_urls({"pdf_url": u, "url_type": "html"} for u in urls)

        class FakeSession:
            def __init__(s, proxy, headed=False, geoip=False, webrtc_spoof=True):
                s.proxy = proxy
            async def start(s):
                pass
            async def render(s, url, timeout_s, settle_s=2.0):
                return responses(url)
            async def close(s):
                pass

        leases = iter([Lease(server=f"1.2.3.{i}:1", exit_ip=f"1.2.3.{i}", area="", expires_at=time.time() + 9999)
                       for i in range(1, n_leases + 1)])

        class FakeIp:
            def __init__(s, proxy):
                pass
            def extract_checked(s):
                try:
                    return next(leases)
                except StopIteration:
                    raise ProxyUnavailable("no more test IPs")

        with mock.patch.object(bp, "BrowserSession", FakeSession), mock.patch.object(bp, "IpSource", FakeIp):
            pool = BrowserPool(self.cfg, PROXY, self.db, self.out, workers)
            pool.run()
        return pool

    def row(self, url):
        return next(r for r in self.db.rows() if r["url"] == url)

    def _ok(self, url):
        from paperlite.browser import RenderResult
        final = url + "#final"
        return RenderResult(ok=True, seconds=1.0, url=final, status=200, title="Paper", html=f"<html>{url}</html>")

    def test_downloads_pages_in_browser(self):
        urls = [f"https://www.sciencedirect.com/science/article/abs/pii/{i}" for i in range(3)]
        pool = self.run_pool(self._ok, urls)
        self.assertEqual((pool.stats.success, pool.stats.failed), (3, 0))
        self.assertEqual(pool.stats.browsers, 1)                 # 一个浏览器连下三篇
        r = self.row(urls[0])
        self.assertEqual((r["status"], r["url_type"]), ("success", "html"))
        self.assertEqual(r["final_url"], urls[0] + "#final")
        self.assertEqual(Path(r["file_path"]).suffix, ".html")
        self.assertEqual(Path(r["file_path"]).read_bytes(), f"<html>{urls[0]}</html>".encode())

    def test_realtime_inspector_stops_matching_pending_pages(self):
        from paperlite.browser import RenderResult
        self.cfg = Limits(page_interval_s=(0, 0), pass_timeout_s=5, min_left_s=1,
                              title_dup_trigger=3, title_window_size=10,
                              rule_min_support=3, rule_min_precision=1.0)
        urls = [f"https://example.org/cover/{i}" for i in range(6)]

        def cover(url):
            return RenderResult(ok=True, seconds=1.0, url=url, status=200,
                                title="Outside Front Cover", html="<html>cover</html>")

        pool = self.run_pool(cover, urls)
        self.assertEqual(pool.stats.success, 3)
        rows = self.db.rows()
        self.assertEqual(sum(r["status"] == "success" for r in rows), 3)
        self.assertEqual(self.db.suspect_count(), 3)

    def test_404_fails_without_retry(self):
        from paperlite.browser import RenderResult
        gone = "https://www.sciencedirect.com/science/article/abs/pii/404"
        def resp(url):
            if url == gone:
                return RenderResult(ok=True, seconds=1.0, url=url, status=404, title="Not Found", html="x")
            return self._ok(url)
        pool = self.run_pool(resp, ["https://www.sciencedirect.com/science/article/abs/pii/ok", gone])
        self.assertEqual((pool.stats.success, pool.stats.failed), (1, 1))
        r = self.row(gone)
        self.assertEqual((r["status"], r["error_kind"], r["http_status"]), ("failed", "not_found", 404))

    def test_render_failure_retries_then_fails(self):
        from paperlite.browser import RenderResult
        url = "https://www.sciencedirect.com/science/article/abs/pii/stuck"
        def resp(u):
            if u == self.cfg.landing_for("www.sciencedirect.com"):
                return self._ok(u)                               # 落地页能过盾
            return RenderResult(ok=False, seconds=5.0, error="TimeoutError: 页面未稳定")
        pool = self.run_pool(resp, [url], n_leases=9)
        self.assertEqual((pool.stats.success, pool.stats.failed), (0, 1))
        r = self.row(url)
        self.assertEqual((r["status"], r["error_kind"], r["challenge_count"]), ("failed", "challenge_failed", 2))

    def test_proxy_net_error_switches_ip_without_counting(self):
        """IP 中途失效（ERR_TUNNEL_CONNECTION_FAILED）：不计挑战次数、任务放回；连续 2 次就换 IP。"""
        from paperlite.browser import RenderResult
        landing = self.cfg.landing_for("www.sciencedirect.com")
        url = "https://www.sciencedirect.com/science/article/abs/pii/net"
        dead = {"n": 2}                                          # 第一个 IP 上的前两次文章渲染都是隧道错误
        def resp(u):
            if u != landing and dead["n"] > 0:
                dead["n"] -= 1
                return RenderResult(ok=False, seconds=0.1,
                                    error=f"Error: Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at {u}")
            return self._ok(u)
        pool = self.run_pool(resp, [url])                        # max_url_challenges=2：若计数早就判死了
        self.assertEqual((pool.stats.success, pool.stats.failed), (1, 0))
        self.assertEqual(pool.stats.browsers, 2)                 # 换过一次 IP
        r = self.row(url)
        self.assertEqual((r["status"], r["challenge_count"], r["retry_count"]), ("success", 0, 0))

    def test_is_proxy_net_error(self):
        from paperlite.browser_pool import is_proxy_net_error
        for e in ("Error: Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x", "net::ERR_PROXY_CONNECTION_FAILED",
                  "net::ERR_CONNECTION_CLOSED", "net::ERR_CONNECTION_RESET", "net::ERR_EMPTY_RESPONSE"):
            self.assertTrue(is_proxy_net_error(e), e)
        for e in ("TimeoutError: 90s 内页面未稳定", "net::ERR_NAME_NOT_RESOLVED", "", None):
            self.assertFalse(is_proxy_net_error(e), e)


@unittest.skipUnless(os.environ.get("PAPERLITE_TEST_REDIS"), "设置 PAPERLITE_TEST_REDIS=redis://... 才跑")
class RedisStoreTest(unittest.TestCase):
    def setUp(self):
        from paperlite.store import IdentityStore
        self.store = IdentityStore(os.environ["PAPERLITE_TEST_REDIS"], prefix=f"cp_test_{uuid.uuid4().hex[:6]}")
        self.host = "dl.acm.org"

    def tearDown(self):
        for k in self.store.r.scan_iter(f"{self.store.prefix}:*"):
            self.store.r.delete(k)

    def test_claim_is_exclusive_and_prefers_soonest_expiry(self):
        late, soon = ident(exit_ip="9.9.9.9", expires_at=time.time() + 500), ident(expires_at=time.time() + 200)
        self.store.add(late)
        self.store.add(soon)
        a = self.store.claim(self.host, "w1", min_left_s=20, lease_ttl_s=60)
        b = self.store.claim(self.host, "w2", min_left_s=20, lease_ttl_s=60)
        self.assertEqual((a.exit_ip, b.exit_ip), ("1.2.3.4", "9.9.9.9"))
        self.assertIsNone(self.store.claim(self.host, "w3", 20, 60))
        self.assertEqual(a.cookies, soon.cookies)

    def test_min_left_skips_dying_identity(self):
        self.store.add(ident(expires_at=time.time() + 10))
        self.assertIsNone(self.store.claim(self.host, "w", min_left_s=20, lease_ttl_s=60))

    def test_release_discard_and_reap(self):
        self.store.add(ident())
        a = self.store.claim(self.host, "w", 20, 60)
        self.assertEqual(self.store.stats(self.host).inuse, 1)
        self.store.release(a)
        self.assertEqual(self.store.stats(self.host).ready, 1)
        a = self.store.claim(self.host, "w", 20, 60)
        self.store.r.delete(self.store._k(self.host, "lease", a.id))     # 模拟占用者崩溃
        self.assertEqual(self.store.reap(self.host), 1)
        a = self.store.claim(self.host, "w", 20, 60)
        self.store.discard(a)
        st = self.store.stats(self.host)
        self.assertEqual((st.ready, st.inuse), (0, 0))

    def test_single_producer_lock(self):
        self.assertTrue(self.store.hold_producer(self.host, "p1", 15))
        self.assertTrue(self.store.hold_producer(self.host, "p1", 15))      # 续期
        self.assertFalse(self.store.hold_producer(self.host, "p2", 15))     # 待命
        self.store.release_producer(self.host, "p2")                        # 不是持有者，释放无效
        self.assertEqual(self.store.producer_holder(self.host), "p1")
        self.store.release_producer(self.host, "p1")
        self.assertTrue(self.store.hold_producer(self.host, "p2", 15))      # 接手

    def test_breaker_counters(self):
        for _ in range(3):
            self.store.note_fresh_reject(self.host)
        self.assertEqual(self.store.fresh_rejects(self.host), 3)
        self.store.note_success(self.host)
        self.assertEqual(self.store.fresh_rejects(self.host), 0)
        self.store.trip(self.host, 60, threshold=5)
        self.assertGreater(self.store.paused_for(self.host), 50)
        self.assertEqual(self.store.fresh_rejects(self.host), 4)   # 恢复后再被拒一个就再熔断

    def test_stats_counts_workers_and_healthy(self):
        self.store.add(ident(expires_at=time.time() + 60))
        self.store.add(ident(expires_at=time.time() + 600))
        self.store.worker_alive(self.host, "w1")
        st = self.store.stats(self.host, refill_before_s=90)
        self.assertEqual((st.ready, st.healthy, st.workers), (2, 1, 1))
        self.store.worker_gone(self.host, "w1")
        self.assertEqual(self.store.stats(self.host).workers, 0)


if __name__ == "__main__":
    unittest.main()


# ── turnstile ──

def test_turnstile_parse_proxy():
    from paperlite.turnstile import parse_proxy
    assert parse_proxy("") is None
    assert parse_proxy("http://1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}
    assert parse_proxy("socks5://u:p%40@h:1080") == {"server": "socks5://h:1080", "username": "u", "password": "p%40"}
    import pytest
    with pytest.raises(ValueError):
        parse_proxy("1.2.3.4")


def test_turnstile_checkbox_point_inside_widget():
    from paperlite.turnstile import checkbox_point
    for box in ({"x": 100, "y": 200, "width": 300, "height": 65}, {"x": 0, "y": 0, "width": 150, "height": 140}):
        for _ in range(50):
            x, y = checkbox_point(box)
            assert box["x"] + 10 < x < box["x"] + 50
            assert box["y"] + 10 < y < box["y"] + box["height"] - 10


def test_turnstile_harness_escapes_params():
    import json
    from paperlite.turnstile import harness_html
    html = harness_html("0x4AAA", action="login", cdata='a"</script><b>')
    assert '"sitekey": "0x4AAA"' in html and '"action": "login"' in html
    assert "</script><b>" not in html
    assert json.loads(json.dumps('a"</script><b>').replace("<", "\\u003c")) == 'a"</script><b>'
    assert json.dumps('a"</script><b>').replace("<", "\\u003c") in html


# ── 网页任务：跳转与挑战判断 ──

def test_meta_refresh_target():
    from paperlite.consumer import meta_refresh_target
    linkinghub = (b'<html><head><META HTTP-EQUIV="REFRESH" content="2; url=\'/retrieve/articleSelectPrefsTemp?'
                  b'Redirect=http%3A%2F%2Fjpsmjournal.com%2Fretrieve%2Fpii%2FS0885392416309782&amp;key=9361\'"/>'
                  b'<title>Redirecting</title></head></html>')
    assert meta_refresh_target(linkinghub, "https://linkinghub.elsevier.com/retrieve/pii/S0885392416309782") == (
        "https://linkinghub.elsevier.com/retrieve/articleSelectPrefsTemp?"
        "Redirect=http%3A%2F%2Fjpsmjournal.com%2Fretrieve%2Fpii%2FS0885392416309782&key=9361")
    assert meta_refresh_target(b'<meta content="0;URL=https://b.org/x" http-equiv=refresh>', "https://a.org/") == "https://b.org/x"
    assert meta_refresh_target(b'<meta http-equiv="refresh" content="300">', "https://a.org/") is None          # 定时刷新
    assert meta_refresh_target(b'<meta http-equiv="refresh" content="600; url=/x">', "https://a.org/") is None
    assert meta_refresh_target(b'<meta http-equiv="X-UA-Compatible" content="IE=edge">', "https://a.org/") is None


def test_cf_probe_scripts_are_not_challenge():
    from paperlite.consumer import is_challenge
    normal = b'<html><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>' \
             b'<script src="/cdn-cgi/challenge-platform/scripts/precursor"></script></html>'
    assert not is_challenge({}, normal)
    page = b'<html><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=1"></script></html>'
    assert is_challenge({}, page)
    assert is_challenge({"cf-mitigated": "challenge"}, b"")


# ──────────────────────────────────────────────────────────────────────────
# 命令行契约：断言 README「命令」「主要配置」两节描述的子命令、参数、默认值、
# choices 与真实的 argparse 定义一致。只测参数解析（纯函数），不碰 Redis/网络。
# 改了 CLI 的命令集或参数默认值而没更新 README 时，这里会红。
# ──────────────────────────────────────────────────────────────────────────
class CliArgsTest(unittest.TestCase):
    def setUp(self):
        import contextlib
        import io
        from paperlite.cli import _build_parser
        self.ap, self.parsers = _build_parser()
        self._contextlib, self._io = contextlib, io

    def parse(self, *argv):
        return self.ap.parse_args(list(argv))

    def fails(self, *argv):
        """argparse 出错会 print_usage 到 stderr 再 sys.exit —— 吞掉输出，断言退出。"""
        buf = self._io.StringIO()
        with self.assertRaises(SystemExit), self._contextlib.redirect_stderr(buf):
            self.ap.parse_args(list(argv))

    # README 命令表列出的子命令（help 是帮助本身）
    def test_command_set_matches_readme(self):
        from paperlite.cli import _COMMANDS
        self.assertEqual(
            set(_COMMANDS),
            {"init", "import", "run", "produce", "download", "status",
             "inspect", "retry", "export", "turnstile", "help"},
        )
        # 每个命令都建了子解析器（help 自身也在内）
        self.assertEqual(set(self.parsers), set(_COMMANDS))

    # README 快速开始/脚注：--version、--config、--log-level 默认 INFO
    def test_global_flags(self):
        self.assertEqual(self.parse("status").log_level, "INFO")
        self.assertIsNone(self.parse("status").config)
        self.fails("--version")          # version action 直接退出
        self.fails("不是命令")            # 未知命令报错退出

    # 所有操作命令可加 --db 指定任务库（覆盖 [paths].db）；没有 --out/--host 了
    def test_db_arg_on_commands(self):
        for name in ("import", "run", "produce", "download", "status", "inspect", "retry", "export"):
            argv = [name] + (["--urls", "x.txt"] if name == "import" else
                             ["--status", "failed"] if name == "retry" else [])
            self.assertEqual(self.parse(*argv, "--db", "d.db").db, "d.db", name)
        self.assertEqual(self.parse("run", "--storage", "/mnt/x").storage, "/mnt/x")   # run/download 另有 --storage
        self.fails("status", "--out", "o")        # --out 已移除
        self.fails("import", "--host", "h")       # --host 已移除

    # README import 行：--urls 必填；--type 为 pdf|html 默认 pdf
    def test_import(self):
        from paperlite.tasks import UrlType
        self.fails("import")                                   # 缺 --urls
        ns = self.parse("import", "--urls", "acm.txt")
        self.assertEqual(ns.type, UrlType.PDF)
        self.assertEqual(self.parse("import", "--urls", "x", "--type", "html").type, "html")
        self.fails("import", "--urls", "x", "--type", "xml")   # 非法 choice

    # README run 行：--workers 默认 20；--curl/--browser 互斥；--no-proxy；--type
    def test_run(self):
        ns = self.parse("run")
        self.assertEqual(ns.workers, 20)
        self.assertFalse(ns.browser or ns.curl or ns.no_proxy)
        self.assertEqual(ns.type, "pdf")
        self.assertIsNone(ns.config_file)                      # 裸 run：无配置文件，用 flag
        self.assertTrue(self.parse("run", "--browser", "--no-proxy").no_proxy)
        self.fails("run", "--browser", "--curl")               # 互斥

    # README「配置文件启动」：paperlite run config.toml 按 [run] 启动
    def test_run_config_file_positional(self):
        self.assertEqual(self.parse("run", "config.toml").config_file, "config.toml")

    # 默认模式 browser：run 子命令不带标志、[run] 省略 mode 都走浏览器
    def test_default_mode_is_browser(self):
        from paperlite.config import RunConfig
        from paperlite.cli import _configured_run_args
        import argparse
        self.assertEqual(RunConfig().mode, "browser")
        ns = self.parse("run")
        self.assertFalse(ns.curl or ns.browser)               # 无标志；分发按 "not --curl" → 浏览器分支
        mapped = _configured_run_args(argparse.Namespace(config=None, log_level="INFO"),
                                      RunConfig(command="run"))
        self.assertTrue(mapped.browser and not mapped.curl)   # [run] 省略 mode → browser

    # README：produce 常驻，--keep 覆盖 min_identities
    def test_produce(self):
        self.assertIsNone(self.parse("produce").keep)
        self.assertEqual(self.parse("produce", "--keep", "20").keep, 20)

    # README：download 需 --workers（默认 20），可选 --urls
    def test_download(self):
        self.assertEqual(self.parse("download").workers, 20)
        self.assertEqual(self.parse("download", "--workers", "8").workers, 8)

    # README retry：必须二选一；--status 只接受 failed/non_pdf；--kind 配合
    def test_retry(self):
        from paperlite.cli import ERROR_KINDS
        self.fails("retry")                                    # 缺 required 互斥组
        self.assertEqual(self.parse("retry", "--status", "failed").status, "failed")
        self.assertEqual(self.parse("retry", "--status", "non_pdf").status, "non_pdf")
        self.assertTrue(self.parse("retry", "--stopped-volumes").stopped_volumes)
        self.fails("retry", "--status", "failed", "--stopped-volumes")   # 互斥
        self.fails("retry", "--status", "pending")             # 非法 choice
        self.assertIn("network", ERROR_KINDS)
        self.assertEqual(self.parse("retry", "--status", "failed", "--kind", "network").kind, "network")

    # README export：--status 为全部状态之一；--file 默认标准输出（-）
    def test_export(self):
        from paperlite.tasks import Status
        self.assertEqual(self.parse("export").file, "-")
        for s in Status.ALL:
            self.assertEqual(self.parse("export", "--status", s).status, s)
        self.fails("export", "--status", "bogus")

    # README inspect 行：--trigger/--min-precision/--min-support/--list/--reset
    def test_inspect(self):
        ns = self.parse("inspect", "--trigger", "10", "--min-precision", "0.9",
                        "--min-support", "5", "--list", "--reset")
        self.assertEqual((ns.trigger, ns.min_precision, ns.min_support), (10, 0.9, 5))
        self.assertTrue(ns.list and ns.reset)

    # README Turnstile 节：--url/--sitekey、--serve 起服务、--json、默认值
    def test_turnstile(self):
        ns = self.parse("turnstile", "--url", "https://x/", "--sitekey", "0x4A")
        self.assertEqual((ns.timeout, ns.concurrency, ns.bind), (60, 4, "127.0.0.1"))
        self.assertFalse(ns.json)
        self.assertEqual(self.parse("turnstile", "--serve", "8191").serve, 8191)
        self.assertTrue(self.parse("turnstile", "--url", "x", "--sitekey", "k", "--json").json)

    # README：init --force
    def test_init(self):
        self.assertFalse(self.parse("init").force)
        self.assertTrue(self.parse("init", "--force").force)


# ──────────────────────────────────────────────────────────────────────────
# 代理分层体检：经代理探判定目标（curl 低层，拿 TCP/TLS/TTFB/总耗时四段），要求
# 连通+TLS+2xx → 回显出口 IP == proxy_ip → 延迟不超阈值；两个目标任一通过即合格。
# 用 mock _probe 测决策逻辑，不走真实网络。
# ──────────────────────────────────────────────────────────────────────────
class ProxyCheckTest(unittest.TestCase):
    def _src(self, **kw):
        from paperlite.proxy import IpSource
        return IpSource(ProxySettings(api_url="https://api/x", **kw))

    def _lease(self, ip="1.2.3.4"):
        from paperlite.proxy import Lease
        return Lease(server="1.2.3.4:8000", exit_ip=ip, area="", expires_at=time.time() + 600)

    def _ok(self, egress="1.2.3.4", total_ms=500.0, status=200):
        from paperlite.proxy import ProbeResult
        return ProbeResult(ok=True, status=status, tcp_ms=30, tls_ms=200, ttfb_ms=total_ms * 0.8,
                           total_ms=total_ms, egress=egress)

    def _fail(self, error="CurlError: connect"):
        from paperlite.proxy import ProbeResult
        return ProbeResult(ok=False, error=error)

    def test_extract_ip(self):
        from paperlite.proxy import _extract_ip
        self.assertEqual(_extract_ip("fl=1f\nip=203.0.113.7\nts=1.2"), "203.0.113.7")   # cloudflare trace
        self.assertEqual(_extract_ip('{\n  "origin": "203.0.113.7"\n}'), "203.0.113.7")  # httpbin/ip
        self.assertEqual(_extract_ip("no ip here"), "")

    def test_passes_on_first_good_judge(self):
        src = self._src()
        with mock.patch.object(src, "_probe", return_value=self._ok("1.2.3.4")) as probe:
            self.assertTrue(src._check(self._lease("1.2.3.4")))
            self.assertEqual(probe.call_count, 1)            # 首个目标就过，不再探第二个

    def test_connect_fail_rejected(self):
        src = self._src()
        with mock.patch.object(src, "_probe", return_value=self._fail()) as probe:
            self.assertFalse(src._check(self._lease()))
            self.assertEqual(probe.call_count, 2)            # 两个目标都探过才判死

    def test_bad_status_rejected(self):
        src = self._src()
        with mock.patch.object(src, "_probe", return_value=self._ok(status=403)):
            self.assertFalse(src._check(self._lease("1.2.3.4")))

    def test_exit_ip_mismatch_rejected(self):
        src = self._src()
        with mock.patch.object(src, "_probe", return_value=self._ok(egress="9.9.9.9")):
            self.assertFalse(src._check(self._lease("1.2.3.4")))   # 回显出口≠proxy_ip

    def test_latency_gate_rejects_slow(self):
        src = self._src(check_max_latency_ms=1000)
        with mock.patch.object(src, "_probe", return_value=self._ok(total_ms=2000)):
            self.assertFalse(src._check(self._lease("1.2.3.4")))   # 两目标都超阈值

    def test_second_judge_denoise(self):
        src = self._src()
        with mock.patch.object(src, "_probe", side_effect=[self._fail(), self._ok("1.2.3.4")]):
            self.assertTrue(src._check(self._lease("1.2.3.4")))    # 首个目标挂、备用好 → 整体通过

    def test_ip_verify_skipped_when_unparsed(self):
        src = self._src()                                    # verify_exit_ip 默认 True
        with mock.patch.object(src, "_probe", return_value=self._ok(egress="")):
            self.assertTrue(src._check(self._lease("1.2.3.4")))    # 抠不出回显 IP 时不因此误杀

    def test_extract_checked_retries_then_raises(self):
        from paperlite.proxy import ProxyUnavailable
        src = self._src(check_max_tries=3)
        with mock.patch.object(src, "_extract", return_value=self._lease()), \
             mock.patch.object(src, "_check", return_value=False) as chk:
            with self.assertRaises(ProxyUnavailable):
                src.extract_checked()
            self.assertEqual(chk.call_count, 3)              # 连续 3 个不过才放弃


class GeoipGateTest(unittest.TestCase):
    """geoip=true 但 GeoIP 库缺失时，启动前报错而非卡死下载。"""

    def test_require_geoip(self):
        from paperlite.cli import _require_geoip
        from paperlite.config import Limits
        _require_geoip(Limits(geoip=False))                                  # 关：不检查、不报错
        with mock.patch("paperlite.browser.ensure_geoip_db", return_value=(True, "/x/db.mmdb", "u")):
            _require_geoip(Limits(geoip=True))                               # 就位（或下成功）：通过
        with mock.patch("paperlite.browser.ensure_geoip_db",
                        return_value=(False, "/x/GeoLite2-City.mmdb", "https://example/db")):
            with self.assertRaises(SystemExit) as e:
                _require_geoip(Limits(geoip=True))                           # 下不动：报错
            msg = str(e.exception)
            self.assertIn("GeoLite2-City.mmdb", msg)        # 指明文件
            self.assertIn("https://example/db", msg)        # 给出下载地址
            self.assertIn("geoip = false", msg)             # 给出关闭选项
