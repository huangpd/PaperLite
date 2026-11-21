#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PaperLite 命令行。一份 config = 一个批次：各种主机的 URL 混着导进 PostgreSQL 任务队列（[pg]），
一台或多台机器共享，默认用**浏览器模式**统一跑。输出目录在 [paths].storage（可挂载）。

  paperlite init                                    # 在当前目录生成 config.toml 模板
  paperlite import   --urls mixed.csv               # 导入任务（txt 一行一个，或带 url 列的 CSV），多主机随便混
  paperlite import   --urls pages.txt --type html   # 要网页本身（存 .html），数据库 url_type = html
  paperlite run config.toml                         # 按 [run] 启动（默认浏览器模式），下载完退出
  paperlite run      --workers 20                   # 命令行临时跑（默认浏览器；--curl 走单主机 cookie 池）
  paperlite status                                  # 任务统计（+ curl 模式下的身份池）
  paperlite retry    --status failed                # 把失败的重置为 pending
  paperlite export   --status non_pdf > non_pdf.csv
  paperlite turnstile --url <页面> --sitekey <key>  # 取 Turnstile token；--serve 8191 起 HTTP 服务

任务队列 = [pg]（dsn + batch）；输出目录 = --storage > [paths].storage，可放 goofys/s3fs 等对象存储挂载。

