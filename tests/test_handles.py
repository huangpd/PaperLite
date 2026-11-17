#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paperlite.handles import handle_api_url, parse_handle, resolve_handles
from paperlite.tasks import Status, TaskDB, url_key


def _ok(url):
    return json.dumps({"responseCode": 1, "handle": "x", "values": [
        {"index": 1, "type": "URL", "data": {"format": "string", "value": url}}]})


NOT_FOUND = json.dumps({"responseCode": 100, "handle": "x"})


class ParseHandleTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_handle(200, _ok("https://linkinghub.elsevier.com/retrieve/pii/S1")),
                         ("ok", "https://linkinghub.elsevier.com/retrieve/pii/S1"))
        self.assertEqual(parse_handle(404, NOT_FOUND), ("not_found", None))
        self.assertEqual(parse_handle(200, json.dumps({"responseCode": 200, "values": []})), ("no_url", None))
        self.assertEqual(parse_handle(200, json.dumps({"responseCode": 1, "values": [
            {"type": "EMAIL", "data": {"value": "a@b"}}]})), ("no_url", None))
        # 暂时性失败一律 retry，绝不判失效
        self.assertEqual(parse_handle(429, ""), ("retry", None))
        self.assertEqual(parse_handle(503, NOT_FOUND), ("retry", None))
        self.assertEqual(parse_handle(404, "<html>gateway</html>"), ("retry", None))
        self.assertEqual(parse_handle(200, "not json"), ("retry", None))

    def test_api_url_encodes_doi(self):
        self.assertEqual(handle_api_url("10.1016/s1472-6483(10)61454-x"),
                         "https://doi.org/api/handles/10.1016/s1472-6483(10)61454-x?type=URL")
        u = handle_api_url("10.1002/a#b?c;d e")
        self.assertIn("10.1002/a%23b%3Fc%3Bd%20e?type=URL", u)


class ApplyHandlesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TaskDB(Path(self.tmp.name) / "t.db")
        self.db.import_urls([
            {"doi": "10.1016/j.a.1", "url": "https://doi.org/10.1016/j.a.1", "url_type": "html"},
            {"doi": "10.1016/j.a.2", "url": "https://doi.org/10.1016/j.a.2", "url_type": "html"},
            {"doi": "10.1016/j.a.3", "url": "https://doi.org/10.1016/j.a.3", "url_type": "html"},
            {"doi": "10.1016/j.a.4", "url": "https://doi.org/10.1016/j.a.4", "url_type": "html"},
            {"doi": "10.1016/j.a.5", "url": "https://www.sciencedirect.com/x/5", "url_type": "html"},
        ])
        # 模拟导入脚本：real_host 留空（DOI 映射回填前）
        self.db._conn.execute("UPDATE paper_pdf_resources SET real_host = NULL WHERE doi = '10.1016/j.a.2'")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _row(self, doi):
        return self.db._conn.execute("SELECT * FROM paper_pdf_resources WHERE doi = ?", (doi,)).fetchone()

    def test_unresolved_only_doi_org_pending(self):
        dois = [d for _, d in self.db.unresolved_doi_tasks(0, 100)]
        self.assertEqual(dois, ["10.1016/j.a.1", "10.1016/j.a.2", "10.1016/j.a.3", "10.1016/j.a.4"])
        self.db.save_handles([("10.1016/j.a.1", "ok", "https://linkinghub.elsevier.com/retrieve/pii/S1")])
        self.assertNotIn("10.1016/j.a.1", [d for _, d in self.db.unresolved_doi_tasks(0, 100)])

    def test_apply(self):
        new1 = "https://linkinghub.elsevier.com/retrieve/pii/S1"
        self.db.save_handles([
            ("10.1016/j.a.1", "ok", new1),
            ("10.1016/j.a.2", "ok", "https://linkinghub.elsevier.com/retrieve/pii/S2"),
            ("10.1016/j.a.3", "not_found", None),
            ("10.1016/j.a.4", "ok", new1),          # 与 a.1 同一落地 URL：url_key 冲突
            ("10.1016/j.a.5", "not_found", None),   # 已是出版商直链：不动
        ])
        r = self.db.apply_handles(batch=2)          # 小批量也能走完
        self.assertEqual(r, {"updated": 2, "invalid": 1, "conflict": 1})

        a1 = self._row("10.1016/j.a.1")
        self.assertEqual((a1["url"], a1["url_key"], a1["status"]), (new1, url_key(new1), Status.PENDING))
        self.assertIsNone(a1["real_host"])          # 原来是 doi.org，清空交给 backfill 按 DOI 映射
        self.assertIsNone(self._row("10.1016/j.a.2")["real_host"])

        a3 = self._row("10.1016/j.a.3")
        self.assertEqual((a3["status"], a3["error_kind"], a3["http_status"]), (Status.FAILED, "not_found", 404))
        self.assertIn("未注册", a3["error_message"])

        self.assertEqual(self._row("10.1016/j.a.4")["url"], "https://doi.org/10.1016/j.a.4")
        a5 = self._row("10.1016/j.a.5")
        self.assertEqual((a5["status"], a5["url"]), (Status.PENDING, "https://www.sciencedirect.com/x/5"))

        # 可重复执行：只剩冲突那条
        self.assertEqual(self.db.apply_handles(), {"updated": 0, "invalid": 0, "conflict": 1})

    def test_keeps_publisher_real_host(self):
        self.db._conn.execute("UPDATE paper_pdf_resources SET real_host = 'ajog.org' WHERE doi = '10.1016/j.a.1'")
        self.db.save_handles([("10.1016/j.a.1", "ok", "https://linkinghub.elsevier.com/retrieve/pii/S1")])
        self.db.apply_handles()
        self.assertEqual(self._row("10.1016/j.a.1")["real_host"], "ajog.org")


