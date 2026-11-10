"""监控模块：配置、WAF 识别、埋点 no-op、入库聚合、HTTP 面板、浏览器模式埋点接线。
RabbitMQ 端到端用例需 PAPERLITE_TEST_AMQP=amqp://... 才跑（与 Redis 用例同样门控）。"""

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from paperlite.browser import RenderResult, classify_waf
from paperlite.monitor import reporter as mon
from paperlite.monitor import schema
from paperlite.monitor.config import MonitorConfig, load_monitor
from paperlite.monitor.db import MonitorDB


def msg(type_, ts, data, inst="n1:1", vendor="qg", batch="b"):
    return schema.envelope(type_, ts, inst.split(":")[0], inst, batch, vendor, data)


def page(ts, host="a.org", ok=True, passed=True, waf="cloudflare", ms=1000, vendor="qg"):
    return msg(schema.PAGE, ts, {"host": host, "passed": passed, "ok": ok, "status": 200 if ok else None,
                                 "waf": waf, "elapsed_ms": ms, "error": ""}, vendor=vendor)


class MonitorConfigTest(unittest.TestCase):
    def test_defaults_disabled_and_rejects_unknown(self):
        self.assertFalse(MonitorConfig().enabled)
        with self.assertRaises(ValueError):
            MonitorConfig.from_dict({"enable": True})
        with self.assertRaises(ValueError):
            MonitorConfig.from_dict({"enabled": True, "amqp_url": ""})

    def test_load_from_toml_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.toml"
            p.write_text('[monitor]\nenabled = true\namqp_url = "amqp://x"\nvendor = "qg"\n')
            cfg = load_monitor(p)
            self.assertTrue(cfg.enabled)
            self.assertEqual(cfg.vendor_name, "qg")
            self.assertFalse(load_monitor(Path(tmp) / "none.toml").enabled)

    def test_paperlite_config_accepts_monitor_section(self):
        from paperlite import config as config_mod
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.toml"
            p.write_text('[monitor]\nenabled = false\n[limits]\nmax_browsers = 3\n')
            self.assertEqual(config_mod.load_limits(p).max_browsers, 3)


class WafClassifyTest(unittest.TestCase):
    def test_vendors(self):
        self.assertEqual(classify_waf({"cf-mitigated": "challenge"}), "cloudflare")
        self.assertEqual(classify_waf({"server": "cloudflare", "cf-ray": "x"}), "cloudflare")
        self.assertEqual(classify_waf({"x-amzn-waf-action": "challenge"}), "aws_waf")
        self.assertEqual(classify_waf({"set-cookie": "aws-waf-token=abc; Path=/"}), "aws_waf")
        self.assertEqual(classify_waf({"x-datadome": "protected"}), "datadome")
        self.assertEqual(classify_waf({"set-cookie": "datadome=abc; Max-Age=1"}), "datadome")
        self.assertEqual(classify_waf({"server": "nginx"}), "none")
        self.assertEqual(classify_waf({}), "none")


