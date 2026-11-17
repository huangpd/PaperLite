#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只给 DOI 的导入：拼 doi.org 链接、按族群排序、real_host 留空交给 DOI 映射回填。"""
import tempfile
import unittest
from pathlib import Path

from paperlite.cli import _import
from paperlite.tasks import TaskDB


class DoiImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = TaskDB(self.dir / "t.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _write(self, name, text):
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def _rows(self):
        return self.db._conn.execute("SELECT doi, url, url_type, real_host, source FROM paper_pdf_resources "
                                     "ORDER BY id").fetchall()

    def test_doi_csv_sorted_by_pattern(self):
        path = self._write("dois.csv", "﻿DOI,title\n"
                           "10.1016/j.cej.2021.2,a\n"
                           "10.1016/j.ajog.2020.1,b\n"
                           "https://doi.org/10.1016/J.CEJ.2021.1,c\n"
                           "not-a-doi,d\n"
                           "10.1016/j.ajog.2020.1,dup\n")
        with self.assertLogs("paperlite", "WARNING") as logs:
            _import(self.db, path, source="batch-x", url_type="html")
        self.assertIn("1 行不是 DOI", "".join(logs.output))
        rows = self._rows()
        self.assertEqual([r["doi"] for r in rows],      # 同族群连号，族群内按 DOI；重复只入一次
                         ["10.1016/j.ajog.2020.1", "10.1016/j.cej.2021.1", "10.1016/j.cej.2021.2"])
        self.assertEqual(rows[1]["url"], "https://doi.org/10.1016/j.cej.2021.1")
        self.assertTrue(all(r["real_host"] is None for r in rows))
        self.assertTrue(all(r["url_type"] == "html" and r["source"] == "batch-x" for r in rows))
        self.assertEqual(self.db.host(), "doi.org")

    def test_doi_lines_keep_commas_and_parens(self):
        path = self._write("dois.txt", "10.1016/s1472-6483(10)61454-x\n\n10.1002/(sici)1097-0258(19960229)15:4<351::aid-sim148>3.0.co;2-#\n"
                           "10.1000/a,b\n")
        _import(self.db, path)
        self.assertEqual(sorted(r["doi"] for r in self._rows()),
                         ["10.1000/a,b", "10.1002/(sici)1097-0258(19960229)15:4<351::aid-sim148>3.0.co;2-#",
                          "10.1016/s1472-6483(10)61454-x"])

    def test_doi_org_url_leaves_real_host_empty(self):
        path = self._write("urls.txt", "https://doi.org/10.1016/j.x.1\nhttps://www.sciencedirect.com/a/2\n")
        _import(self.db, path)
        self.assertEqual({r["url"]: r["real_host"] for r in self._rows()},
                         {"https://doi.org/10.1016/j.x.1": None,
                          "https://www.sciencedirect.com/a/2": "www.sciencedirect.com"})

    def test_backfill_maps_imported_doi(self):
        path = self._write("dois.csv", "doi\n10.1016/j.ajog.2020.1\n10.9999/unmapped.1\n")
        _import(self.db, path)
        self.db._conn.execute("CREATE TABLE doi_domain_routes (pattern_key TEXT PRIMARY KEY, real_host TEXT)")
        self.db._conn.execute("INSERT INTO doi_domain_routes VALUES ('10.1016/j.ajog', 'www.ajog.org')")
        self.db.backfill_real_hosts()
        self.assertEqual(self.db.pending_by_host(), {"www.ajog.org": 1, "doi.org": 1})

    def test_nothing_importable_fails_loudly(self):
        for name, text in (("empty.txt", ""), ("junk.txt", "hello\nworld\n"), ("junk.csv", "doi\nnope\n")):
            with self.subTest(name=name), self.assertRaises(SystemExit):
                _import(self.db, self._write(name, text))


if __name__ == "__main__":
    unittest.main()
