import json
import threading
import time
import unittest
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import urlsplit

from paperlite.config import Limits
from paperlite.curl.downloader import session_for_identity
from paperlite.curl.cookies import jar_from_records, jar_to_records
from paperlite.curl.producer import Producer
from paperlite.proxy import ProxySettings
from paperlite.curl.identity import Identity
from tests.pgutil import identity_row, make_store, needs_pg


def identity(records, **changes):
    values = dict(host="dl.example.test", exit_ip="127.0.0.1", proxy_server="127.0.0.1:8080",
                  user_agent="Mozilla/5.0 Chrome/146.0.0.0", cookies=jar_from_records(records),
                  expires_at=time.time() + 300)
    values.update(changes)
    return Identity(**values)


class CookieJarTest(unittest.TestCase):
    def test_record_roundtrip_preserves_scopes_and_attributes(self):
        expires = int(time.time()) + 120
        original = identity([
            {"name": "cf_clearance", "value": "parent", "domain": ".example.test", "path": "/",
             "expires": expires, "secure": True, "httpOnly": True, "sameSite": "None"},
            {"name": "cf_clearance", "value": "host", "domain": "dl.example.test", "path": "/",
             "expires": -1, "secure": True},
            {"name": "cf_clearance", "value": "article", "domain": "dl.example.test", "path": "/doi",
             "expires": expires, "secure": True},
            {"name": "__Host-session", "value": "scoped", "domain": "dl.example.test", "path": "/",
             "expires": -1, "secure": True},
        ])
        restored = Identity.from_dict(original.to_dict())
        self.assertIsInstance(restored.cookies, CookieJar)
        with session_for_identity(restored) as session:
            self.assertIs(session.cookies.jar, restored.cookies)
            cookies = {(cookie.domain, cookie.path, cookie.name): cookie for cookie in session.cookies.jar}
        self.assertEqual(len(cookies), 4)
        parent = cookies[(".example.test", "/", "cf_clearance")]
        self.assertEqual((parent.value, parent.expires, parent.secure, parent.domain_specified),
                         ("parent", expires, True, True))
        self.assertTrue(parent.has_nonstandard_attr("HttpOnly"))
        self.assertEqual(parent.get_nonstandard_attr("SameSite"), "None")
        host = cookies[("dl.example.test", "/", "cf_clearance")]
        self.assertEqual((host.value, host.expires, host.discard, host.domain_specified),
                         ("host", None, True, False))
        self.assertEqual(cookies[("dl.example.test", "/doi", "cf_clearance")].value, "article")
        self.assertFalse(cookies[("dl.example.test", "/", "__Host-session")].domain_specified)

    def test_reads_legacy_browser_cookie_list(self):
        raw = identity([]).to_dict()
        del raw["cookies_format"]
        raw["cookies"] = json.dumps([
            {"name": "cf_clearance", "value": "parent", "domain": ".example.test", "path": "/"},
            {"name": "cf_clearance", "value": "host", "domain": "dl.example.test", "path": "/doi",
             "expires": -1, "httpOnly": True, "sameSite": "Lax"},
        ])
        restored = Identity.from_dict(raw)
        cookies = list(restored.cookies)
        self.assertEqual([cookie.value for cookie in cookies], ["parent", "host"])
        self.assertEqual([cookie.domain_specified for cookie in cookies], [True, False])
        self.assertTrue(cookies[1].has_nonstandard_attr("HttpOnly"))
        self.assertEqual(cookies[1].get_nonstandard_attr("SameSite"), "Lax")
        self.assertEqual(restored.to_dict()["cookies_format"], "cookiejar-v1")


