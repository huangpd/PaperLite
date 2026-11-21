"""命令行（cli.py）：子命令、参数、地理库检查。"""
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from paperlite.tasks import Status
from tests.pgutil import make_source, needs_pg, new_schema, pg_config




class CliTest(unittest.TestCase):
    """命令行：配置文件里的 [pg] 就是任务队列；没有 [pg] 报错。"""

    def setUp(self):
        # 在空的临时目录里跑：不读项目根目录的 config.toml
        self._cwd = os.getcwd()
        self._tmp_cwd = tempfile.TemporaryDirectory()
        os.chdir(self._tmp_cwd.name)
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        os.environ.pop("PAPERLITE_CONFIG", None)

    def tearDown(self):
        self._env.stop()
        os.chdir(self._cwd)
        self._tmp_cwd.cleanup()

    def run_cli(self, *argv):
        from paperlite import cli
        with mock.patch("sys.argv", ["paperlite", *argv]):
            cli.main()

    def test_no_command_prints_help(self):
        import contextlib, io
        for argv in ((), ("help",), ("help", "run")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.run_cli(*argv)                             # 不抛 SystemExit
            self.assertIn("用法: paperlite", buf.getvalue())

    def test_init_writes_template(self):
        self.run_cli("init")
        cfg = Path("config.toml")
        self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)
        with open(cfg, "rb") as f:
            raw = tomllib.load(f)
        self.assertEqual(set(raw), {"pg", "proxy", "limits", "paths", "run", "monitor"})
        self.assertEqual(set(raw["pg"]), {"dsn", "batch"})
        self.assertFalse(raw["monitor"]["enabled"])               # 模板默认不上报
        with self.assertRaises(SystemExit):                     # 已存在不覆盖
            self.run_cli("init")
        self.run_cli("init", "--force")

    def test_missing_pg_section_fails(self):
        Path("config.toml").write_text("[paths]\n")
        urls = Path("u.txt")
        urls.write_text("https://dl.acm.org/doi/pdf/10.1145/1.2\n")
        with self.assertRaises(SystemExit) as e:
            self.run_cli("import", "--urls", str(urls))
        self.assertIn("[pg]", str(e.exception))

    def test_removed_legacy_settings_explained(self):
        from paperlite import config
        for text, needle in (('[paths]\ndb = "t.db"\n', "[paths] db"), ("[queue]\nbackend = 'pg'\n", "[queue]"),
                             ("[run]\npg = true\n", "[run] pg"), ("[limits]\nvolume_stop_after = 3\n", "volume_stop_after"),
                             ('[landing]\n"h.org" = "https://h.org/"\n', "[landing]")):
            Path("c.toml").write_text(text)
            with self.assertRaises(ValueError) as e:
                config.load_limits(Path("c.toml")); config.load_paths(Path("c.toml")); config.load_run(Path("c.toml"))
            self.assertIn(needle, str(e.exception))

    @needs_pg
    def test_import_retry_export_status_via_pg(self):
        schema = new_schema(self)
        Path("config.toml").write_text(pg_config(schema, batch="cli"))
        f = Path("mixed.txt")
        f.write_text("https://dl.acm.org/doi/pdf/10.1145/1.2\nhttps://example.org/a.pdf\nhttp://192.168.1.111/x\n")
        self.run_cli("import", "--urls", str(f))
        bad = Path("mixed.txt.bad_url.csv").read_text().splitlines()
        self.assertEqual(bad, ["doi,url,reason", ",http://192.168.1.111/x,private_ip"])   # 文本文件没有 doi 列
        db = make_source(self, batch="cli", schema=schema)
        self.assertEqual(sorted(db.hosts()), ["dl.acm.org", "example.org"])   # 多主机同一批次
        self.assertEqual(db.counts()[Status.PENDING], 2)
        t = db.claim("w")
        db.finish(t, Status.FAILED, error_kind="network")
        db.close()
        self.run_cli("retry", "--status", "failed", "--kind", "network")
        csv_path = Path("x.csv")
        self.run_cli("export", "--file", str(csv_path))
        lines = csv_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("id,doi,url"))
        self.assertNotIn("failed", "".join(lines[1:]))                       # 已放回 pending
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.run_cli("status")
        self.assertIn("pending 2", buf.getvalue())
        self.assertIn("块:", buf.getvalue())


