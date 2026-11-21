"""配置加载与校验（config.py）。"""
import os
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paperlite.config import Limits, RunConfig, proxy_settings
from paperlite.curl.downloader import impersonate_for
from paperlite.logutil import JsonFormatter
from tests.fakes import PROXY
from paperlite.proxy import Lease, parse_deadline




class ConfigTest(unittest.TestCase):
    def test_from_dict(self):
        cfg = Limits.from_dict({"request_gap_s": [1, 2]})
        self.assertEqual(cfg.request_gap_s, (1.0, 2.0))

    def test_rejects_unknown_and_bad_gap(self):
        with self.assertRaises(ValueError):
            Limits.from_dict({"max_browser": 3})
        with self.assertRaises(ValueError):
            Limits.from_dict({"request_gap_s": [5, 3]})

    def test_page_interval_parsed_and_validated(self):
        self.assertEqual(Limits().page_interval_s, (10.0, 30.0))
        self.assertEqual(Limits.from_dict({"page_interval_s": [15, 40]}).page_interval_s, (15.0, 40.0))
        with self.assertRaises(ValueError):
            Limits.from_dict({"page_interval_s": [30, 10]})

    def test_title_window_must_cover_trigger(self):
        self.assertEqual(Limits().title_window_size, 500)
        with self.assertRaises(ValueError):
            Limits.from_dict({"title_dup_trigger": 10, "title_window_size": 9})

    def test_default_run_config(self):
        run = RunConfig.from_dict({"command": "run", "mode": "browser", "proxy": False,
                                   "workers": 1, "urls": "x.csv", "url_type": "html"})
        self.assertEqual((run.command, run.mode, run.proxy, run.workers), ("run", "browser", False, 1))
        with self.assertRaises(ValueError):
            RunConfig.from_dict({"command": "run", "mode": "curl", "proxy": False})

    def test_configured_run_argument_mapping(self):
        import argparse
        from paperlite.cli import _configured_run_args
        base = argparse.Namespace(config="x.toml", log_level="INFO")
        args = _configured_run_args(base, RunConfig(command="run", mode="browser", proxy=False,
                                                     workers=2, urls="x.csv", url_type="html"))
        self.assertEqual((args.cmd, args.browser, args.no_proxy, args.workers, args.urls, args.type),
                         ("run", True, True, 2, "x.csv", "html"))

    def test_json_log_formatter(self):
        record = logging.LogRecord("paperlite.test", logging.INFO, __file__, 1,
                                   "页面保存成功", (), None)
        record.event = "page_saved"
        record.doi = "10.1/example"
        record.seconds = 3.2
        data = json.loads(JsonFormatter().format(record))
        self.assertEqual((data["event"], data["doi"], data["seconds"]),
                         ("page_saved", "10.1/example", 3.2))
        self.assertNotIn("bytes", data)

    def test_console_formatter_human_readable(self):
        from paperlite.logutil import ConsoleFormatter
        record = logging.LogRecord("paperlite", logging.INFO, __file__, 1,
                                   "待下载 100", (), None)
        record.event = "progress"           # 内建+隐藏字段不进文本
        record.pdf = 140
        line = ConsoleFormatter(color=False).format(record)
        self.assertNotIn("{", line)         # 不是 JSON
        self.assertIn("INFO", line)
        self.assertIn("待下载 100", line)
        self.assertIn("pdf=140", line)      # 业务字段附在后面
        self.assertNotIn("event=", line)    # progress 这类隐藏字段不打
        self.assertFalse(line.startswith("\033"))   # 无色时不含 ANSI

    def test_console_formatter_colors_only_when_enabled(self):
        from paperlite.logutil import ConsoleFormatter
        rec = logging.LogRecord("paperlite", logging.ERROR, __file__, 1, "过盾超时", (), None)
        self.assertTrue(ConsoleFormatter(color=True).format(rec).startswith("\033["))
        self.assertFalse(ConsoleFormatter(color=False).format(rec).startswith("\033["))

    def test_log_format_selection(self):
        import io
        from paperlite.logutil import _console_formatter, ConsoleFormatter, JsonFormatter
        nontty = io.StringIO()                      # isatty() -> False
        # auto：非 TTY（重定向/管道/采集）给 JSON，便于机器解析
        self.assertIsInstance(_console_formatter("auto", nontty), JsonFormatter)
        # 显式覆盖
        self.assertIsInstance(_console_formatter("json", nontty), JsonFormatter)
        self.assertIsInstance(_console_formatter("text", nontty), ConsoleFormatter)

    def test_proxy_settings(self):
        with self.assertRaises(ValueError):
            proxy_settings({"username": "u"})            # 缺 api_url
        with self.assertRaises(ValueError):
            proxy_settings({"api_url": "x", "proxy_url": "y"})
        self.assertEqual(PROXY.url_for("1.2.3.4:80"), "http://u:p%40ss@1.2.3.4:80")
        self.assertEqual(Lease("1.2.3.4:80", "1.2.3.4", "", 0).browser_proxy(PROXY),
                         {"server": "http://1.2.3.4:80", "username": "u", "password": "p@ss"})

    def test_load_file(self):
        from paperlite.config import load
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[proxy]\napi_url = "https://a/"\n[limits]\nmax_browsers = 3\n')
        try:
            cfg, proxy = load(Path(f.name))
            self.assertEqual((cfg.max_browsers, proxy.api_url), (3, "https://a/"))
            with open(f.name, "a") as g:
                g.write('[landing]\n"h.org" = "https://h.org/x"\n')
            with self.assertRaisesRegex(ValueError, r"\[landing\] 已移除"):      # 不再有固定落地页
                load(Path(f.name))
        finally:
            os.unlink(f.name)

    def test_config_lookup_order(self):
        from paperlite.config import CONFIG_ENV, resolve_path
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CONFIG_ENV, None)
            self.assertEqual(resolve_path(), Path("config.toml"))            # 默认当前目录
            os.environ[CONFIG_ENV] = "/etc/pl.toml"
            self.assertEqual(resolve_path(), Path("/etc/pl.toml"))           # 环境变量
            self.assertEqual(resolve_path("x.toml"), Path("x.toml"))         # --config 优先
            os.environ.pop(CONFIG_ENV)

    def test_parse_deadline_and_impersonate(self):
        self.assertIsNotNone(parse_deadline("2026-10-24  20:52:15"))     # 接口偶有双空格
        self.assertIsNone(parse_deadline(""))
        self.assertTrue(impersonate_for("Mozilla/5.0 Chrome/146.0.0.0").startswith("chrome"))

    def test_parse_extract_json_and_text(self):
        from paperlite.proxy import parse_extract
        j = parse_extract('{"code": "SUCCESS", "data": [{"server": "1.2.3.4:8000", "proxy_ip": "1.2.3.4"}]}')
        self.assertEqual((j["server"], j["proxy_ip"]), ("1.2.3.4:8000", "1.2.3.4"))
        self.assertEqual(parse_extract("140.224.134.247:48747"), {"server": "140.224.134.247:48747"})
        self.assertEqual(parse_extract("1.2.3.4:80\r\n5.6.7.8:81\r\n")["server"], "1.2.3.4:80")   # 多行取第一个
        for bad in ('{"code": 1, "msg": "余额不足"}', "请求过于频繁", "", '{"code": "SUCCESS", "data": []}'):
            with self.assertRaises(RuntimeError):
                parse_extract(bad)
