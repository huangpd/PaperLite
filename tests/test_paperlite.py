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
from paperlite.consumer import Downloader, impersonate_for, rate_limit_wait
from paperlite.cookies import jar_from_records, jar_to_records
from paperlite.inspect import WindowInspector
from paperlite.logutil import JsonFormatter
from paperlite.producer import Producer
from paperlite.proxy import Lease, ProxySettings, parse_deadline
from paperlite.store import Identity
from paperlite.tasks import Status, UrlType, derive_doi, pdf_path, url_key, url_problem
from tests.pgutil import DSN, execute, make_source, needs_pg, new_schema, pg_config, row as pg_row, rows as pg_rows

FAKE_PDF = b"%PDF-1.4\n" + b"0" * 2048          # 超过最小文件大小（1024 字节）
PROXY = ProxySettings(api_url="https://api.example/get?key=k", username="u", password="p@ss")


def ident(**kw) -> Identity:
    d = dict(host="dl.acm.org", exit_ip="1.2.3.4", proxy_server="1.2.3.4:8000", user_agent="Mozilla/5.0 Chrome/146.0",
             cookies=[{"name": "cf_clearance", "value": "v", "domain": ".acm.org", "path": "/"}],
             expires_at=time.time() + 600)
    d.update(kw)
    d["cookies"] = jar_from_records(d["cookies"])
    return Identity(**d)


class ConfigTest(unittest.TestCase):
    def test_from_dict(self):
        cfg = Limits.from_dict({"request_gap_s": [1, 2]})
        self.assertEqual(cfg.request_gap_s, (1.0, 2.0))

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
            f.write('[proxy]\napi_url = "https://a/"\n[limits]\nmax_browsers = 3\n')
        try:
            cfg, proxy = load(Path(f.name))
            self.assertEqual((cfg.max_browsers, proxy.api_url), (3, "https://a/"))
            with open(f.name, "a") as g:
                g.write('[landing]\n"h.org" = "https://h.org/x"\n')
            with self.assertRaisesRegex(ValueError, r"\[landing\] 已移除"):      # 不再有固定落地页
                load(Path(f.name))
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


class UrlProblemTest(unittest.TestCase):
    """导入时剔除明显下载不到的链接（来自 hw1 两个批次里的真实坏数据）。"""

    def test_obviously_bad(self):
        cases = {
            "https://10.65.16.77/index.php/sociobiology/article/view/3300": "private_ip",
            "http://192.168.1.111/index.php/ForoEducacional/article/view/779": "private_ip",
            "https://192.168.7.25:443/index.php/assc/article/view/6870": "private_ip",
            "http://172.20.1.1/a": "private_ip",
            "http://127.0.0.1/a": "private_ip",
            "http://[::1]/a": "private_ip",
            "https://.asm.org/content/177/11/3358": "bad_host",
            "http://0": "bad_host",
            "http://null/10.1017/S0022112011000036": "bad_host",
            "http://http://sociologiaitaliana.egeaonline.it/it/21": "bad_host",
            "https://opgadmin/oe/abstract.cfm?doi=10.1364/OE.473012": "bad_host",
            "https://x..org/a": "bad_host",
            "http://x.org:abc/": "bad_host",
            "ftp://ftp.example.org/a.pdf": "bad_scheme",
            "http:///path": "no_host",
            "": "bad_scheme",
        }
        for url, reason in cases.items():
            self.assertEqual(url_problem(url), reason, url)

    def test_normal_kept(self):
        for url in ("https://www.sciencedirect.com/science/article/pii/S0092867424000011",
                    "https://linkinghub.elsevier.com/retrieve/pii/0001691855900019",
                    "HTTPS://WWW.Nature.COM/articles/x", "https://example.com./a", "https://a_b.example.com/x",
                    "https://xn--fiqs8s.cn/a",
                    "http://219.238.6.200/x", "http://155.206.11.181:90/content/x",     # 公网 IP 建站：放行
                    "http://10040.indexcopernicus.com/abstracted.php?level=5&ICID=1138423"):   # 合法但打不开：交给下载
            self.assertIsNone(url_problem(url), url)


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

        from paperlite.inspect import inspect
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
        from paperlite.inspect import inspect
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


@needs_pg
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
        db = make_source(self)
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
        db = make_source(self)
        db.import_urls([{"url": "https://www.sciencedirect.com/science/article/abs/pii/S2"}])
        pool = BrowserPool(Limits(), None, db, Path(tmp.name) / "out", workers=1, no_proxy=True)
        pool.inspector = mock.Mock()
        task = db.claim("w")
        r = RenderResult(ok=False, seconds=90.0, title="Just a moment...", error="TimeoutError")
        session = SimpleNamespace(render=mock.AsyncMock(return_value=r))
        lease = Lease(server="x", exit_ip="1.2.3.4", area="", expires_at=time.time() + 600)
        asyncio.run(pool._do_task(task, session, lease, "t"))
        pool.inspector.observe.assert_not_called()


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
        from paperlite.pgqueue import Result
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