class ReporterTest(unittest.TestCase):
    def tearDown(self):
        mon.shutdown(0)
        mon._reporter = mon.Reporter()

    def test_disabled_is_noop(self):
        self.assertFalse(mon.get().enabled)
        mon.page("a.org", True, True)                 # 不抛、不缓冲
        mon.proxy_check(False, 10, "slow")
        self.assertEqual(len(mon.get()._buf), 0)
        hb = mon.Heartbeat(lambda: {"x": 1})
        self.assertIsNone(hb._thread)                 # 未启用不建线程

    def test_attach_returns_none_without_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(mon.attach(Path(tmp) / "nope.toml", "b", dict))

    def test_enabled_buffers_envelopes_and_drops_oldest(self):
        r = mon.Reporter(MonitorConfig(enabled=True, buffer_size=2, vendor="qg", node="n"), batch="bt")
        r.enabled = True                              # 不起发送线程，只看缓冲
        for i in range(3):
            r.emit(schema.PAGE, host=f"h{i}")
        self.assertEqual([m["data"]["host"] for m in r._buf], ["h1", "h2"])
        self.assertEqual(r.dropped, 1)
        m = r._buf[0]
        self.assertEqual((m["v"], m["type"], m["vendor"], m["batch"], m["node"]), (1, "page", "qg", "bt", "n"))
        self.assertEqual(schema.decode(schema.encode(m)), m)

    def test_log_forwarding_skips_monitor_own_logs(self):
        import logging
        r = mon.Reporter(MonitorConfig(enabled=True))
        r.enabled = True
        h = mon._ForwardHandler(r)
        h.handle(logging.makeLogRecord({"name": "paperlite.browser_pool", "levelno": 30, "levelname": "WARNING",
                                        "msg": "提取 IP 失败"}))
        h.handle(logging.makeLogRecord({"name": "paperlite.monitor.reporter", "levelno": 30,
                                        "levelname": "WARNING", "msg": "连不上"}))
        self.assertEqual([m["data"]["message"] for m in r._buf], ["提取 IP 失败"])

    def test_redact(self):
        self.assertEqual(mon._redact("amqp://u:secret@h:5672/%2F"), "amqp://u:***@h:5672/%2F")

    def test_schema_rejects_unknown(self):
        with self.assertRaises(ValueError):
            schema.decode(b'{"v": 9, "type": "page", "data": {}}')
        with self.assertRaises(ValueError):
            schema.decode(b'{"v": 1, "type": "evil", "data": {}}')


class MonitorDBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = MonitorDB(Path(self.tmp.name) / "m.db")
        self.now = time.time()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_windows_and_rates(self):
        now = self.now
        self.db.ingest([
            page(now - 60, ms=1000), page(now - 60, ms=3000),
            page(now - 60, ok=False, passed=True, ms=2000),            # 过了盾但 404
            page(now - 60, ok=False, passed=False, ms=90000),          # 没过盾
            page(now - 3 * 86400, host="old.org"),                     # 只在 7d/30d 窗内
            msg(schema.PROXY_CHECK, now - 10, {"ok": True, "total_ms": 500, "reason": ""}),
            msg(schema.PROXY_CHECK, now - 10, {"ok": False, "total_ms": 4000, "reason": "slow_4000ms"}),
            msg(schema.PROXY_CHECK, now - 10, {"ok": False, "total_ms": 0, "reason": "exit_ip_mismatch(1.1.1.1)"}),
        ])
        d = self.db.summary("1d", now=now)
        self.assertEqual((d["pages"]["attempts"], d["pages"]["success"], d["pages"]["failed"]), (4, 2, 2))
        self.assertEqual(d["pages"]["success_rate"], 0.5)
        self.assertEqual(d["pages"]["pass_rate"], 0.75)
        self.assertEqual(d["pages"]["avg_ms"], 2000)                     # 耗时不含没过盾的 90s 超时
        self.assertAlmostEqual(d["pages"]["p95_ms"], 3000, delta=3000 * 0.07)   # 直方图分位，误差 ±6%
        self.assertEqual((d["ip"]["checked"], d["ip"]["passed"]), (3, 1))
        self.assertEqual({r["reason"] for r in d["ip"]["fail_reasons"]}, {"slow", "exit_ip_mismatch"})
        self.assertEqual([x["host"] for x in d["domains"]], ["a.org"])
        self.assertEqual(self.db.summary("7d", now=now)["pages"]["attempts"], 5)

    def test_waf_buckets_include_browser_landing(self):
        now = self.now
        self.db.ingest([
            page(now, waf="cloudflare"), page(now, waf="datadome", passed=False, ok=False),
            page(now, waf="none"),
            msg(schema.BROWSER, now, {"ok": True, "kind": "ok", "host": "a.org", "waf": "aws_waf", "seconds": 20,
                                      "error": ""}),
            msg(schema.BROWSER, now, {"ok": False, "kind": "exception", "host": "a.org", "waf": "none",
                                      "seconds": 0, "error": "boom"}),
        ])
        w = {x["waf"]: x for x in self.db.summary("1d", now=now)["waf"]}
        self.assertEqual((w["cloudflare"]["attempts"], w["cloudflare"]["pass_rate"]), (1, 1.0))
        self.assertEqual((w["datadome"]["attempts"], w["datadome"]["pass_rate"]), (1, 0.0))
        self.assertEqual((w["aws_waf"]["attempts"], w["aws_waf"]["avg_ms"]), (1, 20000))
        b = self.db.summary("1d", now=now)["browser"]
        self.assertEqual((b["starts"], b["start_ok"], b["exceptions"]), (2, 1, 1))

    def test_vendor_by_domain(self):
        now = self.now
        self.db.ingest([page(now, vendor="qg"), page(now, vendor="qg", ok=False, passed=False),
                        page(now, vendor="kdl", host="b.org"),
                        msg(schema.PROXY_CHECK, now, {"ok": True, "total_ms": 1, "reason": ""}, vendor="kdl")])
        v = {x["vendor"]: x for x in self.db.summary("1d", now=now)["vendors"]}
        self.assertEqual(v["qg"]["success_rate"], 0.5)
        self.assertEqual(v["qg"]["domains"][0]["host"], "a.org")
        self.assertEqual((v["kdl"]["ip_checked"], v["kdl"]["ip_pass_rate"]), (1, 1.0))

    def test_heartbeat_latest_wins_and_online(self):
        now = self.now
        hb = lambda ts, n, inst="n1:1": msg(schema.HEARTBEAT, ts, {"mode": "browser", "browsers_online": n,
                                                                   "workers": 20, "pending": 100, "running": n,
                                                                   "success": 0, "failed": 0}, inst=inst)
        self.db.ingest([hb(now - 5, 7), hb(now - 30, 3),                # 乱序到达：旧心跳不覆盖新的
                        hb(now - 3600, 9, inst="n2:2")])                # 早已离线
        d = self.db.summary("1d", now=now)
        self.assertEqual((d["browser"]["online"], d["browser"]["instances"]), (7, 1))
        self.assertEqual(d["realtime"]["pending"], 100)
        self.assertEqual(len(d["realtime"]["instances"]), 2)

    def test_prune_and_timeline(self):
        now = self.now
        self.db.ingest([page(now - 40 * 86400), page(now - 100)])
        self.db.prune(now=now, force=True)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM page_h").fetchone()[0], 1)   # 过期汇总已删
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM page").fetchone()[0], 1)
        tl = self.db.summary("1d", now=now)["trend"]
        self.assertEqual(sum(b["attempts"] for b in tl), 1)
        self.assertGreaterEqual(len(tl), 24)
        with self.assertRaises(ValueError):
            self.db.summary("2d")

    def test_domain_table_paging_sort_filter(self):
        now, evs = self.now, []
        for i in range(45):                                            # 45 个域名，访问次数 i+1 递增
            evs += [page(now - 60, host=f"d{i:02d}.org") for _ in range(i + 1)]
        evs += [page(now - 60, host="bad.org", ok=False, passed=False) for _ in range(30)]   # 样本够、全失败 → 异常
        evs += [page(now - 60, host="tiny.org", ok=False, passed=False) for _ in range(2)]   # 样本不足：不算异常
        self.db.ingest(evs)
        d = self.db.domains("1d", now=now, limit=20)
        self.assertEqual((d["total"], d["matched"], len(d["rows"])), (47, 47, 20))
        self.assertEqual(d["rows"][0]["host"], "d44.org")                # 默认按访问次数降序
        self.assertIsNotNone(d["rows"][0]["p95_ms"])                     # P95 只给当前页算，但当前页有
        self.assertEqual(len(self.db.domains("1d", now=now, offset=40, limit=20)["rows"]), 7)
        self.assertEqual(d["abnormal"], 1)
        worst = self.db.domains("1d", sort="success_rate", order="asc", now=now)["rows"]
        self.assertEqual(worst[0]["host"], "bad.org")
        full = [x["host"] for x in self.db.domains("1d", sort="success_rate", order="asc", now=now, limit=200)["rows"]]
        self.assertGreater(full.index("tiny.org"), full.index("d19.org"))   # 样本不足的 0% 排在所有样本足够的后面
        self.assertEqual([x["host"] for x in self.db.domains("1d", abnormal=True, now=now)["rows"]], ["bad.org"])
        s = self.db.domains("1d", q="D0", now=now)
        self.assertEqual((s["matched"], s["total"], s["abnormal"]), (10, 47, 1))     # 搜索不分大小写；总数不受搜索影响
        p95 = self.db.domains("1d", sort="p95_ms", now=now)["rows"]
        self.assertTrue(all(x["p95_ms"] is not None for x in p95[:5]))
        for bad in ({"sort": "evil"}, {"order": "up"}):
            with self.assertRaises(ValueError):
                self.db.domains("1d", now=now, **bad)

    def test_domain_group_by_site(self):
        from paperlite.monitor.db import site_of
        self.assertEqual(site_of("onlinelibrary.wiley.com"), "wiley.com")
        self.assertEqual(site_of("pubs.rsc.ac.uk"), "rsc.ac.uk")
        self.assertEqual(site_of("www.cnki.com.cn"), "cnki.com.cn")
        self.assertEqual(site_of("a.org"), "a.org")
        self.assertEqual(site_of("10.0.0.1"), "10.0.0.1")
        now = self.now
        self.db.ingest([page(now, host="onlinelibrary.wiley.com"), page(now, host="www.wiley.com"),
                        page(now, host="link.springer.com")])
        g = self.db.domains("1d", group=True, now=now)
        rows = {x["host"]: x for x in g["rows"]}
        self.assertEqual((g["total"], rows["wiley.com"]["attempts"]), (2, 2))
        self.assertEqual(rows["wiley.com"]["hosts"], ["onlinelibrary.wiley.com", "www.wiley.com"])
        self.assertIsNotNone(rows["wiley.com"]["p95_ms"])
        self.assertEqual(self.db.domains("1d", group=True, q="onlinelibrary", now=now)["matched"], 1)   # 搜子域也能命中

    def test_demo_data_renders_all_sections(self):
        from paperlite.monitor.cli import demo_messages
        self.db.ingest(demo_messages(self.now, days=3))
        d = self.db.summary("30d", now=self.now)
        self.assertTrue(d["domains"] and d["vendors"] and d["realtime"]["events"])
        self.assertEqual({w["waf"] for w in d["waf"] if w["attempts"]}, {"cloudflare", "aws_waf", "datadome"})
        json.dumps(d)                                                   # 可直接序列化给前端