class CliArgsTest(unittest.TestCase):
    def setUp(self):
        import contextlib
        import io
        from paperlite.cli import _build_parser
        self.ap, self.parsers = _build_parser()
        self._contextlib, self._io = contextlib, io

    def parse(self, *argv):
        return self.ap.parse_args(list(argv))

    def fails(self, *argv):
        """argparse 出错会 print_usage 到 stderr 再 sys.exit —— 吞掉输出，断言退出。"""
        buf = self._io.StringIO()
        with self.assertRaises(SystemExit), self._contextlib.redirect_stderr(buf):
            self.ap.parse_args(list(argv))

    # 文档 命令表列出的子命令（help 是帮助本身）
    def test_command_set_matches_readme(self):
        from paperlite.cli import _COMMANDS
        self.assertEqual(
            set(_COMMANDS),
            {"init", "import", "run", "produce", "download", "status",
             "inspect", "retry", "export", "turnstile", "help"},
        )
        # 每个命令都建了子解析器（help 自身也在内）
        self.assertEqual(set(self.parsers), set(_COMMANDS))

    # 文档 快速开始/脚注：--version、--config、--log-level 默认 INFO
    def test_global_flags(self):
        self.assertEqual(self.parse("status").log_level, "INFO")
        self.assertIsNone(self.parse("status").config)
        self.fails("--version")          # version action 直接退出
        self.fails("不是命令")            # 未知命令报错退出

    # 任务队列只在 [pg]：--db / --pg 已移除；没有 --out/--host 了
    def test_no_db_or_pg_flags(self):
        for name in ("import", "run", "produce", "download", "status", "inspect", "retry", "export"):
            argv = [name] + (["--urls", "x.txt"] if name == "import" else
                             ["--status", "failed"] if name == "retry" else [])
            self.parse(*argv)
            self.fails(*argv, "--db", "d.db")
            self.fails(*argv, "--pg")
        self.assertEqual(self.parse("run", "--storage", "/mnt/x").storage, "/mnt/x")   # run/download 另有 --storage
        self.fails("status", "--out", "o")        # --out 已移除
        self.fails("import", "--host", "h")       # --host 已移除

    # 文档 import 行：--urls 必填；--type 为 pdf|html，不写时取配置 [run] url_type（没有则 pdf）
    def test_import(self):
        from paperlite.cli import _url_type
        import argparse
        self.fails("import")                                   # 缺 --urls
        ns = self.parse("import", "--urls", "acm.txt")
        self.assertIsNone(ns.type)
        self.assertEqual(self.parse("import", "--urls", "x", "--type", "html").type, "html")
        self.fails("import", "--urls", "x", "--type", "xml")   # 非法 choice
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.toml"
            ns = lambda t=None: argparse.Namespace(config=str(cfg), type=t)
            self.assertEqual(_url_type(ns()), "pdf")           # 没有配置文件
            cfg.write_text('[run]\nurl_type = "html"\n')
            self.assertEqual(_url_type(ns()), "html")          # 跟配置走：以前这里固定是 pdf，整批类型记错
            self.assertEqual(_url_type(ns("pdf")), "pdf")      # 命令行优先

    # 文档 run 行：--workers 默认 20；--curl/--browser 互斥；--no-proxy；--type
    def test_run(self):
        ns = self.parse("run")
        self.assertEqual(ns.workers, 20)
        self.assertFalse(ns.browser or ns.curl or ns.no_proxy)
        self.assertIsNone(ns.type)                             # 不写时取配置 [run] url_type
        self.assertIsNone(ns.config_file)                      # 裸 run：无配置文件，用 flag
        self.assertTrue(self.parse("run", "--browser", "--no-proxy").no_proxy)
        self.fails("run", "--browser", "--curl")               # 互斥

    # 文档「配置文件启动」：paperlite run config.toml 按 [run] 启动
    def test_run_config_file_positional(self):
        self.assertEqual(self.parse("run", "config.toml").config_file, "config.toml")

    # 默认模式 browser：run 子命令不带标志、[run] 省略 mode 都走浏览器
    def test_default_mode_is_browser(self):
        from paperlite.config import RunConfig
        from paperlite.cli import _configured_run_args
        import argparse
        self.assertEqual(RunConfig().mode, "browser")
        ns = self.parse("run")
        self.assertFalse(ns.curl or ns.browser)               # 无标志；分发按 "not --curl" → 浏览器分支
        mapped = _configured_run_args(argparse.Namespace(config=None, log_level="INFO"),
                                      RunConfig(command="run"))
        self.assertTrue(mapped.browser and not mapped.curl)   # [run] 省略 mode → browser

    # 文档：produce 常驻，按下载线程与可领取任务生产；--keep 已删除
    def test_produce(self):
        self.parse("produce")
        self.fails("produce", "--keep", "20")

    # 文档：download 需 --workers（默认 20），可选 --urls
    def test_download(self):
        self.assertEqual(self.parse("download").workers, 20)
        self.assertEqual(self.parse("download", "--workers", "8").workers, 8)

    # 文档 retry：--status 必填，只接受 failed/non_pdf；--kind 配合；--stopped-volumes 随卷期停止移除
    def test_retry(self):
        from paperlite.cli import ERROR_KINDS
        self.fails("retry")                                    # 缺 --status
        self.assertEqual(self.parse("retry", "--status", "failed").status, "failed")
        self.assertEqual(self.parse("retry", "--status", "non_pdf").status, "non_pdf")
        self.fails("retry", "--stopped-volumes")
        self.fails("retry", "--status", "pending")             # 非法 choice
        self.assertIn("network", ERROR_KINDS)
        self.assertEqual(self.parse("retry", "--status", "failed", "--kind", "network").kind, "network")

    # 文档 export：--status 为 pending / success / failed / non_pdf 之一；--file 默认标准输出（-）
    def test_export(self):
        from paperlite.tasks import Status
        self.assertEqual(self.parse("export").file, "-")
        for s in (Status.PENDING, Status.SUCCESS, Status.FAILED, Status.NON_PDF):
            self.assertEqual(self.parse("export", "--status", s).status, s)
        self.fails("export", "--status", "running")            # running 只是进程内计数，不落库
        self.fails("export", "--status", "bogus")

    # 文档 inspect 行：--trigger/--min-precision/--min-support/--list/--reset
    def test_inspect(self):
        ns = self.parse("inspect", "--trigger", "10", "--min-precision", "0.9",
                        "--min-support", "5", "--list", "--reset")
        self.assertEqual((ns.trigger, ns.min_precision, ns.min_support), (10, 0.9, 5))
        self.assertTrue(ns.list and ns.reset)

    # 文档 Turnstile 节：--url/--sitekey、--serve 起服务、--json、默认值
    def test_turnstile(self):
        ns = self.parse("turnstile", "--url", "https://x/", "--sitekey", "0x4A")
        self.assertEqual((ns.timeout, ns.concurrency, ns.bind), (60, 4, "127.0.0.1"))
        self.assertFalse(ns.json)
        self.assertEqual(self.parse("turnstile", "--serve", "8191").serve, 8191)
        self.assertTrue(self.parse("turnstile", "--url", "x", "--sitekey", "k", "--json").json)

    # 文档：init --force
    def test_init(self):
        self.assertFalse(self.parse("init").force)
        self.assertTrue(self.parse("init", "--force").force)