class CookieEchoProxy(BaseHTTPRequestHandler):
    def do_GET(self):
        target = urlsplit(self.path)
        body = json.dumps({"cookie": self.headers.get("Cookie", ""),
                           "user_agent": self.headers.get("User-Agent", "")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if target.path == "/renew":
            self.send_header("Set-Cookie", "session=renewed; Path=/; Max-Age=600; HttpOnly")
            self.send_header("Set-Cookie", "obsolete=; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@needs_pg
class StoreCookieReplayTest(unittest.TestCase):
    """身份池（PostgreSQL）里的 cookie 经真实 HTTP 往返：作用域、续存、删除、寿命。"""
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CookieEchoProxy)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        self.store = make_store(self)
        self.proxy_server = f"127.0.0.1:{self.server.server_port}"
        self.proxy = ProxySettings(api_url="http://unused.example.test")

    def saved(self, ident_id):
        row = identity_row(self.store, ident_id)
        return Identity.from_dict(row[0]) if row else None

    def test_real_replay_scopes_renewal_deletion_and_lifetime(self):
        expires = int(time.time()) + 120
        original = identity([
            {"name": "cf_clearance", "value": "parent", "domain": ".example.test", "path": "/"},
            {"name": "cf_clearance", "value": "host", "domain": "dl.example.test", "path": "/"},
            {"name": "cf_clearance", "value": "article", "domain": "dl.example.test", "path": "/doi"},
            {"name": "secure_only", "value": "hidden", "domain": "dl.example.test", "secure": True},
            {"name": "expired", "value": "hidden", "domain": "dl.example.test", "expires": time.time() - 10},
            {"name": "session", "value": "old", "domain": "dl.example.test", "expires": expires},
            {"name": "obsolete", "value": "old", "domain": "dl.example.test", "expires": expires},
        ], proxy_server=self.proxy_server)
        self.store.add(original)
        claimed = self.store.claim(original.host, "worker-1", 20, 60)
        with session_for_identity(claimed) as session:
            def fetch(url):
                return session.get(url, proxy=self.proxy.url_for(claimed.proxy_server), timeout=3).json()

            echoed = fetch("http://dl.example.test/doi/article")
            sent = echoed["cookie"].split("; ")
            self.assertEqual({value for value in sent if value.startswith("cf_clearance=")},
                             {"cf_clearance=parent", "cf_clearance=host", "cf_clearance=article"})
            self.assertEqual(sent[0], "cf_clearance=article")
            self.assertNotIn("secure_only=hidden", sent)
            self.assertNotIn("expired=hidden", sent)
            self.assertEqual(echoed["user_agent"], original.user_agent)
            self.assertEqual(fetch("http://sub.dl.example.test/doi/article")["cookie"], "cf_clearance=parent")
            self.assertEqual(fetch("http://unrelated.test/doi/article")["cookie"], "")
            self.assertNotIn("cf_clearance=article", fetch("http://dl.example.test/other")["cookie"])
            fetch("http://dl.example.test/renew")
            self.store.heartbeat(claimed, 60)
            saved = self.saved(original.id)
            self.assertEqual(next(cookie.value for cookie in saved.cookies if cookie.name == "session"), "renewed")
            self.assertNotIn("obsolete", [cookie.name for cookie in saved.cookies])
            session.cookies.set("handoff", "saved-on-release", domain="dl.example.test")
            self.store.release(claimed)
        next_claim = self.store.claim(original.host, "worker-2", 20, 60)
        with session_for_identity(next_claim) as session:
            echoed = session.get("http://dl.example.test/other",
                                 proxy=self.proxy.url_for(next_claim.proxy_server), timeout=3).json()
            self.assertIn("session=renewed", echoed["cookie"])
            self.assertIn("handoff=saved-on-release", echoed["cookie"])
            self.assertNotIn("obsolete=", echoed["cookie"])
        self.assertEqual(next_claim.expires_at, original.expires_at)          # 保存 cookie 不延长身份寿命
        self.assertEqual(identity_row(self.store, original.id)[3], original.expires_at)

    def test_producer_acceptance_saves_response_cookies_before_insertion(self):
        original = identity([
            {"name": "session", "value": "old", "domain": "dl.example.test", "path": "/"},
        ], proxy_server=self.proxy_server)
        ips = mock.Mock()
        ips.s = self.proxy
        producer = Producer(Limits(), self.store, ips, original.host)
        producer.home = "http://dl.example.test/renew"          # 还没有成功链接、也没有队列：用首页验收
        self.assertTrue(producer._curl_usable(original))
        self.store.add(original)
        claimed = self.store.claim(original.host, "worker", 20, 60)
        self.assertEqual(next(cookie.value for cookie in claimed.cookies if cookie.name == "session"), "renewed")

    def test_stale_holder_cannot_overwrite_new_holder_cookies(self):
        original = identity([])
        self.store.add(original)
        stale = self.store.claim(original.host, "stale", 20, 60)
        self.store._x("UPDATE pl_identities SET lease_until = 0 WHERE id = %s", (original.id,))   # stale 崩溃：租约过期
        self.store.reap(original.host)
        current = self.store.claim(original.host, "current", 20, 60)
        current.cookies = jar_from_records([{"name": "session", "value": "current", "domain": "dl.example.test"}])
        self.store.heartbeat(current, 60)
        self.store.heartbeat(stale, 60)
        self.store.release(stale)
        saved = self.saved(original.id)
        self.assertEqual(jar_to_records(saved.cookies), jar_to_records(current.cookies))
        self.assertEqual(identity_row(self.store, original.id)[1], "current")
        self.assertEqual(self.store.stats(original.host).ready, 0)

    def test_expired_identity_is_not_recreated_when_saving_cookies(self):
        original = identity([])
        self.store.add(original)
        claimed = self.store.claim(original.host, "worker", 20, 60)
        self.store._x("DELETE FROM pl_identities WHERE id = %s", (original.id,))      # 期间已过期被清掉
        self.store.heartbeat(claimed, 60)
        self.store.release(claimed)
        self.assertIsNone(identity_row(self.store, original.id))
        self.assertEqual((self.store.stats(original.host).ready, self.store.stats(original.host).inuse), (0, 0))