class DashboardHttpTest(unittest.TestCase):
    def test_pages_and_token(self):
        from paperlite.monitor.dashboard import make_server
        with tempfile.TemporaryDirectory() as tmp:
            db = MonitorDB(Path(tmp) / "m.db")
            db.ingest([page(time.time())])
            srv = make_server(MonitorConfig(port=0, token="s3"), db)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            try:
                get = lambda p: urllib.request.urlopen(base + p, timeout=5)
                self.assertEqual(json.load(get("/health")), {"ok": True})
                with self.assertRaises(urllib.error.HTTPError) as e:
                    get("/api/summary")
                self.assertEqual(e.exception.code, 401)
                self.assertIn("PaperLite", get("/?token=s3").read().decode())
                self.assertEqual(json.load(get("/api/summary?window=7d&token=s3"))["pages"]["attempts"], 1)
                with self.assertRaises(urllib.error.HTTPError) as e:
                    get("/api/summary?window=9d&token=s3")
                self.assertEqual(e.exception.code, 400)
                dm = json.load(get("/api/domains?window=1d&sort=success_rate&order=asc&q=a.&group=1&token=s3"))
                self.assertEqual((dm["total"], dm["rows"][0]["host"]), (1, "a.org"))
                for bad in ("sort=evil", "offset=x", "order=up"):
                    with self.assertRaises(urllib.error.HTTPError) as e:
                        get(f"/api/domains?{bad}&token=s3")
                    self.assertEqual(e.exception.code, 400)
                with self.assertRaises(urllib.error.HTTPError) as e:
                    get("/api/domains")
                self.assertEqual(e.exception.code, 401)
            finally:
                srv.shutdown()
                srv.server_close()
                db.close()


