import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from paperlite.classify import classify_html
from paperlite.config import PoolConfig, proxy_settings
from paperlite.consumer import Downloader, impersonate_for
from paperlite.producer import Producer
from paperlite.proxy import Lease, ProxySettings, parse_deadline
from paperlite.store import Identity
from paperlite.tasks import Status, TaskDB, derive_paper_uid, derive_volume, pdf_path, url_key

PROXY = ProxySettings(api_url="https://api.example/get?key=k", username="u", password="p@ss")


def ident(**kw) -> Identity:
    d = dict(host="dl.acm.org", exit_ip="1.2.3.4", proxy_server="1.2.3.4:8000", user_agent="Mozilla/5.0 Chrome/146.0",
             cookies=[{"name": "cf_clearance", "value": "v", "domain": ".acm.org", "path": "/"}],
             expires_at=time.time() + 600)
    d.update(kw)
    return Identity(**d)


class ConfigTest(unittest.TestCase):
    def test_from_dict(self):
        cfg = PoolConfig.from_dict({"request_gap_s": [1, 2]}, landing={"DL.ACM.ORG": "https://x/"})
        self.assertEqual(cfg.request_gap_s, (1.0, 2.0))
        self.assertEqual(cfg.landing_for("dl.acm.org"), "https://x/")
        self.assertEqual(cfg.landing_for("other.org"), "https://other.org/")

    def test_rejects_unknown_and_bad_gap(self):
        with self.assertRaises(ValueError):
            PoolConfig.from_dict({"max_browser": 3})
        with self.assertRaises(ValueError):
            PoolConfig.from_dict({"request_gap_s": [5, 3]})

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
            f.write('[proxy]\napi_url = "https://a/"\n[pool]\nmax_browsers = 3\n[landing]\n"h.org" = "https://h.org/x"\n')
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
        self.assertEqual(derive_paper_uid(url), "10.1145/1005062.1005064")
        self.assertTrue(derive_paper_uid("https://x.org/a.pdf").startswith("url-"))
        p = pdf_path(Path("out"), "10.1145/3779295")
        self.assertEqual(p.name, "10.1145_3779295.pdf")
        self.assertEqual(len(p.parent.name), 2)              # out/<h0:2>/<h2:4>/
        self.assertEqual(url_key("HTTPS://DL.ACM.org/a#x"), url_key("https://dl.acm.org/a"))
        self.assertNotEqual(url_key("https://dl.acm.org/A"), url_key("https://dl.acm.org/a"))


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
        with self.assertRaises(ValueError):
            self.db.import_urls([{"pdf_url": "https://other.org/x.pdf"}])

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
        self.assertEqual(self.db.claim("w").pdf_url, self.urls(2)[0]["pdf_url"])   # 被挑战的排到后面
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
        vols = [t.paper_uid.split("/")[1].split(".")[0] for t in got if t]
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


