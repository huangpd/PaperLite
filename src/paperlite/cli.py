#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PaperLite 命令行。一份 config = 一个批次：各种主机的 URL 混着导进**一个任务库**，默认用**浏览器模式**统一跑。
位置在 [paths] 里定义：db（任务库，本地盘）、storage（输出 PDF/HTML，可挂载），两者相互独立。

  paperlite init                                    # 在当前目录生成 config.toml 模板
  paperlite import   --urls mixed.csv               # 导入任务（txt 一行一个，或带 url 列的 CSV），多主机随便混
  paperlite import   --urls pages.txt --type html   # 要网页本身（存 .html），数据库 url_type = html
  paperlite run config.toml                         # 按 [run] 启动（默认浏览器模式），下载完退出
  paperlite run      --workers 20                   # 命令行临时跑（默认浏览器；--curl 走单主机 cookie 池）
  paperlite status                                  # 任务统计（+ curl 模式下的身份池）
  paperlite retry    --status failed                # 把失败的重置为 pending
  paperlite export   --status non_pdf > non_pdf.csv
  paperlite turnstile --url <页面> --sitekey <key>  # 取 Turnstile token；--serve 8191 起 HTTP 服务

位置：任务库 = --db > [paths].db；输出目录 = --storage > [paths].storage。
db 必须本地盘（SQLite 要随机写/文件锁/mmap，FUSE 挂载不支持）；storage 可放 goofys/s3fs 等对象存储挂载。

配置查找顺序：--config > 环境变量 PAPERLITE_CONFIG > 当前目录的 config.toml（paperlite init 生成）。
也可以 python -m paperlite ...。
"""

import argparse
import asyncio
import csv
import dataclasses
import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from paperlite import __version__
from paperlite import config as config_mod
from paperlite.config import Limits, Paths, RunConfig
from paperlite.browser_pool import BrowserPool
from paperlite.consumer import Downloader
from paperlite import logutil
from paperlite.logutil import setup_logging
from paperlite.producer import Producer
from paperlite.proxy import IpSource, ProxySettings
from paperlite.store import IdentityStore
from paperlite.tasks import ERROR_KINDS, Status, TaskDB, UrlType

logger = logging.getLogger("paperlite")


def _configured_run_args(args, run: RunConfig, config: str | None = None):
    """把 [run] 转成与 ``paperlite run ...`` 相同的 argparse 参数（db/storage 仍从 [paths] 取）。"""
    return argparse.Namespace(
        cmd="run", config=config if config is not None else args.config,
        log_level=args.log_level, log_format=getattr(args, "log_format", "auto"),
        db=None, storage=None,
        urls=run.urls or None, type=run.url_type, workers=run.workers,
        curl=run.mode == "curl", browser=run.mode == "browser",
        auto=run.mode == "auto", rules=getattr(run, "rules", "domain_rules.toml"),
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
    """只读 [limits]；没有配置文件时用默认值（import / status / retry / export 不必有代理配置）。"""
    try:
        return config_mod.load_limits(config_mod.resolve_path(args.config))
    except FileNotFoundError:
        return Limits()
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")


def _paths(args) -> Paths:
    """[paths]：db / storage / log_dir；没有配置文件时用默认值。"""
    try:
        return config_mod.load_paths(config_mod.resolve_path(args.config))
    except FileNotFoundError:
        return Paths()
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")


def _db_file(args, paths: Paths) -> Path:
    return Path(args.db).expanduser() if getattr(args, "db", None) else paths.db_path


def _storage_dir(args, paths: Paths) -> Path:
    return Path(args.storage).expanduser() if getattr(args, "storage", None) else paths.storage_path


def _open_db(args, create: bool = False) -> TaskDB:
    """打开任务库（位置：--db > [paths].db）。create=False 且文件不存在就报错。"""
    db = _db_file(args, _paths(args))
    if not create and not db.exists():
        raise SystemExit(f"任务库不存在: {db}（先 import，或用 --db 指定）")
    return TaskDB(db)


def _require_geoip(cfg) -> None:
    """geoip=true：启动前确保 GeoIP 库就位——缺失则限时下载一次；下不动就快速报错给手动预置指引
    （而不是让 cloakbrowser 每次启动都卡 5 分钟重试）。"""
    if not cfg.geoip:
        return
    from paperlite.browser import ensure_geoip_db
    ok, path, url = ensure_geoip_db()
    if ok:
        return
    raise SystemExit(
        f"geoip = true，但 GeoIP 库下载不下来（服务器/受限网络常连不上 GitHub），这里先拦下不让它卡死。\n"
        f"目标路径：{path}\n"
        f"请手动装好再跑，二选一：\n"
        f"  1) 能连 GitHub 的机器上下载后放到该路径（或在别处下好 scp 过去）：\n"
        f"     curl -L --create-dirs -o '{path}' '{url}'\n"
        f"  2) 不用 geoip（WebRTC 伪造仍由 webrtc_spoof 保留）：在 config.toml 的 [limits] 设 geoip = false")


def _read_url_file(path: str):
    """txt：一行一个链接；CSV：表头含 url（或旧名 pdf_url），可选 doi / source / batch_no / url_type。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        first = f.readline()
        f.seek(0)
        if "url" in first or "pdf_url" in first:
            yield from (r for r in csv.DictReader(f) if (r.get("url") or r.get("pdf_url") or "").strip())
        else:
            yield from ({"url": line.strip()} for line in f if line.strip().startswith("http"))


