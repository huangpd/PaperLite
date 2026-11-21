#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 DuckDB 读 CSV（按 DOI 排序产出）：容错、报出错行；入库后同出版商/同期刊任务连号，字段语义与原来一致。"""
import csv
import io
import tempfile
import unittest
from pathlib import Path

from paperlite.cli import _import, _read_url_file
from tests.pgutil import execute, make_source, needs_pg


class ImportClusterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _csv(self, name, rows, bom=False):
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        p = self.dir / name
        p.write_text(("﻿" if bom else "") + buf.getvalue(), encoding="utf-8")
        return str(p)

    def test_sorted_by_doi(self):
        path = self._csv("mixed.csv", [
            ["doi", "url", "source", "url_type"],
            ["10.1016/j.cej.2021.2", "https://doi.org/10.1016/j.cej.2021.2", "s1", "html"],
            ["10.1145/100.1", "https://dl.acm.org/doi/pdf/10.1145/100.1", "s2", ""],
            ["10.1016/j.ajog.2020.1", "https://doi.org/10.1016/j.ajog.2020.1", "s3", ""],
            ["10.1002/adma.2023.1", "https://doi.org/10.1002/adma.2023.1", "", ""],
            ["10.1016/J.CEJ.2021.1", "https://doi.org/10.1016/j.cej.2021.1", "", ""],
            ["", "https://example.org/no-doi", "", ""],
            ["10.1016/j.ajog.2020.9", "", "", ""],                    # 没有 url：跳过
        ], bom=True)
        rows = list(_read_url_file(path))
        self.assertEqual([r["url"].rsplit("/", 1)[-1] for r in rows],
                         ["adma.2023.1",                              # 10.1002/adma
                          "j.ajog.2020.1",                            # 10.1016/j.ajog
                          "j.cej.2021.1", "j.cej.2021.2",             # 10.1016/j.cej，族群内按 DOI
                          "100.1",                                    # 10.1145
                          "no-doi"])                                  # 认不出 DOI 的排最后
        self.assertEqual(set(rows[0]), {"doi", "url", "source", "url_type"})   # 字段与 DictReader 一致
        self.assertEqual(rows[1]["source"], "s3")
        self.assertIsNone(rows[0]["source"])

    def test_no_interleaving_of_similar_codes(self):
        """期刊代码互为前缀（j.ajog / j.ajogmf）、DOI 前缀互为前缀（10.1016 / 10.10160）时不交错"""
        dois = ["10.1016/j.ajogmf.2020.1", "10.10160/x.1", "10.1016/j.ajog.2021.2", "10.1016/j.ajogmf.2019.9",
                "10.1016/j.ajog.2020.1", "10.10160/a.1"]
        path = self._csv("sim.csv", [["doi", "url"]] + [[d, "https://doi.org/" + d] for d in dois])
        self.assertEqual([r["doi"] for r in _read_url_file(path)],
                         ["10.1016/j.ajog.2020.1", "10.1016/j.ajog.2021.2",
                          "10.1016/j.ajogmf.2019.9", "10.1016/j.ajogmf.2020.1",
                          "10.10160/a.1", "10.10160/x.1"])

    def test_url_only_and_legacy_columns(self):
        path = self._csv("legacy.csv", [
            ["paper_uid", "pdf_url"],
            ["10.1145/2.1", "https://dl.acm.org/doi/pdf/10.1145/2.1"],
            ["10.1002/x.1", "https://onlinelibrary.wiley.com/doi/pdf/10.1002/x.1"],
        ])
        self.assertEqual([r["paper_uid"] for r in _read_url_file(path)], ["10.1002/x.1", "10.1145/2.1"])
        path = self._csv("urls.csv", [
            ["url"],
            ["https://dl.acm.org/doi/pdf/10.1145/9.9"],
            ["https://www.sciencedirect.com/science/article/pii/S1?doi=10.1016/j.cej.2021.5"],
            ["https://link.springer.com/content/pdf/10.1007/abc.pdf"],
        ])
        self.assertEqual([r["url"].split("/")[2] for r in _read_url_file(path)],
                         ["link.springer.com", "www.sciencedirect.com", "dl.acm.org"])

    def test_quoted_commas_and_import_end_to_end(self):
        path = self._csv("q.csv", [
            ["doi", "url", "title"],
            ["10.1016/j.b.1", "https://doi.org/10.1016/j.b.1", "a, b \"c\""],
            ["10.1016/j.a.1", "https://doi.org/10.1016/j.a.1", "x"],
        ])
        rows = list(_read_url_file(path))
        self.assertEqual(rows[1]["title"], 'a, b "c"')
        self._import_end_to_end(path)

    @needs_pg
    def _import_end_to_end(self, path):
        db = make_source(self)
        _import(db, path, source="batch", url_type="html")
        got = execute(db, "SELECT doi, url_type, source FROM pl_tasks ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in got], [("10.1016/j.a.1", "html", "batch"), ("10.1016/j.b.1", "html", "batch")])

    def test_txt_unchanged(self):
        p = self.dir / "list.txt"
        p.write_text("https://b.example/2\nnot a link\nhttps://a.example/1\n", encoding="utf-8")
        self.assertEqual([r["url"] for r in _read_url_file(str(p))],     # 纯文本保持文件原顺序
                         ["https://b.example/2", "https://a.example/1"])

    def test_broken_csv_fails_loudly(self):
        p = self.dir / "bad.csv"
        p.write_text('doi,url\n10.1/a,"https://x.example/a\n', encoding="utf-8")   # 引号未闭合
        with self.assertRaises(SystemExit):
            list(_read_url_file(str(p)))


if __name__ == "__main__":
    unittest.main()