# ──────────────────────────────────────────────────────────────────────────
# 代理分层体检：经代理探判定目标（curl 低层，拿 TCP/TLS/TTFB/总耗时四段），要求
# 连通+TLS+2xx → 回显出口 IP == proxy_ip → 延迟不超阈值；两个目标任一通过即合格。
# 用 mock _probe 测决策逻辑，不走真实网络。
# ──────────────────────────────────────────────────────────────────────────


class GeoipGateTest(unittest.TestCase):
    """geoip=true 但 GeoIP 库缺失时，启动前报错而非卡死下载。"""

    def test_require_geoip(self):
        from paperlite.cli import _require_geoip
        from paperlite.config import Limits
        _require_geoip(Limits(geoip=False))                                  # 关：不检查、不报错
        with mock.patch("paperlite.browser.session.ensure_geoip_db", return_value=(True, "/x/db.mmdb", "u")):
            _require_geoip(Limits(geoip=True))                               # 就位（或下成功）：通过
        with mock.patch("paperlite.browser.session.ensure_geoip_db",
                        return_value=(False, "/x/GeoLite2-City.mmdb", "https://example/db")):
            with self.assertRaises(SystemExit) as e:
                _require_geoip(Limits(geoip=True))                           # 下不动：报错
            msg = str(e.exception)
            self.assertIn("GeoLite2-City.mmdb", msg)        # 指明文件
            self.assertIn("https://example/db", msg)        # 给出下载地址
            self.assertIn("geoip = false", msg)             # 给出关闭选项