def _import(db: TaskDB, path: str, source: str = "", batch: str = "", url_type: str = UrlType.PDF) -> None:
    """url_type 是整批的默认类型；CSV 里的 url_type 列逐行覆盖。"""
    rows = ({**r, "source": r.get("source") or source, "batch_no": r.get("batch_no") or batch,
             "url_type": (r.get("url_type") or "").strip() or url_type}
            for r in _read_url_file(path))
    try:
        added, existed = db.import_urls(rows)
    except ValueError as e:
        raise SystemExit(str(e))
    logger.info("导入 %s：新增 %d，已存在 %d（已存在的不重置）", path, added, existed)


class _ProducerThread:
    """后台线程跑生产者。结束用 finished 事件判断：信号打断 Thread.join() 后 is_alive() 可能误报已结束。"""

    def __init__(self, cfg, store, proxy, host) -> None:
        self.stop = threading.Event()
        self.finished = threading.Event()
        self._producer = Producer(cfg, store, IpSource(proxy), host)
        threading.Thread(target=self._run, name="producer", daemon=True).start()

    def _run(self) -> None:
        try:
            asyncio.run(self._producer.run(self.stop))
        finally:
            self.finished.set()

    def shutdown(self, timeout_s: float) -> None:
        self.stop.set()
        if not self.finished.wait(timeout_s):
            logger.warning("生产者 %.0fs 内未退出", timeout_s)


def _download(args, cfg, proxy, store, with_producer: bool) -> None:
    db = _open_db(args, create=True)
    if args.urls:
        _import(db, args.urls, url_type=args.type)
    hs = db.hosts()
    if len(hs) > 1:
        db.close()
        raise SystemExit(f"curl 模式需单主机，本库有 {len(hs)} 个：{'、'.join(hs[:5])}。"
                         f"混多主机请去掉 --curl 用浏览器模式")
    out = _storage_dir(args, _paths(args))
    try:
        reclaimed = db.lock_for_download()
    except RuntimeError as e:
        raise SystemExit(str(e))
    if reclaimed:
        logger.info("上次中断残留的 %d 个 running 任务已放回 pending", reclaimed)
    try:
        dl = Downloader(cfg, store, proxy, db, out, args.workers)
    except ValueError as e:
        raise SystemExit(str(e))
    c = db.counts()
    logger.info("输出目录 %s，任务库 %s", out, db.path)
    logger.info("%s：待下载 %d，已成功 %d，non_pdf %d，失败 %d；下载线程 %d", dl.host, c[Status.PENDING],
                c[Status.SUCCESS], c[Status.NON_PDF], c[Status.FAILED], args.workers)
    if not c[Status.PENDING]:
        return
    producer = _ProducerThread(cfg, store, proxy, dl.host) if with_producer else None
    stop = threading.Event()
    threading.Thread(target=_report, args=(dl, store, cfg, stop), daemon=True, name="report").start()
    monitor = _attach_monitor(args, lambda: _snapshot("curl", dl.db, dl.stats, 0, args.workers))
    t0 = time.monotonic()
    try:
        dl.run(stop)
    except KeyboardInterrupt:
        logger.info("中断：在途任务已放回 pending，可重跑继续")
    finally:
        stop.set()
        if producer is not None:
            producer.shutdown(cfg.pass_timeout_s + 30)
        if monitor is not None:
            monitor.close()
        db.close()
    s = dl.stats
    logger.info("结束: 用时 %.1f 分钟，success %d，non_pdf %d，failed %d，用身份 %d 个，丢弃身份 %d 次，"
                "URL 被单独挑战 %d 次，停掉卷期 %d 个（%d 篇未请求）", (time.monotonic() - t0) / 60, s.success,
                s.non_pdf, s.failed, s.identities, s.rejected, s.challenged, s.volumes_stopped, s.inferred)