class DashboardAuthTest(unittest.TestCase):
    def _serve(self, cfg):
        from paperlite.monitor.dashboard import make_server
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = MonitorDB(Path(tmp.name) / "m.db"); self.addCleanup(db.close)
        srv = make_server(cfg, db)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def _get(self, url, user=None, pw=None):
        import base64
        req = urllib.request.Request(url)
        if user is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}

    def test_basic_auth(self):
        base = self._serve(MonitorConfig(port=0, username="ops", password="密码 with:colon"))
        code, h = self._get(base + "/")
        self.assertEqual(code, 401)
        self.assertIn("Basic", h.get("www-authenticate", ""))            # 浏览器据此弹登录框
        self.assertEqual(self._get(base + "/", "ops", "wrong")[0], 401)
        self.assertEqual(self._get(base + "/", "admin", "密码 with:colon")[0], 401)
        self.assertEqual(self._get(base + "/", "ops", "密码 with:colon")[0], 200)   # 非 ASCII / 含冒号密码
        self.assertEqual(self._get(base + "/api/summary", "ops", "密码 with:colon")[0], 200)
        self.assertEqual(self._get(base + "/health")[0], 200)             # 健康检查不需要登录

    def test_token_and_password_either_works(self):
        base = self._serve(MonitorConfig(port=0, password="p", token="t"))
        self.assertEqual(self._get(base + "/api/summary?token=t")[0], 200)
        self.assertEqual(self._get(base + "/api/summary", "admin", "p")[0], 200)
        self.assertEqual(self._get(base + "/api/summary?token=x")[0], 401)

    def test_refuses_public_bind_without_auth(self):
        from paperlite.monitor import cli
        with self.assertRaises(SystemExit) as e:
            cli._serve(MonitorConfig(bind="0.0.0.0", port=0), consume=False)
        self.assertIn("password", str(e.exception))