class _Resp:
    def __init__(self, code, text, headers=None):
        self.status_code, self.text, self.headers = code, text, headers or {}


class ResolveHandlesTest(unittest.TestCase):
    def test_resolve_retries_and_never_stores_transient(self):
        script = {
            "10.1/ok": [_Resp(200, _ok("https://pub.example/1"))],
            "10.1/limited": [_Resp(429, "", {"retry-after": "0"}), _Resp(200, _ok("https://pub.example/2"))],
            "10.1/gone": [_Resp(404, NOT_FOUND)],
            "10.1/down": [_Resp(503, "")] * 10,
        }
        calls = {}

        class FakeSession:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, **kw):
                doi = url.split("/api/handles/", 1)[1].split("?", 1)[0]
                n = calls.get(doi, 0)
                calls[doi] = n + 1
                seq = script[doi]
                return seq[min(n, len(seq) - 1)]

        with tempfile.TemporaryDirectory() as tmp:
            db = TaskDB(Path(tmp) / "t.db")
            db.import_urls([{"doi": d, "url": f"https://doi.org/{d}", "url_type": "html"} for d in script]
                           + [{"doi": "10.1/ok", "url": "https://doi.org/10.1/ok?dup", "url_type": "pdf"}])
            with mock.patch("curl_cffi.requests.AsyncSession", FakeSession), \
                    mock.patch("paperlite.handles._MAX_BACKOFF_S", 0):
                stats = asyncio.run(resolve_handles(db, concurrency=3, interval_s=0, max_attempts=3))

            self.assertEqual((stats.ok, stats.not_found, stats.no_url, stats.gave_up), (2, 1, 0, 1))
            self.assertEqual(calls["10.1/ok"], 1)           # 同一 DOI 两条任务只查一次
            self.assertEqual(calls["10.1/limited"], 2)
            self.assertEqual(calls["10.1/down"], 3)
            stored = dict(db._conn.execute("SELECT doi, status FROM doi_handles").fetchall())
            self.assertEqual(stored, {"10.1/ok": "ok", "10.1/limited": "ok", "10.1/gone": "not_found"})
            # 重试用尽的仍在待查列表，下次会重查
            self.assertEqual([d for _, d in db.unresolved_doi_tasks(0, 100)], ["10.1/down"])
            db.close()

    def test_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = TaskDB(Path(tmp) / "t.db")
            db.import_urls([{"doi": f"10.1/{i}", "url": f"https://doi.org/10.1/{i}"} for i in range(10)])

            class S:
                def __init__(self, *a, **kw):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

                async def get(self, url, **kw):
                    return _Resp(200, _ok("https://pub.example/x"))

            with mock.patch("curl_cffi.requests.AsyncSession", S):
                stats = asyncio.run(resolve_handles(db, concurrency=4, interval_s=0, limit=3))
            self.assertEqual(stats.done, 3)
            self.assertEqual(db._conn.execute("SELECT count(*) FROM doi_handles").fetchone()[0], 3)
            db.close()


if __name__ == "__main__":
    unittest.main()