def _attach_monitor(args, snapshot):
    """[monitor] enabled 时启用监控上报 + 心跳；未配置返回 None。批次名 = 配置文件名（不含扩展名）。"""
    from paperlite.monitor import reporter as mon
    path = config_mod.resolve_path(args.config)
    try:
        return mon.attach(path, path.stem, snapshot)
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")


def _snapshot(mode: str, db: TaskDB, stats, online: int, workers: int) -> dict:
    """心跳内容：在线浏览器数 + 任务库计数（监控面板「实时任务」）。"""
    c = db.counts()
    return {"mode": mode, "browsers_online": online, "workers": workers, "pending": c[Status.PENDING],
            "running": c[Status.RUNNING], "success": c[Status.SUCCESS], "failed": c[Status.FAILED]}


def _report(dl: Downloader, store: IdentityStore, cfg: Limits, stop: threading.Event) -> None:
    """每 30s 一行。速度按"已处理"（PDF + non_pdf + failed）算：non_pdf 成片时只看 PDF 数会误以为变慢。"""
    s = dl.stats
    last_done, last_pdf, last_t = 0, 0, time.monotonic()
    while not stop.wait(30):
        st = store.stats(dl.host, cfg.refill_before_s)
        c = dl.db.counts()
        now = time.monotonic()
        done = s.success + s.non_pdf + s.failed
        minutes = (now - last_t) / 60
        logger.info("进度: 已处理 %d（PDF %d，non_pdf %d，失败 %d），待下载 %d | 身份 空闲 %d 占用 %d "
                    "| 近 30s 处理 %.0f 条/分钟，其中 PDF %.0f 篇/分钟",
                    done, s.success, s.non_pdf, s.failed, c[Status.PENDING] + c[Status.RUNNING],
                    st.ready, st.inuse, (done - last_done) / minutes, (s.success - last_pdf) / minutes)
        last_done, last_pdf, last_t = done, s.success, now


def _status(args, cfg) -> None:
    db = _open_db(args)
    host = db.host()
    c = db.counts()
    print(f"任务库 {db.path}（{host or '空'}）: 共 {sum(c.values())}  " +
          "  ".join(f"{s} {c[s]}" for s in Status.ALL))
    types = db.type_counts()
    if len(types) > 1 or UrlType.PDF not in types:
        for t, tc in sorted(types.items()):
            print(f"  [{t}] 共 {sum(tc.values())}  " + "  ".join(f"{s} {tc[s]}" for s in Status.ALL if tc[s]))
    for status, kind, n, inferred in db.kind_counts():
        print(f"  {status:8s} {kind or '-':16s} {n}" + (f"（其中按卷期推断 {inferred}）" if inferred else ""))
    v, n = db.volume_summary()
    if v:
        print(f"  按卷期停掉 {v} 个卷期，{n} 篇未请求（retry --stopped-volumes 可重新核实）")
    db.close()
    if not host:
        return
    try:
        store = IdentityStore(cfg.redis_url, cfg.key_prefix)
        store.reap(host)
        st = store.stats(host, cfg.refill_before_s)
    except Exception as e:
        print(f"身份池: Redis 不可用（{e}）")
        return
    print(f"身份池 {host}: 空闲 {st.ready}  占用 {st.inuse}  健康 {st.healthy}  在线下载线程 {st.workers}  "
          f"生产者 {store.producer_holder(host) or '无'}")
    for ident in store.list(host):
        print(f"  {ident.exit_ip:15s} 剩余 {ident.left():4.0f}s  过盾 {ident.pass_s:.0f}s  "
              f"创建 {datetime.fromtimestamp(ident.created_at):%H:%M:%S}  指纹 {ident.fingerprint}")