class BrowserPoolMonitorWiringTest(unittest.TestCase):
    """浏览器模式埋点：每次页面尝试发一条 page（带 host / waf / 过盾与成功区分），在线数跟着浏览器开关。"""

    def setUp(self):
        from paperlite.tasks import TaskDB
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TaskDB(Path(self.tmp.name) / "tasks.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _pool(self, url):
        from paperlite.browser_pool import BrowserPool
        from paperlite.config import Limits
        self.db.import_urls([{"url": url}])
        return BrowserPool(Limits(), None, self.db, Path(self.tmp.name) / "out", workers=1, no_proxy=True)

    def _run(self, pool, r):
        from paperlite.proxy import Lease
        task = self.db.claim("w")
        session = SimpleNamespace(render=mock.AsyncMock(return_value=r))
        lease = Lease(server="x", exit_ip="1.2.3.4", area="", expires_at=time.time() + 600)
        with mock.patch.object(mon, "page") as p:
            asyncio.run(pool._do_task(task, session, lease, "t"))
        return p

    def test_success_404_and_timeout(self):
        url = "https://www.sciencedirect.com/science/article/pii/S1"
        pool = self._pool(url)
        p = self._run(pool, RenderResult(ok=True, seconds=1, status=200, url=url, title="T", html="x",
                                         waf="cloudflare"))
        args, kw = p.call_args
        self.assertEqual(args[0], "www.sciencedirect.com")
        self.assertEqual((kw["passed"], kw["ok"], kw["status"], kw["waf"]), (True, True, 200, "cloudflare"))

    def test_404_counts_as_passed_not_ok(self):
        url = "https://www.sciencedirect.com/science/article/pii/S2"
        p = self._run(self._pool(url), RenderResult(ok=True, seconds=1, status=404, url=url, title="NF",
                                                    waf="cloudflare"))
        _, kw = p.call_args
        self.assertEqual((kw["passed"], kw["ok"], kw["status"]), (True, False, 404))

    def test_timeout_not_passed(self):
        url = "https://www.sciencedirect.com/science/article/pii/S3"
        p = self._run(self._pool(url), RenderResult(ok=False, seconds=90, error="TimeoutError", waf="cloudflare"))
        _, kw = p.call_args
        self.assertEqual((kw["passed"], kw["ok"], kw["status"], kw["error"]), (False, False, None, "TimeoutError"))

    def test_online_tracks_open_browsers(self):
        from paperlite import browser_pool as bp
        from paperlite.config import Limits
        seen = []

        class FakeSession:
            def __init__(s, *a, **k):
                pass
            async def start(s):
                pass
            async def render(s, url, timeout_s, settle_s=2.0):
                seen.append(pool.online())
                return RenderResult(ok=True, seconds=1, status=200, url=url, title="T", html="x", waf="none")
            async def close(s):
                pass

        self.db.import_urls([{"url": f"https://a.org/{i}"} for i in range(2)])
        with mock.patch.object(bp, "BrowserSession", FakeSession), \
                mock.patch.object(mon, "browser_start") as bs:
            pool = bp.BrowserPool(Limits(page_interval_s=(0, 0)), None, self.db, Path(self.tmp.name) / "o",
                                  workers=1, no_proxy=True)
            pool.run()
        self.assertEqual(seen, [0, 1, 1])               # 落地页时还没算在线；之后两篇都在线
        self.assertEqual(pool.online(), 0)              # 收工后归零
        self.assertEqual(bs.call_args.args[:2], (True, "ok"))


class ProxyCheckMonitorTest(unittest.TestCase):
    def test_one_event_per_ip(self):
        from paperlite.proxy import IpSource, Lease, ProbeResult, ProxySettings
        src = IpSource(ProxySettings(api_url="https://api/x"))
        lease = Lease(server="1.2.3.4:1", exit_ip="1.2.3.4", area="", expires_at=time.time() + 600)
        with mock.patch.object(src, "_probe", return_value=ProbeResult(ok=True, status=200, total_ms=5000,
                                                                     egress="1.2.3.4")), \
                mock.patch.object(mon, "proxy_check") as pc:
            self.assertFalse(src._check(lease))         # 两个目标都慢
        pc.assert_called_once()
        self.assertEqual((pc.call_args.args[0], pc.call_args.args[2]), (False, "slow_5000ms"))


@unittest.skipUnless(os.environ.get("PAPERLITE_TEST_AMQP"), "设置 PAPERLITE_TEST_AMQP=amqp://... 才跑")
class RabbitEndToEndTest(unittest.TestCase):
    def test_publish_consume_aggregate(self):
        from paperlite.monitor.consumer import consume_forever
        suffix = uuid.uuid4().hex[:8]
        cfg = MonitorConfig(enabled=True, amqp_url=os.environ["PAPERLITE_TEST_AMQP"],
                            exchange=f"pl-test-{suffix}", queue=f"pl-test-{suffix}", vendor="qg")
        with tempfile.TemporaryDirectory() as tmp:
            db = MonitorDB(Path(tmp) / "m.db")
            stop = threading.Event()
            t = threading.Thread(target=consume_forever, args=(cfg, db, stop), daemon=True)
            t.start()
            time.sleep(1.5)                              # 等队列声明好再发
            mon.start(cfg, "e2e")
            try:
                mon.page("a.org", True, True, 200, "cloudflare", 1234)
                mon.proxy_check(True, 300)
                deadline = time.time() + 15
                while time.time() < deadline and db.summary("1d")["pages"]["attempts"] < 1:
                    time.sleep(0.3)
                d = db.summary("1d")
                self.assertEqual(d["pages"]["attempts"], 1)
                self.assertEqual(d["vendors"][0]["vendor"], "qg")
            finally:
                mon.shutdown()
                mon._reporter = mon.Reporter()
                stop.set()
                t.join(5)
                import pika
                conn = pika.BlockingConnection(pika.URLParameters(cfg.amqp_url))
                ch = conn.channel()
                ch.queue_delete(cfg.queue)
                ch.exchange_delete(cfg.exchange)
                conn.close()
                db.close()