class CliTest(unittest.TestCase):
    """命令行：配置文件里的 [pg] 就是任务队列；没有 [pg] 报错。"""

    def setUp(self):
        # 在空的临时目录里跑：不读项目根目录的 config.toml
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
        self.assertEqual(set(raw), {"pg", "proxy", "limits", "paths", "run", "monitor"})
        self.assertEqual(set(raw["pg"]), {"dsn", "batch"})
        self.assertFalse(raw["monitor"]["enabled"])               # 模板默认不上报
        with self.assertRaises(SystemExit):                     # 已存在不覆盖
            self.run_cli("init")
        self.run_cli("init", "--force")

    def test_missing_pg_section_fails(self):
        Path("config.toml").write_text("[paths]\n")
        urls = Path("u.txt")
        urls.write_text("https://dl.acm.org/doi/pdf/10.1145/1.2\n")
        with self.assertRaises(SystemExit) as e:
            self.run_cli("import", "--urls", str(urls))
        self.assertIn("[pg]", str(e.exception))

    def test_removed_legacy_settings_explained(self):
        from paperlite import config
        for text, needle in (('[paths]\ndb = "t.db"\n', "[paths] db"), ("[queue]\nbackend = 'pg'\n", "[queue]"),
                             ("[run]\npg = true\n", "[run] pg"), ("[limits]\nvolume_stop_after = 3\n", "volume_stop_after"),
                             ('[landing]\n"h.org" = "https://h.org/"\n', "[landing]")):
            Path("c.toml").write_text(text)
            with self.assertRaises(ValueError) as e:
                config.load_limits(Path("c.toml")); config.load_paths(Path("c.toml")); config.load_run(Path("c.toml"))
            self.assertIn(needle, str(e.exception))

    @needs_pg
    def test_import_retry_export_status_via_pg(self):
        schema = new_schema(self)
        Path("config.toml").write_text(pg_config(schema, batch="cli"))
        f = Path("mixed.txt")
        f.write_text("https://dl.acm.org/doi/pdf/10.1145/1.2\nhttps://example.org/a.pdf\nhttp://192.168.1.111/x\n")
        self.run_cli("import", "--urls", str(f))
        bad = Path("mixed.txt.bad_url.csv").read_text().splitlines()
        self.assertEqual(bad, ["doi,url,reason", ",http://192.168.1.111/x,private_ip"])   # 文本文件没有 doi 列
        db = make_source(self, batch="cli", schema=schema)
        self.assertEqual(sorted(db.hosts()), ["dl.acm.org", "example.org"])   # 多主机同一批次
        self.assertEqual(db.counts()[Status.PENDING], 2)
        t = db.claim("w")
        db.finish(t, Status.FAILED, error_kind="network")
        db.close()
        self.run_cli("retry", "--status", "failed", "--kind", "network")
        csv_path = Path("x.csv")
        self.run_cli("export", "--file", str(csv_path))
        lines = csv_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("id,doi,url"))
        self.assertNotIn("failed", "".join(lines[1:]))                       # 已放回 pending
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.run_cli("status")
        self.assertIn("pending 2", buf.getvalue())
        self.assertIn("块:", buf.getvalue())


class ProbeUrlTest(unittest.TestCase):
    """不热身：curl 模式验收新身份、复验身份都用真实链接，不用配置的落地页。"""

    def producer(self, store, sample=None):
        from paperlite.producer import Producer
        return Producer(Limits(), store, mock.Mock(), "dl.acm.org", sample_url=sample)

    def test_producer_probe_priority(self):
        store = FakeStore([])
        p = self.producer(store, sample=lambda: "https://dl.acm.org/doi/pdf/10.1145/9")
        self.assertEqual(p._probe_url(), ("https://dl.acm.org/doi/pdf/10.1145/9", False))   # 队列里的待下载链接：宽松判定
        store.good = "https://dl.acm.org/doi/pdf/10.1145/1"
        self.assertEqual(p._probe_url(), (store.good, True))                              # 最近成功链接优先：严格判定
        self.assertEqual(self.producer(FakeStore([]))._probe_url(), ("https://dl.acm.org/", True))   # 都没有：首页
        broken = self.producer(FakeStore([]), sample=mock.Mock(side_effect=RuntimeError("pg down")))
        self.assertEqual(broken._probe_url(), ("https://dl.acm.org/", True))             # 取不到不影响生产

    def test_lenient_acceptance_with_pending_link(self):
        """队列里的待下载链接本身可能是 404：只要没被挑战、没被封，身份就算可用。"""
        p = self.producer(FakeStore([]), sample=lambda: "https://dl.acm.org/doi/pdf/10.1145/gone")
        p.ips.s = PROXY
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = Resp(404, b"<title>ACM Error: 404</title>")
        with mock.patch("paperlite.producer.session_for_identity", return_value=session):
            self.assertTrue(p._curl_usable(ident()))
            session.get.return_value = Resp(403, b"challenge-platform", {"cf-mitigated": "challenge"})
            self.assertFalse(p._curl_usable(ident()))