def _export(args) -> None:
    db = _open_db(args)
    rows = db.rows(args.status)
    out = open(args.file, "w", newline="", encoding="utf-8") if args.file != "-" else sys.stdout
    w = csv.writer(out)
    if rows:
        w.writerow(rows[0].keys())
    w.writerows(tuple(r) for r in rows)
    if out is not sys.stdout:
        out.close()
        print(f"导出 {len(rows)} 行到 {args.file}")
    db.close()


def _turnstile(args, parser) -> None:
    from paperlite import turnstile

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


def _browse(args, cfg, proxy, no_proxy: bool = False) -> None:
    """浏览器下载模式：每个浏览器+IP 过盾一次后连续下同域名多篇（整页存 .html）。不用 Redis 身份池。"""
    db = _open_db(args, create=True)
    if args.urls:
        _import(db, args.urls, url_type=args.type)
    out = _storage_dir(args, _paths(args))
    try:
        reclaimed = db.lock_for_download()
    except RuntimeError as e:
        raise SystemExit(str(e))
    if reclaimed:
        logger.info("上次中断残留的 %d 个 running 任务已放回 pending", reclaimed)
    rules_file = getattr(args, "rules", "domain_rules.toml") if getattr(args, "auto", False) else None
    try:
        pool = BrowserPool(cfg, proxy, db, out, args.workers, no_proxy=no_proxy, rules=rules_file)
    except ValueError as e:
        db.close()
        raise SystemExit(str(e))
    c = db.counts()
    logger.info("输出目录 %s，任务库 %s", out, db.path)
    logger.info("%s：待下载 %d，已成功 %d，失败 %d；浏览器 %d", pool.host, c[Status.PENDING],
                c[Status.SUCCESS], c[Status.FAILED], pool.workers)
    if not c[Status.PENDING]:
        db.close()
        return
    stop = threading.Event()
    threading.Thread(target=_browse_report, args=(pool, cfg, stop), daemon=True, name="report").start()
    monitor = _attach_monitor(args, lambda: _snapshot("browser", pool.db, pool.stats, pool.online(), pool.workers))
    t0 = time.monotonic()
    try:
        pool.run(stop)
    except KeyboardInterrupt:
        logger.info("中断：在途任务重跑时会放回 pending，可继续")
    finally:
        stop.set()
        if monitor is not None:
            monitor.close()
        db.close()
    s = pool.stats
    logger.info("结束: 用时 %.1f 分钟，success %d，failed %d，启动浏览器 %d 个",
                (time.monotonic() - t0) / 60, s.success, s.failed, s.browsers)


def _auto(args, cfg, proxy, no_proxy: bool = False) -> None:
    """自适应模式：三路动态分流（curl_direct 直下、curl_pool 需求驱动身份池、browser_only 浏览器渲染）。"""
    from paperlite.scheduler import AutoScheduler
    db = _open_db(args, create=True)
    if args.urls:
        _import(db, args.urls, url_type=args.type)
    out = _storage_dir(args, _paths(args))
    try:
        reclaimed = db.lock_for_download()
    except RuntimeError as e:
        raise SystemExit(str(e))
    if reclaimed:
        logger.info("上次中断残留的 %d 个 running 任务已放回 pending", reclaimed)
    store = None
    try:
        store = IdentityStore(cfg.redis_url, cfg.key_prefix)
        store.ping()
    except Exception:
        store = None
    scheduler = AutoScheduler(
        cfg, proxy, db, out, args.workers,
        rules_path=getattr(args, "rules", "domain_rules.toml"),
        store=store, no_proxy=no_proxy
    )
    stop = threading.Event()
    try:
        scheduler.run(stop)
    except KeyboardInterrupt:
        logger.info("中断：在途任务已安全回滚至 pending，可随时继续")
    finally:
        stop.set()
        db.close()