配置查找顺序：--config > 环境变量 PAPERLITE_CONFIG > 当前目录的 config.toml（paperlite init 生成）。
也可以 python -m paperlite ...。
"""

import argparse
import asyncio
import csv
import dataclasses
import json
import logging
import os
import signal
import socket
import sys
from datetime import datetime
from pathlib import Path

from paperlite import __version__
from paperlite import config as config_mod
from paperlite.config import Limits, Paths, RunConfig
from paperlite.browser.runner import run_browser
from paperlite.browser.session import require_geoip
from paperlite.config import ProxySettings
from paperlite.curl.identity import IdentityStore
from paperlite.curl.runner import run_curl, run_producer
from paperlite.errors import UserError
from paperlite import logutil
from paperlite.logutil import setup_logging
from paperlite.taskq import PgTaskQueue, PgTaskSource, importer
from paperlite.rules.tasks import ERROR_KINDS, Status, UrlType

logger = logging.getLogger("paperlite")


def _configured_run_args(args, run: RunConfig, config: str | None = None):
    """把 [run] 转成与 ``paperlite run ...`` 相同的 argparse 参数（storage 仍从 [paths] 取）。"""
    return argparse.Namespace(
        cmd="run", config=config if config is not None else args.config,
        log_level=args.log_level, log_format=getattr(args, "log_format", "auto"),
        storage=None,
        urls=run.urls or None, type=run.url_type, workers=run.workers,
        curl=run.mode == "curl", browser=run.mode == "browser",
        no_proxy=not run.proxy, headed=run.headed,
    )


def _load(path: str | None) -> tuple[Limits, ProxySettings]:
    try:
        return config_mod.load(config_mod.resolve_path(path))
    except FileNotFoundError as e:
        raise SystemExit(str(e))
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")


def _limits(args) -> Limits:
    """只读 [limits]；没有 [limits] 段时用默认值（import / status / retry / export 不必有代理配置）。"""
    try:
        return config_mod.load_limits(config_mod.resolve_path(args.config))
    except FileNotFoundError:
        return Limits()
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")


def _paths(args) -> Paths:
    """[paths]：storage / log_dir；没有配置文件时用默认值。"""
    try:
        return config_mod.load_paths(config_mod.resolve_path(args.config))
    except FileNotFoundError:
        return Paths()
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")


def _storage_dir(args, paths: Paths) -> Path:
    return Path(args.storage).expanduser() if getattr(args, "storage", None) else paths.storage_path


def _open_db(args) -> PgTaskSource:
    """打开任务队列：[pg] 段的 PostgreSQL（dsn + batch）。"""
    try:
        pc = config_mod.load_pg(config_mod.resolve_path(args.config))
    except FileNotFoundError as e:
        raise SystemExit(str(e))
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")
    try:
        q = PgTaskQueue(pc.dsn, pc.batch, schema=pc.schema or None,
                        worker=f"{socket.gethostname()}-{os.getpid()}")
    except Exception as e:
        raise SystemExit(f"连接 PostgreSQL 失败: {e}")
    return PgTaskSource(q, chunk_size=pc.chunk_size, lease_s=pc.lease_s, flush_n=pc.flush_n,
                        flush_s=pc.flush_s, retry_delay_s=pc.retry_delay_s)


def _open_store(args) -> IdentityStore:
    """curl 模式的身份池：和任务队列同一个 PostgreSQL（[pg] 的 dsn、schema）。"""
    try:
        pc = config_mod.load_pg(config_mod.resolve_path(args.config))
    except FileNotFoundError as e:
        raise SystemExit(str(e))
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")
    try:
        return IdentityStore(pc.dsn, schema=pc.schema or None)
    except Exception as e:
        raise SystemExit(f"连接 PostgreSQL 失败: {e}")


def _url_type(args) -> str:
    """导入的默认链接类型：命令行 --type > 配置文件 [run] url_type > pdf。
    以前 import 不写 --type 就固定是 pdf，和配置里写的 url_type = "html" 对不上，整批类型会记错。"""
    if getattr(args, "type", None):
        return args.type
    try:
        return config_mod.load_run(config_mod.resolve_path(args.config)).url_type
    except FileNotFoundError:
        return UrlType.PDF
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")


def _attach_monitor(args):
    """返回给 runner 的挂监控函数：[monitor] enabled 时启用上报 + 心跳，未配置返回 None。批次名 = 配置文件名（不含扩展名）。"""
    from paperlite.monitor import reporter as mon
    path = config_mod.resolve_path(args.config)

    def attach(snapshot):
        try:
            return mon.attach(path, path.stem, snapshot)
        except (TypeError, ValueError) as e:
            raise UserError(f"配置错误: {e}") from e
    return attach


def _open_for_run(args) -> tuple[PgTaskSource, Path]:
    """run / download 前的准备：打开任务队列、导入 --urls（有的话）、定输出目录。之后 db 交给 runner 关闭。"""
    db = _open_db(args)
    try:
        if args.urls:
            importer.import_file(db, args.urls, url_type=_url_type(args))
        return db, _storage_dir(args, _paths(args))
    except BaseException:
        db.close()
        raise


def _browse(args, cfg, proxy, no_proxy: bool = False) -> None:
    db, out = _open_for_run(args)
    run_browser(cfg, proxy, db, out, args.workers, no_proxy=no_proxy, attach_monitor=_attach_monitor(args))


def _download(args, cfg, proxy, store, with_producer: bool) -> None:
    db, out = _open_for_run(args)
    run_curl(cfg, proxy, store, db, out, args.workers, with_producer=with_producer,
             attach_monitor=_attach_monitor(args))


def _status(args, cfg) -> None:
    db = _open_db(args)
    q = db.q
    host = db.host()
    c = db.counts()                                 # pending 不含巡检跳过的
    sk = q.skip_counts()
    print(f"任务队列 {db.path}（{host or '空'}）: 共 {sum(c.values()) + sum(sk.values())}  " +
          "  ".join(f"{s} {c[s]}" for s in Status.ALL))
    types = db.type_counts()
    if len(types) > 1 or UrlType.PDF not in types:
        for t, tc in sorted(types.items()):
            print(f"  [{t}] 共 {sum(tc.values())}  " + "  ".join(f"{s} {tc[s]}" for s in Status.ALL if tc[s]))
    ch = q.stats().chunks
    print("  块: " + "  ".join(f"{k} {ch.get(k, 0)}" for k in ("queued", "leased", "done")))
    if sk:
        print(f"  巡检跳过（仍 pending、不再领取）: {sk.get('title_dup', 0)}（inspect --reset 可放回）")
    for status, kind, n in db.kind_counts():
        print(f"  {status:8s} {kind or '-':16s} {n}")
    db.close()
    if not host:
        return
    try:
        pc = config_mod.load_pg(config_mod.resolve_path(args.config))
        store = IdentityStore(pc.dsn, schema=pc.schema or None)
        store.reap(host)
        st = store.stats(host, cfg.refill_before_s)
    except Exception as e:
        print(f"身份池: 读取失败（{e}）")
        return
    if not (st.ready or st.inuse or st.workers):          # 浏览器模式没有身份池，不打空行
        store.close()
        return
    print(f"身份池 {host}: 空闲 {st.ready}  占用 {st.inuse}  健康 {st.healthy}  在线下载线程 {st.workers}  "
          f"生产者 {store.producer_holder(host) or '无'}")
    for ident in store.list(host):
        print(f"  {ident.exit_ip:15s} 剩余 {ident.left():4.0f}s  过盾 {ident.pass_s:.0f}s  "
              f"创建 {datetime.fromtimestamp(ident.created_at):%H:%M:%S}  指纹 {ident.fingerprint}")
    store.close()


def _export(args) -> None:
    db = _open_db(args)
    out = open(args.file, "w", newline="", encoding="utf-8") if args.file != "-" else sys.stdout
    n = 0
    try:
        w = csv.writer(out)
        w.writerow(db.q.EXPORT_COLUMNS)
        for r in db.q.rows(args.status):
            w.writerow(r)
            n += 1
    finally:
        if out is not sys.stdout:
            out.close()
        db.close()
    if out is not sys.stdout:
        print(f"导出 {n} 行到 {args.file}")


def _turnstile(args, parser) -> None:
    from paperlite.browser import turnstile

    try:
        proxy = turnstile.parse_proxy(args.proxy)
    except ValueError as e:
        parser.error(str(e))
    if args.serve:
        try:
            turnstile.serve(args.bind, args.serve, args.concurrency, args.headed, args.proxy)
        except KeyboardInterrupt:
            logger.info("Turnstile 服务已停止")
        return
    if not (args.url and args.sitekey):
        parser.error("缺少 --url / --sitekey（或用 --serve 起服务）")
    r = asyncio.run(turnstile.solve_turnstile(args.url, args.sitekey, action=args.action, cdata=args.cdata,
                                              proxy=proxy, timeout_s=args.timeout, headed=args.headed))
    if args.json:
        print(json.dumps({"ok": r.ok, "token": r.token, "user_agent": r.user_agent, "seconds": r.seconds,
                          "clicks": r.clicks, "error": r.error}, ensure_ascii=False))
    elif r.ok:
        print(r.token)
    if not r.ok:
        sys.exit(f"失败: {r.error}")


def _inspect(args) -> None:
    """巡检（全批次）：同标题重复到阈值就归纳 doi/url 规则，命中的未采任务记跳过、不再领取。
    下载时浏览器模式另有块内实时巡检（PgTaskSource），规则同样记入 pl_skip_rules。"""
    cfg = _limits(args)
    db = _open_db(args)
    q = db.q
    try:
        if args.reset:
            print(f"已放回 {q.clear_skips('title_dup')} 个巡检跳过的任务，清除全部规则")
            return
        if args.list:
            rules = q.skip_rules()
            if not rules:
                print("还没有生效的规则")
            for r in rules:
                print(f"  doi~{r['doi_prefix']!r} url~{r['url_prefix']!r} | 标题 {(r['title'] or '')[:40]!r} | "
                      f"precision {r['precision']} 已采命中 {r['support']} 跳过 {r['flagged']} | "
                      f"{'全批次' if r['scope'] == 'batch' else '下载时块内'} {r['created_at']:%Y-%m-%d %H:%M}")
            print(f"巡检跳过（仍 pending、不再领取）: {q.skip_counts().get('title_dup', 0)}")
            return
        trigger = args.trigger if args.trigger is not None else cfg.title_dup_trigger
        mp = args.min_precision if args.min_precision is not None else cfg.rule_min_precision
        ms = args.min_support if args.min_support is not None else cfg.rule_min_support
        from paperlite.taskq import title_dup as inspect_mod
        rules, flagged = inspect_mod.inspect(q, trigger, mp, ms)
        print(f"巡检完成：本次生效规则 {rules} 条，新跳过 {flagged} 条；"
              f"累计规则 {len(q.skip_rules())} 条、跳过 {q.skip_counts().get('title_dup', 0)} 条")
    finally:
        db.close()


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


# 顶层帮助里的命令清单（按常用顺序）
_COMMANDS = {
    "init": "在当前目录生成配置模板 config.toml",
    "import": "导入任务（txt 一行一个链接，或带 url 列的 CSV）",
    "run": "下载，下载完退出。默认浏览器模式（各种主机混着跑、跟跳转）；--curl 走单主机 cookie 池",
    "produce": "只生产身份（curl 模式），常驻",
    "download": "只下载（curl 模式），需有生产者在跑",
    "status": "任务统计、失败分类、块统计（curl 模式下含身份池）",
    "inspect": "巡检：同标题重复到阈值就归纳 doi/url 规则，标记疑似失效/封面的未采任务（不再采）",
    "retry": "把 failed / non_pdf 的任务重置为 pending",
    "export": "导出任务为 CSV",
    "turnstile": "按 URL + sitekey 取 Cloudflare Turnstile token（--serve 起 HTTP 服务）",
    "help": "显示某个命令的帮助",
}

_DESCRIPTION = "大批量下载 PDF/网页（过 Cloudflare）：各种主机混进一个 PostgreSQL 任务队列，一台或多台机器共享，默认浏览器模式统一跑\n\n命令:\n" + \
    "\n".join(f"  {k:<10}{v}" for k, v in _COMMANDS.items())

_EPILOG = """示例:
  paperlite init                         # 生成 config.toml，填好 [pg]、[proxy] 和 [paths]
  paperlite import --urls mixed.csv      # 导入（多主机随便混）到 [pg] 的批次
  paperlite run config.toml              # 按 [run] 启动（默认浏览器模式），Ctrl+C 随时停，再执行从断点继续
  paperlite run --workers 20             # 命令行临时跑（默认浏览器；--curl 走单主机 cookie 池）
  paperlite status

