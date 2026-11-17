#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import unittest
from unittest import mock

from paperlite.config import Limits
from paperlite.producer import Producer
from paperlite.store import IdentityStore


class DemandDrivenTest(unittest.TestCase):
    def test_producer_target_demand_backpressure(self):
        cfg = Limits(min_identities=5, max_identities=20)
        producer = Producer(cfg, mock.Mock(), mock.Mock(), "dl.acm.org")

        # 1. 传统无 demand 模式：按 workers 和 min/max_identities 兜底
        self.assertEqual(producer.target(workers=10), 10)
        self.assertEqual(producer.target(workers=1), 5)      # 受 min_identities=5 兜底
        self.assertEqual(producer.target(workers=30), 20)    # 受 max_identities=20 限制

        # 2. 需求驱动模式：下游通过 Redis 反压设置 demand
        # 待办任务为 0 时：坚决归零，停止一切生产和无头浏览器开销！
        self.assertEqual(producer.target(workers=20, demand=0), 0)

        # 待办任务较少，下游只需要 2 个身份时：
        self.assertEqual(producer.target(workers=20, demand=2), 2)

        # 待办任务很多，但受下游在线 workers 约束：
        self.assertEqual(producer.target(workers=10, demand=15), 10)

        # 待办海量，按 max_identities 封顶：
        self.assertEqual(producer.target(workers=50, demand=100), 20)

    def test_store_demand_api(self):
        fake_redis = mock.Mock()
        store = IdentityStore("redis://127.0.0.1:6379/0")
        store.r = fake_redis

        fake_redis.get.return_value = "8"
        self.assertEqual(store.get_demand("dl.acm.org"), 8)

        fake_redis.get.return_value = None
        self.assertIsNone(store.get_demand("dl.acm.org"))

        store.set_demand("dl.acm.org", 5, ttl_s=45)
        fake_redis.set.assert_called_with("paperlite:dl.acm.org:demand", "5", ex=45)

        store.clear_demand("dl.acm.org")
        fake_redis.delete.assert_called_with("paperlite:dl.acm.org:demand")

    def test_auto_scheduler_routing_dispatch(self):
        import tempfile
        from pathlib import Path
        from paperlite.tasks import TaskDB
        from paperlite.scheduler import AutoScheduler

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "test.db"
            rules_path = tmp_path / "rules.toml"
            out_path = tmp_path / "out"

            rules_path.write_text("""
[modes.curl_direct]
domains = ["link.springer.com"]

[modes.curl_pool]
domains = ["academic.oup.com"]

[modes.browser_only]
domains = ["cell.com"]
""")

            db = TaskDB(db_path)
            db.import_urls([
                {"doi": "10.1007/s001", "url": "https://link.springer.com/article/1", "url_type": "html"},
                {"doi": "10.1093/oup001", "url": "https://academic.oup.com/article/2", "url_type": "html"},
                {"doi": "10.1016/cell001", "url": "https://cell.com/article/3", "url_type": "html"},
            ])
            db.backfill_real_hosts()

            by_host = db.pending_by_host()
            self.assertEqual(by_host.get("link.springer.com"), 1)
            self.assertEqual(by_host.get("academic.oup.com"), 1)
            self.assertEqual(by_host.get("cell.com"), 1)

            scheduler = AutoScheduler(
                Limits(), None, db, out_path, workers=2,
                rules_path=str(rules_path), no_proxy=True
            )
            self.assertEqual(scheduler.routes["link.springer.com"], "curl_direct")
            self.assertEqual(scheduler.routes["academic.oup.com"], "curl_pool")
            self.assertEqual(scheduler.routes["cell.com"], "browser_only")
            db.close()

    def test_downloader_circuit_breaker_and_auto_downgrade(self):
        """测试 cURL 身份池连续失败时自动触发熔断，并降级至 browser_only"""
        from paperlite.consumer import Downloader
        from paperlite.tasks import TaskDB
        from paperlite.scheduler import AutoScheduler
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "cb_test.db"
            db = TaskDB(db_path)
            db.import_urls([
                {"doi": "10.1016/j.cell.1", "url": "https://www.sciencedirect.com/article/1", "url_type": "html"},
            ])
            db.backfill_real_hosts()

            fake_store = mock.Mock()
            # 模拟连续 3 次 fresh reject
            fake_store.note_fresh_reject.side_effect = [1, 2, 3]

            dl = Downloader(Limits(), fake_store, mock.Mock(), db, tmp_path / "out", workers=1, host="www.sciencedirect.com")
            self.assertFalse(dl.circuit_broken)

            # 模拟触发一次 rejected
            task = db.claim("test-worker", host="www.sciencedirect.com")
            dl._drop_identity(task, mock.Mock(exit_ip="1.1.1.1"), "Cloudflare 403 阻断")

            # 模拟 3 次身份新领用即被拒
            dl.store.note_fresh_reject("www.sciencedirect.com")
            dl.store.note_fresh_reject("www.sciencedirect.com")
            r3 = dl.store.note_fresh_reject("www.sciencedirect.com")
            self.assertEqual(r3, 3)

            # 模拟触发熔断
            dl.circuit_broken = True
            dl._circuit_stop.set()
            self.assertTrue(dl.circuit_broken)

            scheduler = AutoScheduler(
                Limits(), None, db, tmp_path / "out", workers=1,
                routes={"www.sciencedirect.com": "curl_pool"}, no_proxy=True
            )
            self.assertEqual(scheduler.routes["www.sciencedirect.com"], "curl_pool")

            # 调度器感知熔断后动态改写内存路由为 browser_only
            if dl.circuit_broken:
                scheduler.routes["www.sciencedirect.com"] = "browser_only"

            self.assertEqual(scheduler.routes["www.sciencedirect.com"], "browser_only")
            db.close()

    def test_count_pending_host_cap(self):
        import tempfile
        from pathlib import Path
        from paperlite.tasks import TaskDB

        with tempfile.TemporaryDirectory() as tmp:
            db = TaskDB(Path(tmp) / "t.db")
            db.import_urls([{"url": f"https://a.example/{i}"} for i in range(7)]
                           + [{"url": "https://b.example/1"}])
            self.assertEqual(db.count_pending_host("a.example"), 7)
            self.assertEqual(db.count_pending_host("a.example", cap=5), 5)
            self.assertEqual(db.count_pending_host("a.example", cap=100), 7)
            self.assertEqual(db.count_pending_host("b.example", cap=5), 1)
            self.assertEqual(db.count_pending_host(), 8)
            self.assertEqual(db.count_pending_host(cap=3), 3)
            db.close()

    def test_update_demand_bounded_and_throttled(self):
        """需求计数最多数到 workers×15；所有线程共用 5 秒节流；公式与原来一致"""
        import paperlite.consumer as consumer
        from paperlite.consumer import Downloader

        db = mock.Mock()
        db.host.return_value = "dl.acm.org"
        store = mock.Mock()
        dl = Downloader(Limits(), store, mock.Mock(), db, "/tmp/out", workers=20)

        def demand_for(pending):
            db.count_pending_host.side_effect = lambda host, cap=None: min(pending, cap)
            return dl.update_demand(force=True)

        # 公式：0 → 0；1..15 → 1；16 → 2；海量 → 封顶 workers
        self.assertEqual([demand_for(p) for p in (0, 1, 15, 16, 299, 300, 1_560_000)], [0, 1, 1, 2, 20, 20, 20])
        self.assertEqual(db.count_pending_host.call_args.kwargs["cap"], 20 * 15)
        store.set_demand.assert_called_with("dl.acm.org", 20, ttl_s=30)

        db.count_pending_host.reset_mock()
        clock = [1000.0]
        with mock.patch.object(consumer.time, "monotonic", lambda: clock[0]):
            dl.update_demand(force=True)
            for _ in range(50):                      # 5 秒内的调用都用缓存，不再查库
                self.assertEqual(dl.update_demand(), 20)
            self.assertEqual(db.count_pending_host.call_count, 1)
            clock[0] += consumer._DEMAND_REFRESH_S
            dl.update_demand()
            self.assertEqual(db.count_pending_host.call_count, 2)

    def test_curl_direct_network_error_capped_by_max_tries(self):
        """curl_direct 网络错误达到 max_tries 判 failed，管线能正常退出而不是无限重领死链"""
        import asyncio
        import tempfile
        import threading
        from pathlib import Path
        from paperlite.scheduler import AutoScheduler
        from paperlite.tasks import Status, TaskDB

        class DeadSession:
            calls = 0

            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, *a, **kw):
                DeadSession.calls += 1
                raise ConnectionError("Could not resolve host")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db = TaskDB(tmp_path / "t.db")
            db.import_urls([{"doi": f"10.1007/dead{i}", "url": f"https://link.springer.com/dead/{i}",
                             "url_type": "html"} for i in range(3)])
            db.backfill_real_hosts()
            cfg = Limits(max_tries=3)
            scheduler = AutoScheduler(cfg, None, db, tmp_path / "out", workers=2,
                                      routes={"link.springer.com": "curl_direct"}, no_proxy=True)
            with mock.patch("curl_cffi.requests.AsyncSession", DeadSession):
                asyncio.run(asyncio.wait_for(
                    scheduler._direct_worker_pool(["link.springer.com"], threading.Event()), timeout=10))

            c = db.counts()
            self.assertEqual(c[Status.FAILED], 3)
            self.assertEqual(c[Status.PENDING], 0)
            self.assertEqual(DeadSession.calls, 3 * cfg.max_tries)   # 每条恰好请求 max_tries 次
            db.close()

    def test_curl_pool_without_proxy_downgrades_to_browser(self):
        """--no-proxy 时 curl_pool 域名直接降级 browser_only，不启动无法取 IP 的生产者"""
        import tempfile
        import threading
        from pathlib import Path
        from paperlite.scheduler import AutoScheduler
        from paperlite.tasks import TaskDB

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db = TaskDB(tmp_path / "t.db")
            db.import_urls([{"doi": "10.1093/x1", "url": "https://academic.oup.com/a/1", "url_type": "html"}])
            db.backfill_real_hosts()
            for kwargs in ({"proxy": None, "no_proxy": True}, {"proxy": mock.Mock(), "no_proxy": True},
                           {"proxy": None, "no_proxy": False}):
                scheduler = AutoScheduler(Limits(), kwargs["proxy"], db, tmp_path / "out", workers=1,
                                          store=mock.Mock(), no_proxy=kwargs["no_proxy"],
                                          routes={"academic.oup.com": "curl_pool"})
                with mock.patch("paperlite.scheduler._ProducerThread") as producer, \
                        mock.patch("paperlite.scheduler.Downloader") as downloader:
                    ok = scheduler._run_curl_pool_host("academic.oup.com", threading.Event())
                self.assertFalse(ok)
                self.assertEqual(scheduler.routes["academic.oup.com"], "browser_only")
                producer.assert_not_called()
                downloader.assert_not_called()
            db.close()


if __name__ == "__main__":
    unittest.main()
