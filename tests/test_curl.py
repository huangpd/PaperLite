"""curl 模式（paperlite.curl）：身份规则、生产者、下载线程、身份池。"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from paperlite.config import Limits
from paperlite.curl.downloader import Downloader
from paperlite.curl.http import rate_limit_wait
from paperlite.curl.cookies import jar_to_records
from paperlite.curl.producer import Producer
from paperlite.proxy import Lease
from paperlite.curl.identity import Identity
from paperlite.core.tasks import Status, derive_doi, pdf_path, url_key
from tests.pgutil import execute, make_source, make_store, needs_pg, row as pg_row
from tests.fakes import FAKE_PDF, PROXY, ident, Resp, FakeStore







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


class ProbeUrlTest(unittest.TestCase):
    """不热身：curl 模式验收新身份、复验身份都用真实链接，不用配置的落地页。"""

    def producer(self, store, sample=None):
        from paperlite.curl.producer import Producer
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
        with mock.patch("paperlite.curl.producer.session_for_identity", return_value=session):
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
        with self.assertLogs("paperlite.curl.producer", "WARNING"):
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
        from paperlite.curl import producer as producer_mod

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
        from paperlite.curl import producer as producer_mod

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
        from paperlite.curl import producer as producer_mod

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
        from paperlite.curl import producer as producer_mod
        from paperlite.browser.session import PassResult

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

    def test_rejected_replay_never_enters_pool(self):
        import asyncio
        from paperlite.curl import producer as producer_mod
        from paperlite.browser.session import PassResult

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
        from paperlite.curl import producer as producer_mod
        from paperlite.curl.identity import PoolStats

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
        with mock.patch("paperlite.curl.downloader.time.sleep") as sleep:
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
        with mock.patch("paperlite.curl.downloader.time.sleep") as sleep:
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
        restored = Identity.from_dict(original.to_dict())
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
        with mock.patch("paperlite.curl.downloader._RESET_RETRY_WAIT_S", 0):
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
        with mock.patch("paperlite.curl.downloader._IDLE_WAIT_S", 0.05):
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
        with mock.patch("paperlite.curl.downloader._RESET_RETRY_WAIT_S", 0):
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
class IdentityStoreTest(unittest.TestCase):
    """curl 模式身份池（PostgreSQL）。"""

    def setUp(self):
        self.store = make_store(self)
        self.host = "dl.acm.org"

    def test_pending_reports_sum_expire_and_clear(self):
        """可领取任务数：多个下载进程求和；超时未更新的上报作废；进程退出撤掉；无上报为 None"""
        from paperlite.curl import identity as store_mod
        self.assertIsNone(self.store.pending(self.host))
        self.store.report_pending(self.host, "m1-100", 3)
        self.store.report_pending(self.host, "m2-200", 5)
        self.assertEqual(self.store.pending(self.host), 8)
        self.assertEqual(self.store.stats(self.host).pending, 8)
        self.store._x("INSERT INTO pl_id_pending VALUES (%s, 'dead-1', 50, %s)",
                      (self.host, time.time() - store_mod.PENDING_STALE_S - 5))          # 崩溃进程的旧上报
        self.assertEqual(self.store.pending(self.host), 8)
        self.assertIsNone(self.store._one("SELECT 1 FROM pl_id_pending WHERE consumer = 'dead-1'"))   # 顺手清掉
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
        self.store._x("UPDATE pl_identities SET lease_until = 0 WHERE id = %s", (a.id,))   # 模拟占用者崩溃：租约过期
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

    def test_concurrent_claims_never_share_an_identity(self):
        """多个进程（各自一条连接）同时领：SKIP LOCKED 保证一个身份只被一个线程拿到。"""
        stores = [self.store] + [make_store(self, schema=self.store._one("SELECT current_schema()")[0])
                                 for _ in range(3)]
        for i in range(20):
            self.store.add(ident(exit_ip=f"10.0.0.{i}", expires_at=time.time() + 300 + i))
        got, lock = [], threading.Lock()

        def worker(st, n):
            while (x := st.claim(self.host, f"w{n}", 20, 60)) is not None:
                with lock:
                    got.append(x.id)
        ts = [threading.Thread(target=worker, args=(st, n)) for n, st in enumerate(stores * 2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual((len(got), len(set(got))), (20, 20))

    def test_good_url_shared(self):
        self.assertIsNone(self.store.good_url(self.host))
        self.store.note_good_url(self.host, "https://dl.acm.org/doi/pdf/10.1145/1")
        self.assertEqual(self.store.good_url(self.host), "https://dl.acm.org/doi/pdf/10.1145/1")

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


def test_meta_refresh_target():
    from paperlite.curl.downloader import meta_refresh_target
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
    from paperlite.core.page import is_cf_denied, is_challenge
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
# choices 与真实的 argparse 定义一致。只测参数解析（纯函数），不碰数据库/网络。
# 改了 CLI 的命令集或参数默认值而没更新文档时，这里会红。
# ──────────────────────────────────────────────────────────────────────────