def _inspect(args) -> None:
    """巡检：同标题重复到阈值就归纳 doi/url 规则、标记疑似封面的未采任务。"""
    cfg = _limits(args)
    db = _open_db(args)
    try:
        if args.reset:
            print(f"已清除 {db.clear_suspects()} 条 suspect_cover 标记与全部规则，放回可采")
            return
        if args.list:
            rules = db.skip_rules()
            if not rules:
                print("还没有生效的规则")
            for r in rules:
                print(f"  doi~{r['doi_prefix']!r} url~{r['url_prefix']!r} | 标题 {(r['title'] or '')[:40]!r} | "
                      f"precision {r['precision']} 已采命中 {r['support']} 标记 {r['flagged']} | {r['created_at']}")
            return
        trigger = args.trigger if args.trigger is not None else cfg.title_dup_trigger
        mp = args.min_precision if args.min_precision is not None else cfg.rule_min_precision
        ms = args.min_support if args.min_support is not None else cfg.rule_min_support
        from paperlite import inspect as inspect_mod
        rules, flagged = inspect_mod.inspect(db, trigger, mp, ms)
        print(f"巡检完成：本次生效规则 {rules} 条，新标记疑似封面 {flagged} 条；"
              f"累计规则 {len(db.skip_rules())} 条、疑似封面 {db.suspect_count()} 条")
    finally:
        db.close()


def _browse_report(pool: BrowserPool, cfg: Limits, stop: threading.Event) -> None:
    s = pool.stats
    last_done, last_t = 0, time.monotonic()
    while not stop.wait(30):
        c = pool.db.counts()
        now = time.monotonic()
        done = s.success + s.failed
        rate = (done - last_done) / max((now - last_t) / 60, 1e-9)
        last_done, last_t = done, now
        logger.info("进度: 成功 %d 失败 %d 待下载 %d running %d | 浏览器 %d | 近 30s %.0f 篇/分钟",
                    s.success, s.failed, c[Status.PENDING], c[Status.RUNNING], pool.workers, rate)


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


# 顶层帮助里的命令清单（按常用顺序）
_COMMANDS = {
    "init": "在当前目录生成配置模板 config.toml",
    "import": "导入任务（txt 一行一个链接，或带 url 列的 CSV）",
    "run": "下载，下载完退出。默认浏览器模式（各种主机混着跑、跟跳转）；--curl 走单主机 cookie 池",
    "produce": "只生产身份（curl 模式），常驻",
    "download": "只下载（curl 模式），需有生产者在跑",
    "status": "任务统计、失败分类（curl 模式下含身份池）",
    "inspect": "巡检：同标题重复到阈值就归纳 doi/url 规则，标记疑似失效/封面的未采任务（不再采）",
    "retry": "把 failed / non_pdf 的任务重置为 pending",
    "export": "导出任务为 CSV",
    "turnstile": "按 URL + sitekey 取 Cloudflare Turnstile token（--serve 起 HTTP 服务）",
    "preprocess": "预处理：基于 DuckDB 极速聚类大类小类、嗅探落地域名、双视角体检定级并导出 domain_rules.toml",
    "resolve": "预处理阶段一：基于 DuckDB 聚类族群并嗅探真实落地域名，建档至 SQLite 库",
    "audit": "预处理阶段二：仅对已嗅探的独立域名做双视角反爬体检，导出 domain_rules.toml",
    "help": "显示某个命令的帮助",
}

_DESCRIPTION = "大批量下载 PDF/网页（过 Cloudflare）：各种主机混进一个库，默认浏览器模式统一跑\n\n命令:\n" + \
    "\n".join(f"  {k:<10}{v}" for k, v in _COMMANDS.items())