用 paperlite help <命令> 查看某个命令的参数。"""


class _Formatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix="用法: ")


class _Parser(argparse.ArgumentParser):
    _ERRORS = {"argument <命令>: invalid choice": "未知命令", "choose from": "可选", "the following arguments are required": "缺少必填参数", "unrecognized arguments": "无法识别的参数",
               "invalid choice": "无效的选项", "expected one argument": "需要一个值",
               "not allowed with argument": "不能与此参数同时使用",
               "one of the arguments": "必须给出其中之一", "is required": ""}

    def error(self, message):
        for en, zh in self._ERRORS.items():
            message = message.replace(en, zh)
        self.print_usage(sys.stderr)
        sys.exit(f"错误: {message.strip()}\n用 paperlite help {self.prog.split()[-1]} 查看参数"
                 if " " in self.prog else f"错误: {message.strip()}\n用 paperlite -h 查看用法")


def _new_parser(factory, *args, **kw) -> argparse.ArgumentParser:
    p = factory(*args, add_help=False, formatter_class=_Formatter, **kw)
    p._optionals.title = "选项"
    p.add_argument("-h", "--help", action="help", help="显示帮助")
    return p


def _build_parser() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    ap = _new_parser(_Parser, prog="paperlite", usage="%(prog)s [选项] <命令> [参数]",
                     description=_DESCRIPTION, epilog=_EPILOG)
    ap.add_argument("--config", help=f"配置文件，默认取环境变量 {config_mod.CONFIG_ENV}，再默认当前目录的 config.toml")
    ap.add_argument("--log-level", default="INFO", help="日志级别，默认 INFO")
    ap.add_argument("--log-format", choices=logutil.LOG_FORMATS, default="auto",
                    help="终端日志格式：auto（默认，交互式给文本、重定向给 JSON）/ text / json。日志文件始终为 JSON")
    ap.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}", help="显示版本")
    sub = ap.add_subparsers(dest="cmd", metavar="<命令>", help=argparse.SUPPRESS)
    parsers: dict[str, argparse.ArgumentParser] = {}

    def cmd(name: str) -> argparse.ArgumentParser:
        parsers[name] = _new_parser(sub.add_parser, name, prog=f"paperlite {name}",
                                    description=_COMMANDS[name])
        return parsers[name]

    p = cmd("init")
    p.add_argument("--force", action="store_true", help="已存在也覆盖")
    p = cmd("import")
    p.add_argument("--urls", required=True, help="txt 一行一个链接，或带 url 列的 CSV；多主机可混")
    p.add_argument("--source", default="", help="写入 source")
    p.add_argument("--batch", default="", help="写入 batch_no")
    p.add_argument("--type", choices=UrlType.ALL, help="链接类型：pdf = 要 PDF 文件；html = 要网页本身，整页存为 .html。"
                   "不写时取配置文件 [run] url_type（没有则 pdf）；CSV 的 url_type 列逐行覆盖")
    p = cmd("produce")
    p = cmd("download")
    p.add_argument("--storage", help="输出目录 PDF/HTML（覆盖 [paths].storage）；可挂载")
    p.add_argument("--urls", help="先导入这个文件（可选）")
    p.add_argument("--type", choices=UrlType.ALL, help="--urls 的链接类型，同 import --type")
    p.add_argument("--workers", type=int, default=20, help="下载线程数 = 同时在用的身份数，默认 20")
    p = cmd("run")
    p.add_argument("config_file", nargs="?", metavar="[配置文件]",
                   help="给了就按它的 [run] 段启动（paperlite run config.toml），其余 run 参数忽略；省略则用下面的参数临时跑")
    p.add_argument("--storage", help="输出目录 PDF/HTML（覆盖 [paths].storage）；可挂载")
    p.add_argument("--urls", help="先导入这个文件（可选）")
    p.add_argument("--type", choices=UrlType.ALL, help="--urls 的链接类型，同 import --type")
    p.add_argument("--workers", type=int, default=20,
                   help="并发数：浏览器模式 = 同时打开的浏览器数；--curl 模式 = 下载线程/身份数。默认 20")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--browser", action="store_true", help="浏览器模式（默认）：每浏览器+IP 过盾一次后连续下、整页存 .html；可混多主机、能跟跳转")
    g.add_argument("--curl", action="store_true", help="curl 模式：浏览器过盾存 cookie、curl 批量下，省资源；须单主机")
    p.add_argument("--no-proxy", action="store_true", help="仅浏览器模式：直连本机 IP、不取代理（本地测试，无需代理配置）")
    p.add_argument("--headed", action="store_true", help="仅浏览器模式：有界面运行（调试）")
    p = cmd("status")
    p = cmd("inspect")
    p.add_argument("--trigger", type=int, help="同标题重复多少篇触发（覆盖配置 title_dup_trigger）")
    p.add_argument("--min-precision", type=float, help="规则最低精确率（覆盖配置 rule_min_precision）")
    p.add_argument("--min-support", type=int, help="规则最低命中数（覆盖配置 rule_min_support）")
    p.add_argument("--list", action="store_true", help="只列出已生效的规则，不重新巡检")
    p.add_argument("--reset", action="store_true", help="清除全部规则，放回所有巡检跳过的任务")
    p = cmd("retry")
    p.add_argument("--status", required=True, choices=[Status.FAILED, Status.NON_PDF], help="重置该状态的任务")
    p.add_argument("--kind", choices=ERROR_KINDS, help="配合 --status，只重置该 error_kind")
    p = cmd("export")
    p.add_argument("--status", choices=Status.FINAL + (Status.PENDING,), help="只导出该状态")
    p.add_argument("--file", default="-", help="输出文件，默认标准输出")
    p = cmd("turnstile")
    p.add_argument("--url", help="目标页 URL（token 绑定其 hostname）")
    p.add_argument("--sitekey", help="目标站 Turnstile sitekey（0x4AAAA…）")
    p.add_argument("--action", default="", help="可选：控件的 action")
    p.add_argument("--cdata", default="", help="可选：控件的 cData")
    p.add_argument("--proxy", default="", help="代理 scheme://[user:pass@]host:port；提交 token 时须用同一出口")
    p.add_argument("--timeout", type=float, default=60, help="单次最长等待秒数，默认 60")
    p.add_argument("--headed", action="store_true", help="有界面运行（调试）")
    p.add_argument("--json", action="store_true", help="输出 JSON（含 user_agent、耗时）而非只输出 token")
    p.add_argument("--serve", type=int, metavar="PORT", help="起 HTTP 服务：POST /solve {url, sitekey, ...}")
    p.add_argument("--bind", default="127.0.0.1", help="--serve 监听地址，默认 127.0.0.1")
    p.add_argument("--concurrency", type=int, default=4, help="--serve 同时运行的浏览器数，默认 4")
    p = cmd("help")
    p.add_argument("command", nargs="?", choices=[c for c in _COMMANDS if c != "help"], metavar="<命令>")
    return ap, parsers


def main() -> None:
    try:
        _main()
    except UserError as e:
        raise SystemExit(str(e)) from None


def _main() -> None:
    ap, parsers = _build_parser()
    args = ap.parse_args()
    if args.cmd is None:
        try:
            run = config_mod.load_run(config_mod.resolve_path(args.config))
        except FileNotFoundError:
            ap.print_help()
            return
        except (TypeError, ValueError) as e:
            raise SystemExit(f"配置错误: {e}")
        if run.command != "run":
            ap.print_help()
            return
        args = _configured_run_args(args, run)
    elif args.cmd == "run" and args.config_file:          # paperlite run config.toml
        try:
            run = config_mod.load_run(config_mod.resolve_path(args.config_file))
        except FileNotFoundError as e:
            raise SystemExit(str(e))
        except (TypeError, ValueError) as e:
            raise SystemExit(f"配置错误: {e}")
        args = _configured_run_args(args, run, config=args.config_file)
    if args.cmd == "help":
        (parsers[args.command] if args.command else ap).print_help()
        return

    log_path = setup_logging(args.log_level, _paths(args).log_dir, stderr=args.cmd == "export",
                             fmt=getattr(args, "log_format", "auto"))
    for noisy in ("asyncio", "curl_cffi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if log_path:
        logger.info("日志文件 %s", log_path.resolve())

    # SIGTERM（pkill / systemd / docker stop）与 Ctrl+C 一样优雅退出；
    # 非交互 shell 用 & 启动的进程 SIGINT 被设为忽略，这里显式恢复
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _raise_interrupt)

    if args.cmd == "init":
        path = config_mod.resolve_path(args.config)
        try:
            config_mod.write_example(path, args.force)
        except FileExistsError as e:
            sys.exit(str(e))
        logger.info("已生成 %s：填好 [pg] 的 dsn/batch、[proxy] 的接口和代理账号密码、[paths] 的 storage 再用", path.resolve())
        return
    if args.cmd == "import":
        db = _open_db(args)
        try:
            importer.import_file(db, args.urls, args.source, args.batch, _url_type(args))
            logger.info("任务队列 %s", db.path)
        finally:
            db.close()
        return
    if args.cmd == "retry":
        db = _open_db(args)
        try:
            n = db.q.reset(args.status, args.kind)
            db.q.requeue_leftovers(db.chunk_size)
        finally:
            db.close()
        print(f"已重置 {n} 个任务为 pending（重新切块，下载机会接着领）")
        return
    if args.cmd == "export":
        _export(args)
        return
    if args.cmd == "turnstile":
        _turnstile(args, parsers["turnstile"])
        return

    if args.cmd == "run" and not args.curl:         # 浏览器模式（默认）
        if args.no_proxy:
            cfg = _limits(args)
            proxy = None
        else:
            cfg, proxy = _load(args.config)
        if args.headed:
            cfg = dataclasses.replace(cfg, headed=True)
        require_geoip(cfg)
        _browse(args, cfg, proxy, no_proxy=args.no_proxy)
        return

    if args.cmd == "inspect":
        _inspect(args)
        return
    if args.cmd == "status":
        _status(args, _limits(args))
        return

    cfg, proxy = _load(args.config)                 # produce / download / run --curl
    require_geoip(cfg)                              # curl 模式过盾也用浏览器，同样需要 GeoIP 库
    store = _open_store(args)
    try:
        if args.cmd == "produce":
            run_producer(cfg, proxy, store, _open_db(args))
        else:
            _download(args, cfg, proxy, store, with_producer=args.cmd == "run")
    finally:
        store.close()


if __name__ == "__main__":
    main()