class ProducerTargetTest(unittest.TestCase):
    def test_target_follows_workers(self):
        p = Producer(Limits(max_identities=20), mock.Mock(), mock.Mock(), "dl.acm.org")
        self.assertEqual(p.target(0), 0)      # 没有下载线程：不生产
        self.assertEqual(p.target(8), 8)
        self.assertEqual(p.target(50), 20)

    def test_target_is_consumer_driven(self):
        """目标 = min(身份上限, 在线线程, 可领取任务)；旧配置里的 min_identities 不再保底"""
        with self.assertLogs("paperlite.producer", "WARNING"):
            p = Producer(Limits(max_identities=20, min_identities=5), mock.Mock(), mock.Mock(), "dl.acm.org")
        self.assertEqual(p.target(0), 0)                 # 没有消费者：保底已取消
        self.assertEqual(p.target(0, pending=100), 0)    # 有活没人下：也不产
        self.assertEqual(p.target(20, pending=3), 3)     # 只剩 3 篇：最多 3 个身份
        self.assertEqual(p.target(20, pending=0), 0)     # 活领完了：不产
        self.assertEqual(p.target(8, pending=100), 8)
        self.assertEqual(p.target(50, pending=100), 20)
        self.assertEqual(p.target(8, pending=None), 8)   # 旧版下载进程不上报：只按线程数