_EPILOG = """示例:
  paperlite init                         # 生成 config.toml，填好 [proxy] 和 [paths]
  paperlite import --urls mixed.csv      # 导入（多主机随便混）到 [paths].db
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

    def site_args(p):
        p.add_argument("--db", help="任务库文件（覆盖 [paths].db）；必须本地盘")

    p = cmd("init")
    p.add_argument("--force", action="store_true", help="已存在也覆盖")
    p = cmd("import")
    p.add_argument("--urls", required=True, help="txt 一行一个链接，或带 url 列的 CSV；多主机可混")
    site_args(p)
    p.add_argument("--source", default="", help="写入 source")
    p.add_argument("--batch", default="", help="写入 batch_no")
    p.add_argument("--type", choices=UrlType.ALL, default=UrlType.PDF, help="链接类型：pdf = 要 PDF 文件（默认）；html = 要网页本身，整页存为 .html。CSV 的 url_type 列逐行覆盖")
    p = cmd("produce")
    site_args(p)
    p.add_argument("--keep", type=int, help="没有下载线程时也养着这么多身份（覆盖 min_identities），"
                                            "下载前一两分钟先启动可省掉冷启动的过盾等待")
    p = cmd("download")
    site_args(p)
    p.add_argument("--storage", help="输出目录 PDF/HTML（覆盖 [paths].storage）；可挂载")
    p.add_argument("--urls", help="先导入这个文件（可选）")
    p.add_argument("--type", choices=UrlType.ALL, default=UrlType.PDF, help="--urls 的链接类型，同 import --type")
    p.add_argument("--workers", type=int, default=20, help="下载线程数 = 同时在用的身份数，默认 20")
    p = cmd("run")
    p.add_argument("config_file", nargs="?", metavar="[配置文件]",
                   help="给了就按它的 [run] 段启动（paperlite run config.toml），其余 run 参数忽略；省略则用下面的参数临时跑")
    site_args(p)
    p.add_argument("--storage", help="输出目录 PDF/HTML（覆盖 [paths].storage）；可挂载")
    p.add_argument("--urls", help="先导入这个文件（可选）")
    p.add_argument("--type", choices=UrlType.ALL, default=UrlType.PDF, help="--urls 的链接类型，同 import --type")
    p.add_argument("--workers", type=int, default=20,
                   help="并发数：浏览器模式 = 同时打开的浏览器数；--curl 模式 = 下载线程/身份数。默认 20")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--browser", action="store_true", help="浏览器模式（默认）：每浏览器+IP 过盾一次后连续下、整页存 .html；可混多主机、能跟跳转")
    g.add_argument("--curl", action="store_true", help="curl 模式：浏览器过盾存 cookie、curl 批量下，省资源；须单主机")
    g.add_argument("--auto", action="store_true", help="自适应模式：结合 domain_rules.toml 自动对 curl 友好站点直连秒下，对强风控站点走无头浏览器")
    p.add_argument("--rules", default="domain_rules.toml", help="自适应分流规则文件，默认 domain_rules.toml")
    p.add_argument("--no-proxy", action="store_true", help="仅浏览器模式：直连本机 IP、不取代理（本地测试，无需代理配置）")
    p.add_argument("--headed", action="store_true", help="仅浏览器模式：有界面运行（调试）")
    p = cmd("status")
    site_args(p)
    p = cmd("inspect")
    site_args(p)
    p.add_argument("--trigger", type=int, help="同标题重复多少篇触发（覆盖配置 title_dup_trigger）")
    p.add_argument("--min-precision", type=float, help="规则最低精确率（覆盖配置 rule_min_precision）")
    p.add_argument("--min-support", type=int, help="规则最低命中数（覆盖配置 rule_min_support）")
    p.add_argument("--list", action="store_true", help="只列出已生效的规则，不重新巡检")
    p.add_argument("--reset", action="store_true", help="清除所有 suspect_cover 标记与规则（放回可采）")
    p = cmd("retry")
    site_args(p)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", choices=[Status.FAILED, Status.NON_PDF], help="重置该状态的任务")
    g.add_argument("--stopped-volumes", action="store_true",
                   help="重新核实按卷期停掉的：这些卷期的 non_pdf（样本 + 推断）全部放回 pending")
    p.add_argument("--kind", choices=ERROR_KINDS, help="配合 --status，只重置该 error_kind")
    p = cmd("export")
    site_args(p)
    p.add_argument("--status", choices=Status.ALL, help="只导出该状态")
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

    # 预处理命令族
    p = cmd("preprocess")
    p.add_argument("--urls", required=True, help="输入的 CSV 文件路径（含 DOI 列或 URL 列）")
    p.add_argument("--out-map", default="doi_routes.db", help="阶段一中间态 SQLite 映射库，默认 doi_routes.db")
    p.add_argument("--out-rules", default="domain_rules.toml", help="阶段二最终导出的 TOML 规则文件，默认 domain_rules.toml")
    p.add_argument("--proxy", help="使用的代理地址 (如 http://127.0.0.1:7897)，不给则尝试读配置或直连")
    p.add_argument("--concurrency", type=int, default=6, help="阶段一轻量跳转嗅探并发数，默认 6")

    p = cmd("resolve")
    p.add_argument("--urls", required=True, help="输入的 CSV 文件路径（含 DOI 列或 URL 列）")
    p.add_argument("--out-map", default="doi_routes.db", help="输出的 SQLite 映射库，默认 doi_routes.db")
    p.add_argument("--proxy", help="使用的代理地址 (如 http://127.0.0.1:7897)")
    p.add_argument("--concurrency", type=int, default=6, help="轻量跳转嗅探并发数，默认 6")
    p.add_argument("--limit", type=int, help="最多只嗅探前 N 个代表族群（用于小规模测试）")

    p = cmd("audit")
    p.add_argument("--map", default="doi_routes.db", help="阶段一生成的 SQLite 映射库，默认 doi_routes.db")
    p.add_argument("--out-rules", default="domain_rules.toml", help="最终导出的 TOML 规则文件，默认 domain_rules.toml")
    p.add_argument("--proxy", help="使用的代理地址 (如 http://127.0.0.1:7897)")
    p.add_argument("--limit", type=int, help="最多只体检前 N 个独立域名（用于小规模测试）")

    p = cmd("help")
    p.add_argument("command", nargs="?", choices=[c for c in _COMMANDS if c != "help"], metavar="<命令>")
    return ap, parsers


def main() -> None:
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
        logger.info("已生成 %s：填好 [proxy] 的接口和代理账号密码、[paths] 的 db/storage 再用", path.resolve())
        return
    if args.cmd == "import":
        db = _open_db(args, create=True)
        _import(db, args.urls, args.source, args.batch, args.type)
        logger.info("任务库 %s", db.path)
        db.close()
        return
    if args.cmd == "retry":
        db = _open_db(args)
        if args.stopped_volumes:
            v, n = db.reset_stopped_volumes()
            print(f"已重置 {v} 个被停卷期的 {n} 个任务为 pending")
        else:
            print(f"已重置 {db.reset(args.status, args.kind)} 个任务为 pending")
        db.close()
        return
    if args.cmd == "export":
        _export(args)
        return
    if args.cmd == "turnstile":
        _turnstile(args, parsers["turnstile"])
        return
    if args.cmd == "resolve":
        from paperlite.preprocess import PreprocessManager
        asyncio.run(PreprocessManager.run_resolve(
            csv_path=args.urls, out_db=args.out_map, proxy=args.proxy,
            concurrency=args.concurrency, limit_patterns=args.limit
        ))
        return
    if args.cmd == "audit":
        from paperlite.preprocess import PreprocessManager
        asyncio.run(PreprocessManager.run_audit(
            in_db=args.map, out_toml=args.out_rules, proxy=args.proxy,
            limit_domains=args.limit
        ))
        return
    if args.cmd == "preprocess":
        from paperlite.preprocess import PreprocessManager
        asyncio.run(PreprocessManager.run_pipeline(
            csv_path=args.urls, out_db=args.out_map, out_toml=args.out_rules,
            proxy=args.proxy, concurrency=args.concurrency
        ))
        return

    if args.cmd == "run" and getattr(args, "auto", False):  # 自适应中间调度模式
        if args.no_proxy:
            cfg = _limits(args)
            proxy = None
        else:
            cfg, proxy = _load(args.config)
        if args.headed:
            cfg = dataclasses.replace(cfg, headed=True)
        _require_geoip(cfg)
        _auto(args, cfg, proxy, no_proxy=args.no_proxy)
        return

    if args.cmd == "run" and not args.curl:         # 浏览器模式（默认）
        if args.no_proxy:
            cfg = _limits(args)
            proxy = None
        else:
            cfg, proxy = _load(args.config)
        if args.headed:
            cfg = dataclasses.replace(cfg, headed=True)
        _require_geoip(cfg)
        _browse(args, cfg, proxy, no_proxy=args.no_proxy)
        return

    if args.cmd == "inspect":
        _inspect(args)
        return
    if args.cmd == "status":
        _status(args, _limits(args))
        return

    cfg, proxy = _load(args.config)                 # produce / download / run --curl
    _require_geoip(cfg)                             # curl 模式过盾也用浏览器，同样需要 GeoIP 库
    store = IdentityStore(cfg.redis_url, cfg.key_prefix)
    store.ping()
    if args.cmd == "produce":
        db = _open_db(args)
        host = db.host()
        db.close()
        if not host:
            raise SystemExit("任务库是空的，先 import")
        if args.keep is not None:
            cfg = dataclasses.replace(cfg, min_identities=args.keep)
        producer = _ProducerThread(cfg, store, proxy, host)
        try:
            while not producer.finished.wait(1):
                pass
        except KeyboardInterrupt:
            producer.shutdown(cfg.pass_timeout_s + 30)
    else:
        _download(args, cfg, proxy, store, with_producer=args.cmd == "run")


if __name__ == "__main__":
    main()
