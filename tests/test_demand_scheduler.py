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


if __name__ == "__main__":
    unittest.main()