class ProducerReplayTest(unittest.TestCase):
    def test_rate_limit_wait_respects_identity_lifetime_and_cap(self):
        self.assertEqual(rate_limit_wait({"retry-after": "38"}, 100, 20), 38)
        self.assertIsNone(rate_limit_wait({"retry-after": "38"}, 50, 20))
        self.assertIsNone(rate_limit_wait({"retry-after": "120"}, 300, 20))
        self.assertIsNone(rate_limit_wait({}, 300, 20))

    def test_curl_acceptance_checks_page_and_denial(self):
        from paperlite import producer as producer_mod

        ips = mock.Mock()
        ips.s = PROXY
        producer = Producer(Limits(), mock.Mock(), ips, "dl.acm.org")
        cases = [
            (Resp(200, b"<title>Paper</title>"), True),
            (Resp(429, b"<title>Access denied | Cloudflare</title>", {"server": "cloudflare"}), False),
            (Resp(429, b"<title>Too Many Requests</title>"), None),
            (Resp(403, b"<title>Attention Required</title>", {"cf-mitigated": "challenge"}), False),
        ]
        for response, expected in cases:
            with self.subTest(status=response.status_code, body=response.content[:30]):
                session = mock.MagicMock()
                session.__enter__.return_value = session
                session.get.return_value = response
                with mock.patch.object(producer_mod, "session_for_identity", return_value=session):
                    self.assertEqual(producer._curl_usable(ident()), expected)

    def test_curl_429_waits_and_reuses_session(self):
        from paperlite import producer as producer_mod

        ips = mock.Mock()
        ips.s = PROXY
        producer = Producer(Limits(), mock.Mock(), ips, "dl.acm.org")
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.get.side_effect = [
            Resp(429, b"<title>Too Many Requests</title>", {"retry-after": "2"}),
            Resp(200, b"<title>ACM Digital Library</title>"),
        ]
        with mock.patch.object(producer_mod, "session_for_identity", return_value=session), \
                mock.patch.object(producer_mod.time, "sleep") as sleep:
            self.assertTrue(producer._curl_usable(ident()))
        sleep.assert_called_once_with(2.0)
        self.assertEqual(session.get.call_count, 2)

    def test_curl_cf_429_rejects_without_waiting(self):
        """验收遇到 Cloudflare 封禁的 429：直接判不可用（计入熔断），不等 Retry-After、不重试"""
        from paperlite import producer as producer_mod

        ips = mock.Mock()
        ips.s = PROXY
        producer = Producer(Limits(), mock.Mock(), ips, "dl.acm.org")
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = Resp(429, b"<title>Access denied | Cloudflare</title>",
                                        {"retry-after": "2", "server": "cloudflare"})
        with mock.patch.object(producer_mod, "session_for_identity", return_value=session), \
                mock.patch.object(producer_mod.time, "sleep") as sleep:
            self.assertIs(producer._curl_usable(ident()), False)
        sleep.assert_not_called()
        self.assertEqual(session.get.call_count, 1)

    def test_no_clearance_still_reaches_curl_acceptance(self):
        import asyncio
        from paperlite import producer as producer_mod
        from paperlite.browser import PassResult

        store, ips = mock.Mock(), mock.Mock()
        ips.s = PROXY
        lease = Lease("1.2.3.4:80", "1.2.3.4", "", time.time() + 300)
        ips.extract_checked.return_value = lease
        producer = Producer(Limits(pass_timeout_s=30, refill_before_s=30), store, ips, "www.sciencedirect.com")
        passed = PassResult(ok=True, seconds=12, user_agent="Mozilla/5.0 Chrome/145.0.0.0",
                            cookies=[{"name": "JSESSIONID", "value": "test", "domain": "www.sciencedirect.com",
                                      "path": "/"}])
        with mock.patch.object(producer_mod, "pass_challenge", new=mock.AsyncMock(return_value=passed)), \
                mock.patch.object(producer, "_curl_usable", return_value=True):
            asyncio.run(producer._produce_one(1))
        store.add.assert_called_once()
        self.assertEqual(store.add.call_args.args[0].expires_at, lease.expires_at)

    def test_rejected_replay_never_enters_redis(self):
        import asyncio
        from paperlite import producer as producer_mod
        from paperlite.browser import PassResult

        store = mock.Mock()
        ips = mock.Mock()
        ips.s = PROXY
        ips.extract_checked.return_value = Lease("1.2.3.4:80", "1.2.3.4", "", time.time() + 300)
        producer = Producer(Limits(pass_timeout_s=30, refill_before_s=30), store, ips, "dl.acm.org")
        passed = PassResult(ok=True, seconds=12, user_agent="Mozilla/5.0 Chrome/145.0.0.0",
                            cookies=[{"name": "cf_clearance", "value": "test", "domain": "dl.acm.org",
                                      "path": "/", "expires": time.time() + 240}])
        with mock.patch.object(producer_mod, "pass_challenge", new=mock.AsyncMock(return_value=passed)), \
                mock.patch.object(producer, "_curl_usable", return_value=False):
            asyncio.run(producer._produce_one(1))
        store.add.assert_not_called()
        store.note_fresh_reject.assert_called_once_with("dl.acm.org")
        self.assertEqual(producer.stats.failed, 1)


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

    def report_pending(self, host, consumer, n):
        self.pending_reports = getattr(self, "pending_reports", []) + [n]

    def clear_pending(self, host, consumer):
        self.pending_cleared = True

    def note_good_url(self, host, url):
        self.good = url

    def good_url(self, host):
        return getattr(self, "good", None)


