"""浏览器模式（paperlite.browser）：worker 池、巡检接线、Turnstile。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from paperlite.browser.pool import BrowserPool
from paperlite.config import Limits
from paperlite.proxy import Lease
from paperlite.core.tasks import Status, pdf_path
from tests.pgutil import make_source, needs_pg, row as pg_row, rows as pg_rows
from tests.fakes import FAKE_PDF, PROXY




@needs_pg
class BrowserObserveWiringTest(unittest.TestCase):
    """浏览器模式：不止成功页，404 等已加载的失效页也要喂给巡检器（不再只在成功分支调）。"""

    def test_404_branch_feeds_inspector(self):
        import asyncio
        from types import SimpleNamespace
        from paperlite.browser.pool import BrowserPool
        from paperlite.browser.session import RenderResult
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
        from paperlite.browser.pool import BrowserPool
        from paperlite.browser.session import RenderResult
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
class BrowserPoolTest(unittest.TestCase):
    """浏览器下载模式：用假 BrowserSession / IpSource 驱动，不开真浏览器。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.db = make_source(self)
        self.cfg = Limits(page_interval_s=(0, 0), pass_timeout_s=5, min_left_s=1, max_url_challenges=2)

    def run_pool(self, responses, urls, workers=1, n_leases=3):
        from paperlite.browser import pool as bp
        from paperlite.proxy import ProxyUnavailable

        self.db.import_urls({"pdf_url": u, "url_type": "html"} for u in urls)

        class FakeSession:
            def __init__(s, proxy, headed=False, geoip=False, webrtc_spoof=True):
                s.proxy = proxy
            async def start(s):
                pass
            async def render(s, url, timeout_s, settle_s=2.0, **kw):
                try:
                    return responses(url, **kw)
                except TypeError:                       # 只收 url 的 responses
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
        from paperlite.browser.session import RenderResult
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
        from paperlite.browser.session import RenderResult
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
        from paperlite.browser.session import RenderResult
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

    def test_pdf_task_captured_as_pdf(self):
        """浏览器拦截到 PDF 响应（流式写盘、已校验）：记 success，file_path 是 .pdf。"""
        from paperlite.browser.session import RenderResult
        from paperlite.browser.pdf_capture import CapturedPdf
        url = "https://dl.acm.org/doi/pdf/10.1145/100348.100354"
        self.db.import_urls([{"url": url, "url_type": "pdf"}])
        seen = {}

        def resp(u, pdf_dest=None, **kw):
            seen["dest"] = pdf_dest
            pdf_dest.parent.mkdir(parents=True, exist_ok=True)
            pdf_dest.write_bytes(FAKE_PDF)
            return RenderResult(ok=True, seconds=20.0, url=u, status=200, clicks=2, waf="cloudflare",
                                pdf=CapturedPdf(pdf_dest, len(FAKE_PDF), 200, u))
        pool = self.run_pool(resp, [])
        self.assertEqual((pool.stats.success, pool.stats.non_pdf, pool.stats.failed), (1, 0, 0))
        r = self.row(url)
        self.assertEqual((r["status"], r["file_size"], r["error_kind"]), ("success", len(FAKE_PDF), None))
        self.assertEqual(Path(r["file_path"]), pdf_path(self.out, r["doi"]))
        self.assertEqual(seen["dest"], pdf_path(self.out, r["doi"]))          # 要 PDF 的任务才开拦截

    def test_pdf_task_bad_pdf_retried_then_failed(self):
        """截到了 PDF 响应但校验没过（不完整等）：按提取错误重试，max_tries 用完判 failed / extract_failed。"""
        from paperlite.browser.session import RenderResult
        self.cfg = Limits(page_interval_s=(0, 0), pass_timeout_s=5, min_left_s=1, max_tries=2)
        url = "https://dl.acm.org/doi/pdf/10.1145/short"
        self.db.import_urls([{"url": url, "url_type": "pdf"}])
        self.run_pool(lambda u, **kw: RenderResult(ok=True, seconds=1.0, url=u, status=200, html="",
                                                           pdf_error="PDF 不完整: 实际 100，Content-Length 500 字节"), [])
        r = self.row(url)
        self.assertEqual((r["status"], r["error_kind"], r["retry_count"]), ("failed", "extract_failed", 1))
        self.assertIn("不完整", r["error_message"])

    def test_404_fails_without_retry(self):
        from paperlite.browser.session import RenderResult
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
        from paperlite.browser.session import RenderResult
        url = "https://www.sciencedirect.com/science/article/abs/pii/stuck"
        resp = lambda u: RenderResult(ok=False, seconds=5.0, error="TimeoutError: 页面未稳定")
        pool = self.run_pool(resp, [url], n_leases=9)
        self.assertEqual((pool.stats.success, pool.stats.failed, pool.stats.probe_failed), (0, 1, 3))
        r = self.row(url)
        self.assertEqual((r["status"], r["error_kind"], r["challenge_count"]), ("failed", "challenge_failed", 2))

    def test_first_page_failure_blamed_on_ip_not_task(self):
        """不热身：新浏览器第一篇就打不开 → 换 IP、任务放回不计次数；下一个 IP 打开了就照常连续下。"""
        from paperlite.browser.session import RenderResult
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
        from paperlite.browser.session import RenderResult
        self.cfg = Limits(page_interval_s=(0, 0), pass_timeout_s=5, min_left_s=1, max_url_challenges=1)
        bad = ["http://10040.indexcopernicus.com/abstracted.php?ICID=1", "http://219.238.6.200/a"]
        good = [f"https://link.springer.com/article/{i}" for i in range(3)]
        def resp(u):
            if u in bad:
                return RenderResult(ok=False, seconds=1, error="Error: Page.goto: net::ERR_CONNECTION_TIMED_OUT")
            return self._ok(u)
        from paperlite.browser import pool as bp
        self.db.import_urls({"url": u, "url_type": "html"} for u in bad + good)

        class FakeSession:
            def __init__(s, *a, **k):
                pass
            async def start(s):
                pass
            async def render(s, url, timeout_s, settle_s=2.0, **kw):
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
        from paperlite.browser.session import RenderResult
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
        from paperlite.browser.pool import is_proxy_net_error
        for e in ("Error: Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x", "net::ERR_PROXY_CONNECTION_FAILED",
                  "net::ERR_CONNECTION_CLOSED", "net::ERR_CONNECTION_RESET", "net::ERR_EMPTY_RESPONSE"):
            self.assertTrue(is_proxy_net_error(e), e)
        for e in ("TimeoutError: 90s 内页面未稳定", "net::ERR_NAME_NOT_RESOLVED", "", None):
            self.assertFalse(is_proxy_net_error(e), e)