class ProducerTargetTest(unittest.TestCase):
    def test_target_follows_workers(self):
        p = Producer(PoolConfig(max_identities=20, min_identities=2), mock.Mock(), mock.Mock(), "dl.acm.org")
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
        cfg = PoolConfig(max_browsers=5, hedge_after_s=0.05, pass_timeout_s=10)
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
        p = Producer(PoolConfig(max_fresh_rejects=5, breaker_pause_s=600), store, mock.Mock(), "dl.acm.org")
        self.assertTrue(p._breaker_open())
        store.trip.assert_called_once_with("dl.acm.org", 600, 5)
        store.paused_for.return_value = 590
        self.assertTrue(p._breaker_open())                 # 暂停中
        store.paused_for.return_value = 0
        store.fresh_rejects.return_value = 4
        self.assertFalse(p._breaker_open())                # 暂停结束、计数在阈值下：放一个试探


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self.content, self.headers = status, body, headers or {}


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
        self.cfg = PoolConfig(request_gap_s=(0, 0))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_with(self, store, responses_by_ip, urls):
        self.db.import_urls({"pdf_url": u} for u in urls)

        def fake_session(ident):
            s = mock.Mock()
            s.get.side_effect = lambda url, **kw: responses_by_ip[ident.exit_ip](url)
            return s

        dl = Downloader(self.cfg, store, PROXY, self.db, self.out, workers=1)
        with mock.patch.object(Downloader, "_session", side_effect=fake_session):
            dl.run()
        return dl

    def row(self, url):
        return next(r for r in self.db.rows() if r["pdf_url"] == url)

    def test_rejected_cookie_switches_identity_without_counting_retry(self):
        store = FakeStore([ident(exit_ip="1.1.1.1"), ident(exit_ip="2.2.2.2")])
        urls = ["https://dl.acm.org/doi/pdf/10.1145/1", "https://dl.acm.org/doi/pdf/10.1145/2"]
        dl = self.run_with(store, {
            "1.1.1.1": lambda u: Resp(403, b"<html>challenge-platform</html>", {"cf-mitigated": "challenge"}),
            "2.2.2.2": lambda u: Resp(200, b"%PDF-1.4 ..."),
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
        self.assertEqual((r["status"], r["error_kind"], r["error_message"]), ("non_pdf", "not_found", "ACM Error: 404"))
        self.assertTrue((self.out / "non_pdf" / "10.1145_404.html").exists())

    def test_blocked_ip_drops_identity(self):
        store = FakeStore([ident(exit_ip="1.1.1.1"), ident(exit_ip="2.2.2.2")])
        url = "https://dl.acm.org/doi/pdf/10.1145/1"
        dl = self.run_with(store, {"1.1.1.1": lambda u: Resp(403, b"<title>ACM Error: IP blocked</title>"),
                                   "2.2.2.2": lambda u: Resp(200, b"%PDF-1.4")}, [url])
        self.assertEqual((dl.stats.success, dl.stats.rejected, store.discarded), (1, 1, ["1.1.1.1"]))

    def test_fresh_rejects_counted_but_not_network_errors(self):
        store = FakeStore([ident(exit_ip="1.1.1.1"), ident(exit_ip="2.2.2.2"), ident(exit_ip="3.3.3.3")])
        challenge = lambda u: Resp(403, b"challenge-platform", {"cf-mitigated": "challenge"})

        def refused(u):
            raise ConnectionError("curl: (7) Failed to connect")
        self.cfg = PoolConfig(request_gap_s=(0, 0), max_tries=1)
        self.run_with(store, {"1.1.1.1": challenge, "2.2.2.2": refused, "3.3.3.3": challenge},
                      ["https://dl.acm.org/doi/pdf/10.1145/1"])
        self.assertEqual(store.fresh_rejects, 1)            # 1.1.1.1 被拒计入；2.2.2.2 连不上不计，任务判失败后结束

    def test_url_challenge_keeps_identity(self):
        # 站点单独盯上 bad 这条：带同一 cookie 访问落地页和别的论文都正常
        def site(u):
            if "bad" in u:
                return Resp(403, b"challenge-platform", {"cf-mitigated": "challenge"})
            return Resp(200, b"<title>landing</title>") if "/doi/pdf/" not in u else Resp(200, b"%PDF-1.4")
        store = FakeStore([ident()])
        self.cfg = PoolConfig(request_gap_s=(0, 0), max_url_challenges=2,
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
            return Resp(200, b"%PDF-1.4")
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
        self.cfg = PoolConfig(request_gap_s=(0, 0), volume_stop_after=3)
        urls = [f"https://dl.acm.org/doi/pdf/10.1145/1012326.{1012327 + i}" for i in range(11)]
        dl = self.run_with(FakeStore([ident()]), {"1.2.3.4": site}, urls)
        self.assertEqual(len(calls), 3)                     # 11 篇只请求了 3 篇
        self.assertEqual((dl.stats.non_pdf, dl.stats.volumes_stopped, dl.stats.inferred), (3, 1, 8))

    def test_network_error_retries_then_fails(self):
        def boom(u):
            raise TimeoutError("curl: (28) Operation timed out")
        self.cfg = PoolConfig(request_gap_s=(0, 0), max_tries=2)
        url = "https://dl.acm.org/doi/pdf/10.1145/x"
        dl = self.run_with(FakeStore([ident()]), {"1.2.3.4": boom}, [url])
        self.assertEqual((dl.stats.success, dl.stats.failed), (0, 1))
        r = self.row(url)
        self.assertEqual((r["status"], r["error_kind"], r["retry_count"]), ("failed", "network", 1))


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