@needs_pg
class DownloaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.db = make_source(self)
        self.cfg = Limits(request_gap_s=(0, 0))

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
        return pg_row(self.db, url)

    def test_reports_pending_and_clears_on_exit(self):
        store = FakeStore([ident(exit_ip="1.1.1.1")])
        urls = [f"https://dl.acm.org/doi/pdf/10.1145/{i}" for i in range(3)]
        self.run_with(store, {"1.1.1.1": lambda u: Resp(200, FAKE_PDF)}, urls)
        self.assertEqual(store.pending_reports[0], 1)        # workers=1：数到线程数为止
        self.assertTrue(store.pending_cleared)               # 退出时撤掉本进程的上报

    def test_flagged_only_pending_does_not_keep_consumer_alive(self):
        """剩下的 pending 全被巡检标记跳过：线程不登记在线、直接退出，不再拉着生产者过盾"""
        url = "https://dl.acm.org/doi/pdf/10.1145/9"
        self.db.import_urls([{"pdf_url": url}])
        execute(self.db, "UPDATE pl_tasks SET skip_reason = 'title_dup'")
        store = FakeStore([ident(exit_ip="1.1.1.1")])
        dl = Downloader(self.cfg, store, PROXY, self.db, self.out, workers=2)
        finished = threading.Event()
        threading.Thread(target=lambda: (dl.run(), finished.set()), daemon=True).start()
        self.assertTrue(finished.wait(10), "只剩被标记的任务时下载线程应退出")
        self.assertFalse(getattr(store, "alive_calls", 0))   # 从未登记在线
        self.assertEqual(store.pending_reports[0], 0)

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

    def test_non_cf_429_retries_with_same_identity(self):
        """普通站点（非 Cloudflare 封禁）的 429 限流：遵守 Retry-After 等待后沿用身份重试。"""
        store = FakeStore([ident(exit_ip="1.1.1.1")])
        url = "https://dl.acm.org/doi/10.1145/100348.100354"
        # 注意：body 不包含 "Access denied" 标题，所以不触发 is_cf_denied
        calls = iter([Resp(429, b"<title>Too Many Requests</title>", {"retry-after": "2"}),
                      Resp(200, b"<title>Distributed multicasting</title>")])
        with mock.patch("paperlite.consumer.time.sleep") as sleep:
            dl = self.run_with(store, {"1.1.1.1": lambda u: next(calls)}, [url], url_type="html")
        row = self.row(url)
        self.assertEqual((row["status"], row["retry_count"], dl.stats.failed), ("success", 0, 0))
        self.assertEqual((dl.stats.rejected, store.discarded), (0, []))
        sleep.assert_called_once_with(2.0)

    def test_cf_429_access_denied_drops_identity(self):
        """Cloudflare Access denied (HTTP 429 + server=cloudflare + 匹配标题)：
        is_cf_denied 先于 429 检查，直接丢弃身份、任务放回，不等待 Retry-After。"""
        store = FakeStore([ident(exit_ip="1.1.1.1"), ident(exit_ip="2.2.2.2")])
        url = "https://dl.acm.org/doi/10.1145/100348.100354"
        calls = {
            "1.1.1.1": lambda u: Resp(429, b"<title>Access denied | Cloudflare</title>",
                                      {"server": "cloudflare", "retry-after": "30"}),
            "2.2.2.2": lambda u: Resp(200, b"<title>Distributed multicasting</title>"),
        }
        with mock.patch("paperlite.consumer.time.sleep") as sleep:
            dl = self.run_with(store, calls, [url], url_type="html")
        row = self.row(url)
        # CF 封禁身份被丢弃：换身份后由第二个身份成功下载
        self.assertEqual((row["status"], dl.stats.success, dl.stats.rejected), ("success", 1, 1))
        self.assertEqual(store.discarded, ["1.1.1.1"])
        # CF 封禁识别走 is_cf_denied 分支，不调用 time.sleep 等待 Retry-After
        sleep.assert_not_called()

    def test_session_preserves_scoped_cookies_after_serialization(self):
        original = ident(cookies=[
            {"name": "cf_clearance", "value": "first", "domain": ".acm.org", "path": "/", "secure": True},
            {"name": "cf_clearance", "value": "second", "domain": "dl.acm.org", "path": "/doi", "secure": True},
        ])
        restored = Identity.from_redis(original.to_redis())
        session = Downloader._session(None, restored)
        try:
            cookies = list(session.cookies.jar)
            self.assertEqual([(c.domain, c.path, c.secure) for c in cookies],
                             [(".acm.org", "/", True), ("dl.acm.org", "/doi", True)])
        finally:
            session.close()

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
        # 站点单独盯上 bad 这条：带同一 cookie 访问首页和别的论文都正常
        calls = []

        def site(u):
            calls.append(u)
            if "bad" in u:
                return Resp(403, b"challenge-platform", {"cf-mitigated": "challenge"})
            return Resp(200, b"<title>home</title>") if "/doi/pdf/" not in u else Resp(200, FAKE_PDF)
        store = FakeStore([ident()])
        self.cfg = Limits(request_gap_s=(0, 0), max_url_challenges=2)
        bad, good = "https://dl.acm.org/doi/pdf/10.1145/bad", "https://dl.acm.org/doi/pdf/10.1145/good"
        dl = self.run_with(store, {"1.2.3.4": site}, [bad, good])
        # 第一次复验时还没有成功链接 → 用首页；good 下成功后，第二次复验用的就是它
        self.assertEqual(calls, [bad, "https://dl.acm.org/", good, bad, good])
        self.assertEqual(store.good, good)                   # 最近成功链接也同步给了生产者
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


