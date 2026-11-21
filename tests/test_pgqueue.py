"""任务队列（paperlite.pgqueue，PostgreSQL 按块领取）。需要真实 PostgreSQL：

    docker run -d --name paperlite-pg -e POSTGRES_PASSWORD=pl -e POSTGRES_DB=paperlite -p 5439:5432 postgres:16-alpine
    PAPERLITE_TEST_PG=postgresql://postgres:pl@127.0.0.1:5439/paperlite uv run pytest tests/test_pgqueue.py
"""

import os
import random
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

DSN = os.environ.get("PAPERLITE_TEST_PG")


def _rows(n, hosts=("a.com",)):
    rows = [{"doi": f"10.1/x{i:06d}", "url": f"https://{hosts[i % len(hosts)]}/{i}"} for i in range(n)]
    random.Random(7).shuffle(rows)
    return rows


@unittest.skipUnless(DSN, "需要 PAPERLITE_TEST_PG")
class PgQueueTest(unittest.TestCase):
    def setUp(self):
        from paperlite import pgqueue
        self.pg = pgqueue
        self.schema = "t_" + uuid.uuid4().hex[:10]
        self.queues = []

    def tearDown(self):
        import psycopg
        for q in self.queues:
            q.close()
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def q(self, worker="w1", batch="b1"):
        q = self.pg.PgTaskQueue(DSN, batch, schema=self.schema, worker=worker)
        self.queues.append(q)
        return q

    def test_import_sorts_dedups_and_chunks_per_host(self):
        q = self.q()
        rows = _rows(2500, hosts=("a.com", "a.com", "b.com"))
        self.assertEqual(q.import_urls(rows), (2500, 0))
        self.assertEqual(q.import_urls(rows[:10]), (0, 10))          # 重复导入：已存在、不重置
        self.assertEqual(q.build_chunks(1000), 3)                    # a.com 1667 → 2 块；b.com 833 → 1 块
        self.assertEqual(q.build_chunks(1000), 0)                    # 已分块的不再切
        seen = []
        while (ch := q.claim_chunk()) is not None:
            self.assertEqual({t.url.split("/")[2] for t in ch.tasks}, {ch.host})   # 一块只有一个主机
            dois = [t.doi for t in ch.tasks]
            self.assertEqual(dois, sorted(dois))                     # 块内按 doi 顺序
            seen.append((ch.host, len(ch.tasks)))
            q.complete(ch.chunk_id)
        self.assertEqual(seen, [("a.com", 1000), ("a.com", 667), ("b.com", 833)])

    def test_hosts_ordered_by_task_count_not_name(self):
        """热身主机取任务最多的：排序靠前的坏主机（.asm.org）不能挡住整个批次。"""
        q = self.q()
        q.import_urls([{"url": "https://.asm.org/content/1"}] +
                      [{"url": f"https://link.springer.com/{i}"} for i in range(5)] +
                      [{"url": f"https://b.org/{i}"} for i in range(2)])
        self.assertEqual(q.hosts(), ["link.springer.com", "b.org", ".asm.org"])     # 未切块
        q.build_chunks(1000)
        self.assertEqual(q.hosts(), ["link.springer.com", "b.org", ".asm.org"])     # 切块后

    def test_same_url_twice_in_one_import_keeps_one(self):
        q = self.q()
        rows = [{"doi": "10.1/b", "url": "https://a.com/dup"}, {"doi": "10.1/a", "url": "https://a.com/dup"}]
        self.assertEqual(q.import_urls(rows), (1, 1))

    def test_concurrent_build_chunks_never_double_assign(self):
        setup = self.q("setup")
        setup.import_urls(_rows(5000))
        made = []

        def run(i):
            made.append(self.q(f"c{i}").build_chunks(100))

        ts = [threading.Thread(target=run, args=(i,)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(made), [0, 0, 0, 50])        # 只有一个真的切了
        n = setup._conn.execute("SELECT count(*), count(DISTINCT chunk_id) FROM pl_tasks").fetchone()
        self.assertEqual(n, (5000, 50))

    def test_concurrent_claims_never_share_a_chunk(self):
        setup = self.q("setup")
        setup.import_urls(_rows(20_000))
        setup.build_chunks(100)                                      # 200 块
        claimed, lock = [], threading.Lock()

        def run(i):
            w = self.q(f"w{i}")
            while (ch := w.claim_chunk()) is not None:
                with lock:
                    claimed.append((ch.chunk_id, len(ch.tasks)))
                w.report([self.pg.Result(t.id, "success") for t in ch.tasks])
                self.assertTrue(w.complete(ch.chunk_id))

        threads = [threading.Thread(target=run, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = [c for c, _ in claimed]
        self.assertEqual(len(ids), 200)
        self.assertEqual(len(set(ids)), 200)
        self.assertEqual(sum(n for _, n in claimed), 20_000)
        st = setup.stats()
        self.assertEqual(st.tasks, {"success": 20_000})
        self.assertEqual(st.chunks, {"done": 200})

    def test_expired_lease_is_reclaimed_and_old_owner_loses_it(self):
        a, b = self.q("a"), self.q("b")
        a.import_urls(_rows(10))
        a.build_chunks(10)
        ch = a.claim_chunk(lease_s=0)                                # 立即过期：模拟机器挂了
        a.report([self.pg.Result(ch.tasks[0].id, "success")])        # 挂之前已回写的一条
        ch2 = b.claim_chunk()
        self.assertEqual(ch2.chunk_id, ch.chunk_id)
        self.assertEqual(ch2.attempts, 2)
        self.assertEqual(len(ch2.tasks), 9)                          # 已写回的不再下发
        self.assertFalse(a.renew(ch.chunk_id))                       # 原主人续租失败
        self.assertFalse(a.complete(ch.chunk_id))
        # 原主人后来才上报同一条：只改 pending 行，b 的结果不被覆盖
        b.report([self.pg.Result(ch2.tasks[0].id, "failed", error_kind="not_found")])
        self.assertEqual(a.report([self.pg.Result(ch2.tasks[0].id, "success")]), 0)
        self.assertTrue(b.complete(ch2.chunk_id))

    def test_retry_waits_then_goes_to_next_generation(self):
        q = self.q()
        q.import_urls(_rows(5))
        q.build_chunks(5)
        ch = q.claim_chunk()
        t0, t1, t2 = ch.tasks[:3]
        q.report([
            self.pg.Result(t0.id, self.pg.RETRY, error_kind="network", retry_delay_s=0),
            self.pg.Result(t1.id, self.pg.RETRY, error_kind="rate_limited", retry_delay_s=3600),
            self.pg.Result(t2.id, self.pg.RETRY, count_retry=False, challenged=True, retry_delay_s=0),
        ] + [self.pg.Result(t.id, "success") for t in ch.tasks[3:]])
        q.complete(ch.chunk_id)
        self.assertIsNone(q.claim_chunk())
        self.assertEqual(q.requeue_leftovers(100), 1)               # 只有到点的 t0、t2 进入第 1 代
        ch1 = q.claim_chunk()
        self.assertEqual(ch1.generation, 1)
        self.assertEqual([t.id for t in ch1.tasks], [t0.id, t2.id])  # 被挑战的排在后面
        self.assertEqual([(t.retry_count, t.challenge_count) for t in ch1.tasks], [(1, 0), (0, 1)])
        q.report([self.pg.Result(t0.id, "success")])
        q.complete(ch1.chunk_id)                                     # t2 没回写就 complete：仍是剩余任务
        self.assertEqual(q.requeue_leftovers(100), 1)
        ch2 = q.claim_chunk()
        self.assertEqual((ch2.generation, [t.id for t in ch2.tasks]), (2, [t2.id]))
        self.assertEqual(q.stats().tasks, {"success": 3, "pending": 2})   # t1 还在等 1 小时

    def test_chunk_without_claimable_tasks_is_skipped(self):
        q = self.q()
        q.import_urls(_rows(4))
        q.build_chunks(2)                                            # 2 块
        first = q.claim_chunk()
        q.report([self.pg.Result(t.id, "success") for t in first.tasks])
        # 第一块全部写完但没 complete，租约过期后被回收：再领时没有可领任务 → 直接记 done、跳到下一块
        q.reap_expired()
        q._conn.execute("UPDATE pl_chunks SET lease_until = now() - interval '1 second' WHERE chunk_id = %s",
                        (first.chunk_id,))
        nxt = q.claim_chunk()
        self.assertNotEqual(nxt.chunk_id, first.chunk_id)
        self.assertEqual(q.stats().chunks, {"done": 1, "leased": 1})

    def test_lease_keeper_flags_lost_lease(self):
        a, b = self.q("a"), self.q("b")
        a.import_urls(_rows(3))
        a.build_chunks(3)
        ch = a.claim_chunk(lease_s=0)
        b.claim_chunk()                                              # 被 b 回收
        with self.pg.LeaseKeeper(a, ch.chunk_id, lease_s=60, interval_s=0.05) as k:
            self.assertTrue(k.lost.wait(2))


@unittest.skipUnless(DSN, "需要 PAPERLITE_TEST_PG")
class PgTaskSourceTest(unittest.TestCase):
    """PgTaskSource：给下载 worker 用的任务来源（按块领取、本地队列、攒批回写）。"""

    def setUp(self):
        from paperlite import pgqueue
        self.pg = pgqueue
        self.schema = "s_" + uuid.uuid4().hex[:10]
        self.sources = []

    def tearDown(self):
        import psycopg
        for s in self.sources:
            s.close()
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def src(self, worker, **kw):
        kw.setdefault("chunk_size", 4)
        kw.setdefault("flush_s", 0.2)
        kw.setdefault("remote_check_s", 0)
        q = self.pg.PgTaskQueue(DSN, "b1", schema=self.schema, worker=worker)
        s = self.pg.PgTaskSource(q, **kw)
        self.sources.append(s)
        return s

    def queue(self):
        q = self.pg.PgTaskQueue(DSN, "b1", schema=self.schema, worker="inspect")
        self.addCleanup(q.close)
        return q

    def test_import_builds_chunks_and_claim_drains_everything_once(self):
        a, b = self.src("a"), self.src("b")
        self.assertEqual(a.import_urls(_rows(10)), (10, 0))
        self.assertEqual(a.hosts(), ["a.com"])
        got, lock = [], threading.Lock()

        def run(s):
            while (t := s.claim("x")) is not None:
                with lock:
                    got.append(t.id)
                s.finish(t, "success", file_size=1)

        ts = [threading.Thread(target=run, args=(s,)) for s in (a, a, b, b)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        self.assertEqual(len(got), 10)
        self.assertEqual(len(set(got)), 10)
        st = self.queue().stats()
        self.assertEqual((st.tasks, st.chunks), ({"success": 10}, {"done": 3}))
        self.assertEqual(a.count_claimable(cap=1), 0)
        self.assertFalse(a.has_status("running"))

    def test_retry_comes_back_in_same_run_after_other_tasks(self):
        s = self.src("a")
        s.import_urls(_rows(6))
        order, tries = [], {}
        while (t := s.claim("x")) is not None:
            order.append(t.doi)
            tries[t.doi] = tries.get(t.doi, 0) + 1
            if t.doi == "10.1/x000000" and tries[t.doi] == 1:
                s.retry_later(t, count=True, error_kind="network", error_message="reset")
            else:
                s.finish(t, "success")
        self.assertEqual(order[-1], "10.1/x000000")           # 重试排到最后（下一代块）
        self.assertEqual(tries["10.1/x000000"], 2)
        self.assertEqual(self.queue().stats().tasks, {"success": 6})

    def test_challenged_until_max_then_failed(self):
        s = self.src("a")
        s.import_urls(_rows(1))
        results = []
        while (t := s.claim("x")) is not None:
            results.append(s.challenged(t, 3, "1.1.1.1", "HTTP 403 挑战"))
        self.assertEqual(results, [False, False, True])
        st = self.queue().stats().tasks
        self.assertEqual(st, {"failed": 1})

    def test_close_releases_unfinished_chunk_for_other_machines(self):
        a = self.src("a")
        a.import_urls(_rows(4))
        t = a.claim("x")
        a.finish(t, "success")
        a.close()                                             # 中断：剩 3 条没做
        self.sources.remove(a)
        b = self.src("b")
        rest = []
        while (t := b.claim("y")) is not None:
            rest.append(t.id)
            b.finish(t, "success")
        self.assertEqual(len(rest), 3)                        # 不用等租约过期
        self.assertEqual(self.queue().stats().tasks, {"success": 4})

    def test_lost_lease_drops_local_tasks(self):
        a = self.src("a", lease_s=0.3)
        a.import_urls(_rows(4))
        t = a.claim("x")                                      # 本地还剩 3 条
        for k in a._chunks.values():                          # 模拟续租线程卡死：租约过期
            k.keeper._stop.set()
        time.sleep(0.5)
        b = self.src("b")
        stolen = b.claim("y")                                 # b 回收并领到同一块
        self.assertIsNotNone(stolen)
        for k in a._chunks.values():
            k.keeper.lost.set()                               # a 的续租发现租约已丢
        a.finish(t, "success")                                # 在途的照常上报
        self.assertIsNone(a.claim("x"))                       # 本地剩余的放弃，没有别的块
        a._flush()
        self.assertEqual(self.queue().stats().tasks.get("success"), 1)

    def test_curl_downloaders_on_three_machines_share_one_batch(self):
        """3 个 Downloader（模拟 3 台机器）各 2 线程，共享一个 PG 批次：每条恰好处理一次。"""
        from tests.test_paperlite import FAKE_PDF, PROXY, FakeStore, Resp, ident
        from paperlite.config import Limits
        from paperlite.consumer import Downloader

        urls = [f"https://dl.acm.org/doi/pdf/10.1145/{i}" for i in range(30)]
        loader = self.src("loader")
        loader.import_urls({"url": u} for u in urls)
        hits, lock = {}, threading.Lock()

        def respond(url):
            with lock:
                hits[url] = hits.get(url, 0) + 1
            return Resp(404, b"<html>not found</html>") if url.endswith("/7") else Resp(200, FAKE_PDF)

        def fake_session(_ident):
            s = mock.Mock()
            s.get.side_effect = lambda url, **kw: respond(url)
            return s

        cfg = Limits(request_gap_s=(0, 0))
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(Downloader, "_session", side_effect=fake_session):
            dls = [Downloader(cfg, FakeStore([ident(exit_ip=f"9.9.9.{m}{i}") for i in range(2)]), PROXY,
                              self.src(f"m{m}"), Path(tmp), workers=2) for m in range(3)]
            ts = [threading.Thread(target=d.run) for d in dls]
            for t in ts:
                t.start()
            for t in ts:
                t.join(30)
            self.assertFalse(any(t.is_alive() for t in ts))
            for d in dls:
                d.db._flush()
            pdfs = list(Path(tmp).rglob("*.pdf"))
        self.assertEqual(sorted(hits), sorted(urls))
        self.assertEqual(set(hits.values()), {1})             # 没有一条被两台机器重复请求
        self.assertEqual(len(pdfs), 29)
        st = self.queue().stats()
        self.assertEqual(sum(st.tasks.values()), 30)
        self.assertEqual(st.tasks.get("success"), 29)
        self.assertEqual(st.chunks, {"done": 8})
        self.assertTrue(all(d.stats.success for d in dls))    # 三台都分到了活


    def test_browser_pools_on_two_machines_share_one_batch(self):
        """浏览器模式：2 个 BrowserPool（模拟 2 台机器）各 2 个浏览器；标题各不相同，巡检开着也不跳过任何任务。"""
        from tests.test_paperlite import PROXY
        from paperlite import browser_pool as bp
        from paperlite.browser import RenderResult
        from paperlite.config import Limits
        from paperlite.proxy import Lease

        urls = [f"https://www.sciencedirect.com/science/article/abs/pii/{i}" for i in range(20)]
        self.src("loader").import_urls({"url": u, "url_type": "html"} for u in urls)
        hits, lock = {}, threading.Lock()

        class FakeSession:
            def __init__(s, proxy, headed=False, geoip=False, webrtc_spoof=True):
                pass
            async def start(s):
                pass
            async def render(s, url, timeout_s, settle_s=2.0):
                with lock:
                    hits[url] = hits.get(url, 0) + 1
                return RenderResult(ok=True, seconds=0.1, url=url, status=200, title=f"P{url}", html="<html/>")
            async def close(s):
                pass

        n = iter(range(1000))

        class FakeIp:
            def __init__(s, proxy):
                pass
            def extract_checked(s):
                i = next(n)
                return Lease(server=f"1.2.{i}.1:1", exit_ip=f"1.2.{i}.1", area="", expires_at=time.time() + 9999)

        cfg = Limits(page_interval_s=(0, 0), pass_timeout_s=5, min_left_s=1, title_dup_trigger=3)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(bp, "BrowserSession", FakeSession), \
                mock.patch.object(bp, "IpSource", FakeIp):
            pools = [bp.BrowserPool(cfg, PROXY, self.src(f"m{m}"), Path(tmp), 2) for m in range(2)]
            self.assertTrue(all(p.inspector is not None for p in pools))
            ts = [threading.Thread(target=p.run) for p in pools]
            for t in ts:
                t.start()
            for t in ts:
                t.join(30)
            self.assertFalse(any(t.is_alive() for t in ts))
            for p in pools:
                p.db._flush()
            self.assertEqual(len(list(Path(tmp).rglob("*.html"))), 20)
        pages = {u: k for u, k in hits.items() if "/pii/" in u}            # 去掉热身落地页
        self.assertEqual(sorted(pages), sorted(urls))
        self.assertEqual(set(pages.values()), {1})
        st = self.queue().stats()
        self.assertEqual((st.tasks, st.chunks), ({"success": 20}, {"done": 5}))

    # ── 块内巡检 ────────────────────────────────────────────

    def test_inspector_hooks_only_touch_local_queue(self):
        a, b = self.src("a", chunk_size=5), self.src("b", chunk_size=5)
        a.import_urls({"doi": f"10.1/x{i:02d}", "url": f"https://h.org/pii/A{i:02d}"} for i in range(10))
        ta = a.claim("x")                                     # a 领第 1 块（x00..x04），本地剩 4 条
        tb = b.claim("y")                                     # b 领第 2 块
        self.assertEqual(a.count_pending_matching("10.1/x", ""), 4)
        self.assertEqual(a.flag_suspects("10.1/x", "https://h.org/pii/A", "Deleted", 10, 1.0), 4)
        a.finish(ta, "success")                               # 第 1 块：1 条成功 + 4 条跳过 → 收尾
        self.assertIsNone(a.claim("x"))                       # 第 2 块在 b 手里，a 没有可领的
        a._flush()
        q = self.queue()
        self.assertEqual(q.skip_counts(), {"title_dup": 4})  # b 手里的块不受影响
        rest = [tb.doi]
        b.finish(tb, "success")
        while (t := b.claim("y")) is not None:
            rest.append(t.doi)
            b.finish(t, "success")
        self.assertEqual(sorted(rest), [f"10.1/x{i:02d}" for i in range(5, 10)])
        rules = q.skip_rules()                                # 块内触发的规则也记下来，inspect --list 可查
        self.assertEqual([(r["doi_prefix"], r["scope"], r["flagged"]) for r in rules], [("10.1/x", "chunk", 4)])

    def test_browser_title_dup_skips_rest_in_chunk(self):
        """浏览器模式块内巡检：一段 URL 全是同一个"Deleted DOI"页，触发后本块里剩下的同前缀任务不再渲染。"""
        from tests.test_paperlite import PROXY
        from paperlite import browser_pool as bp
        from paperlite.browser import RenderResult
        from paperlite.config import Limits
        from paperlite.proxy import Lease

        base = "https://www.sciencedirect.com/science/article/pii/"
        rows = ([{"doi": f"10.1/a{i:03d}", "url": f"{base}A{i:03d}", "url_type": "html"} for i in range(30)] +
                [{"doi": f"10.1/b{i:03d}", "url": f"{base}B{i:03d}", "url_type": "html"} for i in range(10)])
        self.src("loader", chunk_size=100).import_urls(rows)
        rendered = []

        class FakeSession:
            def __init__(s, proxy, headed=False, geoip=False, webrtc_spoof=True):
                pass
            async def start(s):
                pass
            async def render(s, url, timeout_s, settle_s=2.0):
                if "/pii/" in url:
                    rendered.append(url)
                title = "Deleted DOI" if "/pii/A" in url else f"Paper {url[-3:]}"
                return RenderResult(ok=True, seconds=0.1, url=url, status=200, title=title, html="<html/>")
            async def close(s):
                pass

        class FakeIp:
            def __init__(s, proxy):
                pass
            def extract_checked(s):
                return Lease(server="1.2.3.4:1", exit_ip="1.2.3.4", area="", expires_at=time.time() + 9999)

        cfg = Limits(page_interval_s=(0, 0), pass_timeout_s=5, min_left_s=1, title_dup_trigger=3,
                     rule_min_support=3, rule_min_precision=0.9)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(bp, "BrowserSession", FakeSession), \
                mock.patch.object(bp, "IpSource", FakeIp):
            pool = bp.BrowserPool(cfg, PROXY, self.src("m", chunk_size=100), Path(tmp), 1)
            pool.run()
            pool.db._flush()
        a_seen = [u for u in rendered if "/pii/A" in u]
        self.assertEqual(sorted(u for u in rendered if "/pii/B" in u), [f"{base}B{i:03d}" for i in range(10)])
        self.assertLess(len(a_seen), 30)                      # 触发后跳过了一部分
        q = self.queue()
        self.assertEqual(q.skip_counts().get("title_dup", 0), 30 - len(a_seen))
        self.assertEqual(q.stats().tasks.get("success"), len(rendered))



class PgConfigTest(unittest.TestCase):
    """[pg] 段解析（不需要 PostgreSQL）。"""

    def test_from_dict(self):
        from paperlite.config import PgConfig
        c = PgConfig.from_dict({"dsn": "postgresql://h/db", "batch": "b", "chunk_size": 500})
        self.assertEqual((c.batch, c.chunk_size, c.lease_s), ("b", 500, 1800))
        for bad in ({"batch": "b"}, {"dsn": "x"}, {"dsn": "x", "batch": "b", "chunk_size": 0},
                    {"dsn": "x", "batch": "b", "nope": 1}):
            with self.assertRaises(ValueError):
                PgConfig.from_dict(bad)

    def test_load_pg_requires_section(self):
        from paperlite import config
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text('[pg]\ndsn = "postgresql://h/db"\nbatch = "b"\n')
            self.assertEqual(config.load_pg(p).batch, "b")
            p.write_text("[paths]\n")
            with self.assertRaisesRegex(ValueError, r"\[pg\]"):
                config.load_pg(p)


if __name__ == "__main__":
    unittest.main()