def test_turnstile_parse_proxy():
    from paperlite.browser.turnstile import parse_proxy
    assert parse_proxy("") is None
    assert parse_proxy("http://1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}
    assert parse_proxy("socks5://u:p%40@h:1080") == {"server": "socks5://h:1080", "username": "u", "password": "p%40"}
    import pytest
    with pytest.raises(ValueError):
        parse_proxy("1.2.3.4")


def test_turnstile_checkbox_point_inside_widget():
    from paperlite.browser.turnstile import checkbox_point
    for box in ({"x": 100, "y": 200, "width": 300, "height": 65}, {"x": 0, "y": 0, "width": 150, "height": 140}):
        for _ in range(50):
            x, y = checkbox_point(box)
            assert box["x"] + 10 < x < box["x"] + 50
            assert box["y"] + 10 < y < box["y"] + box["height"] - 10


def test_turnstile_harness_escapes_params():
    import json
    from paperlite.browser.turnstile import harness_html
    html = harness_html("0x4AAA", action="login", cdata='a"</script><b>')
    assert '"sitekey": "0x4AAA"' in html and '"action": "login"' in html
    assert "</script><b>" not in html
    assert json.loads(json.dumps('a"</script><b>').replace("<", "\\u003c")) == 'a"</script><b>'
    assert json.dumps('a"</script><b>').replace("<", "\\u003c") in html


# ── 网页任务：跳转与挑战判断 ──