@needs_pg
class BrowserPoolTest(unittest.TestCase):
    """浏览器下载模式：用假 BrowserSession / IpSource 驱动，不开真浏览器。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.db = make_source(self)
        self.cfg = Limits(page_interval_s=(0, 0), pass_timeout_s=5, min_left_s=1, max_url_challenges=2)

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
        return pg_row(self.db, url)

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
        rows = pg_rows(self.db)
        self.assertEqual(sum(r["status"] == "success" for r in rows), 3)
        self.assertEqual(self.db.q.skip_counts(), {"title_dup": 3})

    def test_pdf_task_getting_a_web_page_is_non_pdf(self):
        """浏览器只能拿到网页：要 PDF 的任务打开的是摘要页，记 non_pdf（按页面分类），不能记成功。"""
        from paperlite.browser import RenderResult
        url = "https://linkinghub.elsevier.com/retrieve/pii/0001691855900019"
        self.db.import_urls([{"url": url, "url_type": "pdf"}])
        pool = self.run_pool(lambda u: RenderResult(ok=True, seconds=1.0, url="https://www.sciencedirect.com/x",
                                                    status=200, title="Abstract page", html="<title>Abstract</title>"),
                             [])
        self.assertEqual((pool.stats.success, pool.stats.non_pdf, pool.stats.failed), (0, 1, 0))
        r = self.row(url)
        self.assertEqual((r["status"], r["error_kind"], r["url_type"], r["file_path"]),
                         ("non_pdf", "landing_page", "pdf", None))
        self.assertEqual(len(list((self.out / "non_pdf").glob("*.html"))), 1)       # 网页留在 non_pdf/ 备查

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
        """整站打不开：前 3 个新浏览器的首篇失败算 IP 的（换 IP、不计次数），之后认定是站点问题、正常计次判死，
        不会为一个打不开的站无限换 IP。"""
        from paperlite.browser import RenderResult
        url = "https://www.sciencedirect.com/science/article/abs/pii/stuck"
        resp = lambda u: RenderResult(ok=False, seconds=5.0, error="TimeoutError: 页面未稳定")
        pool = self.run_pool(resp, [url], n_leases=9)
        self.assertEqual((pool.stats.success, pool.stats.failed, pool.stats.probe_failed), (0, 1, 3))
        r = self.row(url)
        self.assertEqual((r["status"], r["error_kind"], r["challenge_count"]), ("failed", "challenge_failed", 2))

    def test_first_page_failure_blamed_on_ip_not_task(self):
        """不热身：新浏览器第一篇就打不开 → 换 IP、任务放回不计次数；下一个 IP 打开了就照常连续下。"""
        from paperlite.browser import RenderResult
        urls = [f"https://www.sciencedirect.com/science/article/abs/pii/{i}" for i in range(3)]
        bad_ip = {"left": 1}
        def resp(u):
            if bad_ip["left"]:
                bad_ip["left"] -= 1
                return RenderResult(ok=False, seconds=90.0, error="TimeoutError: 90s 内未通过挑战")
            return self._ok(u)
        pool = self.run_pool(resp, urls)
        self.assertEqual((pool.stats.success, pool.stats.failed), (3, 0))
        self.assertEqual((pool.stats.probe_failed, pool.stats.browsers), (1, 1))   # 换了一次 IP，第二个浏览器连下三篇
        self.assertTrue(all(r["challenge_count"] == 0 and r["retry_count"] == 0 for r in pg_rows(self.db)))

    def test_bad_hosts_first_do_not_stop_no_proxy_worker(self):
        """不走代理、批次开头是格式合法但打不开的站点（明显坏的在导入时已剔除）：worker 不会因此收工，
        后面的正常站点照常下载，打不开的最终判死。"""
        from paperlite.browser import RenderResult
        self.cfg = Limits(page_interval_s=(0, 0), pass_timeout_s=5, min_left_s=1, max_url_challenges=1)
        bad = ["http://10040.indexcopernicus.com/abstracted.php?ICID=1", "http://219.238.6.200/a"]
        good = [f"https://link.springer.com/article/{i}" for i in range(3)]
        def resp(u):
            if u in bad:
                return RenderResult(ok=False, seconds=1, error="Error: Page.goto: net::ERR_CONNECTION_TIMED_OUT")
            return self._ok(u)
        from paperlite import browser_pool as bp
        self.db.import_urls({"url": u, "url_type": "html"} for u in bad + good)

        class FakeSession:
            def __init__(s, *a, **k):
                pass
            async def start(s):
                pass
            async def render(s, url, timeout_s, settle_s=2.0):
                return resp(url)
            async def close(s):
                pass
        with mock.patch.object(bp, "BrowserSession", FakeSession):
            pool = BrowserPool(self.cfg, None, self.db, self.out, 1, no_proxy=True)
            pool.run()
        self.assertEqual((pool.stats.success, pool.stats.failed), (3, 2))
        # 两个坏主机各让一个新浏览器首篇失败（不计次数、重开浏览器）；浏览器在正常站点上打开后，
        # 坏链接下一轮被领到时已不是首篇，正常计次判死
        self.assertEqual(pool.stats.probe_failed, 2)

    def test_proxy_net_error_switches_ip_without_counting(self):
        """IP 中途失效（ERR_TUNNEL_CONNECTION_FAILED）：不计挑战次数、任务放回、换 IP。"""
        from paperlite.browser import RenderResult
        url = "https://www.sciencedirect.com/science/article/abs/pii/net"
        dead = {"n": 2}                                          # 前两个浏览器的首篇都是隧道错误
        def resp(u):
            if dead["n"] > 0:
                dead["n"] -= 1
                return RenderResult(ok=False, seconds=0.1,
                                    error=f"Error: Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at {u}")
            return self._ok(u)
        pool = self.run_pool(resp, [url])                        # max_url_challenges=2：若计数早就判死了
        self.assertEqual((pool.stats.success, pool.stats.failed), (1, 0))
        self.assertEqual((pool.stats.probe_failed, pool.stats.browsers), (2, 1))   # 换过两次 IP
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

    def test_pending_reports_sum_expire_and_clear(self):
        """可领取任务数：多个下载进程求和；超时未更新的上报作废；进程退出撤掉；无上报为 None"""
        from paperlite import store as store_mod
        self.assertIsNone(self.store.pending(self.host))
        self.store.report_pending(self.host, "m1-100", 3)
        self.store.report_pending(self.host, "m2-200", 5)
        self.assertEqual(self.store.pending(self.host), 8)
        self.assertEqual(self.store.stats(self.host).pending, 8)
        self.store.r.hset(self.store._k(self.host, "pending"), "dead-1",
                          f"50:{time.time() - store_mod.PENDING_STALE_S - 5:.0f}")     # 崩溃进程的旧上报
        self.assertEqual(self.store.pending(self.host), 8)
        self.assertFalse(self.store.r.hexists(self.store._k(self.host, "pending"), "dead-1"))   # 顺手清掉
        self.store.clear_pending(self.host, "m1-100")
        self.store.report_pending(self.host, "m2-200", 0)
        self.assertEqual(self.store.pending(self.host), 0)   # 有上报但没活：0（不是 None），生产者停产

    def test_claim_is_exclusive_and_prefers_soonest_expiry(self):
        late, soon = ident(exit_ip="9.9.9.9", expires_at=time.time() + 500), ident(expires_at=time.time() + 200)
        self.store.add(late)
        self.store.add(soon)
        a = self.store.claim(self.host, "w1", min_left_s=20, lease_ttl_s=60)
        b = self.store.claim(self.host, "w2", min_left_s=20, lease_ttl_s=60)
        self.assertEqual((a.exit_ip, b.exit_ip), ("1.2.3.4", "9.9.9.9"))
        self.assertIsNone(self.store.claim(self.host, "w3", 20, 60))
        self.assertEqual(jar_to_records(a.cookies), jar_to_records(soon.cookies))

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
    from paperlite.consumer import is_cf_denied, is_challenge
    normal = b'<html><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>' \
             b'<script src="/cdn-cgi/challenge-platform/scripts/precursor"></script></html>'
    assert not is_challenge({}, normal)
    page = b'<html><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=1"></script></html>'
    assert is_challenge({}, page)
    assert is_challenge({"cf-mitigated": "challenge"}, b"")
    assert is_cf_denied(429, {"server": "cloudflare"}, b"<title>Access denied | Cloudflare</title>")
    assert is_cf_denied(403, {"cf-ray": "test"}, b"<title>Attention Required! | Cloudflare</title>")
    assert not is_cf_denied(429, {}, b"<title>Access denied</title>")
    assert not is_cf_denied(200, {"server": "cloudflare"}, b"<title>Access denied</title>")


# ──────────────────────────────────────────────────────────────────────────
# 命令行契约：断言 docs/reference/cli.md 与 configuration.md 描述的子命令、参数、默认值、
# choices 与真实的 argparse 定义一致。只测参数解析（纯函数），不碰 Redis/网络。
# 改了 CLI 的命令集或参数默认值而没更新文档时，这里会红。
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

    # 文档 命令表列出的子命令（help 是帮助本身）
    def test_command_set_matches_readme(self):
        from paperlite.cli import _COMMANDS
        self.assertEqual(
            set(_COMMANDS),
            {"init", "import", "run", "produce", "download", "status",
             "inspect", "retry", "export", "turnstile", "help"},
        )
        # 每个命令都建了子解析器（help 自身也在内）
        self.assertEqual(set(self.parsers), set(_COMMANDS))

    # 文档 快速开始/脚注：--version、--config、--log-level 默认 INFO
    def test_global_flags(self):
        self.assertEqual(self.parse("status").log_level, "INFO")
        self.assertIsNone(self.parse("status").config)
        self.fails("--version")          # version action 直接退出
        self.fails("不是命令")            # 未知命令报错退出

    # 任务队列只在 [pg]：--db / --pg 已移除；没有 --out/--host 了
    def test_no_db_or_pg_flags(self):
        for name in ("import", "run", "produce", "download", "status", "inspect", "retry", "export"):
            argv = [name] + (["--urls", "x.txt"] if name == "import" else
                             ["--status", "failed"] if name == "retry" else [])
            self.parse(*argv)
            self.fails(*argv, "--db", "d.db")
            self.fails(*argv, "--pg")
        self.assertEqual(self.parse("run", "--storage", "/mnt/x").storage, "/mnt/x")   # run/download 另有 --storage
        self.fails("status", "--out", "o")        # --out 已移除
        self.fails("import", "--host", "h")       # --host 已移除

    # 文档 import 行：--urls 必填；--type 为 pdf|html，不写时取配置 [run] url_type（没有则 pdf）
    def test_import(self):
        from paperlite.cli import _url_type
        import argparse
        self.fails("import")                                   # 缺 --urls
        ns = self.parse("import", "--urls", "acm.txt")
        self.assertIsNone(ns.type)
        self.assertEqual(self.parse("import", "--urls", "x", "--type", "html").type, "html")
        self.fails("import", "--urls", "x", "--type", "xml")   # 非法 choice
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.toml"
            ns = lambda t=None: argparse.Namespace(config=str(cfg), type=t)
            self.assertEqual(_url_type(ns()), "pdf")           # 没有配置文件
            cfg.write_text('[run]\nurl_type = "html"\n')
            self.assertEqual(_url_type(ns()), "html")          # 跟配置走：以前这里固定是 pdf，整批类型记错
            self.assertEqual(_url_type(ns("pdf")), "pdf")      # 命令行优先

    # 文档 run 行：--workers 默认 20；--curl/--browser 互斥；--no-proxy；--type
    def test_run(self):
        ns = self.parse("run")
        self.assertEqual(ns.workers, 20)
        self.assertFalse(ns.browser or ns.curl or ns.no_proxy)
        self.assertIsNone(ns.type)                             # 不写时取配置 [run] url_type
        self.assertIsNone(ns.config_file)                      # 裸 run：无配置文件，用 flag
        self.assertTrue(self.parse("run", "--browser", "--no-proxy").no_proxy)
        self.fails("run", "--browser", "--curl")               # 互斥

    # 文档「配置文件启动」：paperlite run config.toml 按 [run] 启动
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

    # 文档：produce 常驻，按下载线程与可领取任务生产；--keep 已删除
    def test_produce(self):
        self.parse("produce")
        self.fails("produce", "--keep", "20")

    # 文档：download 需 --workers（默认 20），可选 --urls
    def test_download(self):
        self.assertEqual(self.parse("download").workers, 20)
        self.assertEqual(self.parse("download", "--workers", "8").workers, 8)

    # 文档 retry：--status 必填，只接受 failed/non_pdf；--kind 配合；--stopped-volumes 随卷期停止移除
    def test_retry(self):
        from paperlite.cli import ERROR_KINDS
        self.fails("retry")                                    # 缺 --status
        self.assertEqual(self.parse("retry", "--status", "failed").status, "failed")
        self.assertEqual(self.parse("retry", "--status", "non_pdf").status, "non_pdf")
        self.fails("retry", "--stopped-volumes")
        self.fails("retry", "--status", "pending")             # 非法 choice
        self.assertIn("network", ERROR_KINDS)
        self.assertEqual(self.parse("retry", "--status", "failed", "--kind", "network").kind, "network")

    # 文档 export：--status 为 pending / success / failed / non_pdf 之一；--file 默认标准输出（-）
    def test_export(self):
        from paperlite.tasks import Status
        self.assertEqual(self.parse("export").file, "-")
        for s in (Status.PENDING, Status.SUCCESS, Status.FAILED, Status.NON_PDF):
            self.assertEqual(self.parse("export", "--status", s).status, s)
        self.fails("export", "--status", "running")            # running 只是进程内计数，不落库
        self.fails("export", "--status", "bogus")

    # 文档 inspect 行：--trigger/--min-precision/--min-support/--list/--reset
    def test_inspect(self):
        ns = self.parse("inspect", "--trigger", "10", "--min-precision", "0.9",
                        "--min-support", "5", "--list", "--reset")
        self.assertEqual((ns.trigger, ns.min_precision, ns.min_support), (10, 0.9, 5))
        self.assertTrue(ns.list and ns.reset)

    # 文档 Turnstile 节：--url/--sitekey、--serve 起服务、--json、默认值
    def test_turnstile(self):
        ns = self.parse("turnstile", "--url", "https://x/", "--sitekey", "0x4A")
        self.assertEqual((ns.timeout, ns.concurrency, ns.bind), (60, 4, "127.0.0.1"))
        self.assertFalse(ns.json)
        self.assertEqual(self.parse("turnstile", "--serve", "8191").serve, 8191)
        self.assertTrue(self.parse("turnstile", "--url", "x", "--sitekey", "k", "--json").json)

    # 文档：init --force
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
