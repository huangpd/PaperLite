#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PaperLite 命令行。一个站点（主机）一个输出目录，默认 <out_root>/<主机>（out_root 默认 pdfs），任务库是其中的 tasks.db。
生产与消费可以分开跑（不同进程，只共享 Redis），也可以 run 一起跑。

  paperlite init                                    # 在当前目录生成 config.toml 模板
  paperlite import   --urls acm.txt                 # 导入任务（txt 一行一个，或带 pdf_url 列的 CSV）→ pdfs/dl.acm.org/
  paperlite run      --workers 20                   # 同进程生产 + 消费，下载完退出
  paperlite produce                                 # 只生产身份，常驻
  paperlite download --workers 20                   # 只消费（需有生产者在跑）
  paperlite status                                  # 任务统计 + 身份池
  paperlite retry    --status failed                # 把失败的重置为 pending
  paperlite export   --status non_pdf > non_pdf.csv

站点：--out > --host / --urls 的主机 / --db 记录的主机 → <out_root>/<主机> > out_root 下唯一的站点；多个站点时须指定。
任务库：--db > 配置 [pool] db_dir > <输出目录>/tasks.db（PDF 输出到 goofys / s3fs 这类对象存储挂载时，
任务库必须放本地盘：SQLite 要随机写、文件锁和 mmap，FUSE 挂载不支持）。

配置查找顺序：--config > 环境变量 PAPERLITE_CONFIG > 当前目录的 config.toml（paperlite init 生成）。
也可以 python -m paperlite ...。
"""

import argparse
import asyncio
import csv
import dataclasses
import logging
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from paperlite import __version__
from paperlite import config as config_mod
from paperlite.config import PoolConfig
from paperlite.consumer import Downloader
from paperlite.producer import Producer
from paperlite.proxy import IpSource, ProxySettings
from paperlite.store import IdentityStore
from paperlite.tasks import ERROR_KINDS, Status, TaskDB, url_host

logger = logging.getLogger("paperlite")



def _load(path: str | None) -> tuple[PoolConfig, ProxySettings]:
    try:
        return config_mod.load(config_mod.resolve_path(path))
    except FileNotFoundError as e:
        raise SystemExit(str(e))
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")


def _pool_cfg(args) -> PoolConfig:
    """只读 [pool]（out_root / db_dir 等）；没有配置文件时用默认值，import / status / retry / export 不必有代理配置。"""
    try:
        return config_mod.load_pool(config_mod.resolve_path(args.config))
    except FileNotFoundError:
        return PoolConfig()
    except (TypeError, ValueError) as e:
        raise SystemExit(f"配置错误: {e}")


def _host_from_urls(path: str | None) -> str | None:
    if not path:
        return None
    first = next(iter(_read_url_file(path)), None)
    return url_host(first["pdf_url"]) if first else None


def _resolve_out(args, pool: PoolConfig) -> Path:
    """站点输出目录：--out > --host / --urls / --db 的主机 → <out_root>/<主机> > out_root 下唯一的站点。"""
    if getattr(args, "out", None):
        return Path(args.out).expanduser()
    host = getattr(args, "host", None) or _host_from_urls(getattr(args, "urls", None))
    if not host and getattr(args, "db", None) and Path(args.db).expanduser().exists():
        db = TaskDB(Path(args.db).expanduser())
        host = db.host()
        db.close()
    root = Path(pool.out_root).expanduser()
    if host:
        return root / host
    sites = sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    if len(sites) == 1:
        return root / sites[0]
    if not sites:
        raise SystemExit(f"{root.resolve()} 下还没有站点：先 import --urls <文件>，或用 --out 指定输出目录")
    raise SystemExit(f"{root.resolve()} 下有多个站点：{', '.join(sites)}；用 --host 或 --out 指定")


def _open_site(args, create: bool = False) -> tuple[Path, TaskDB]:
    """返回 (输出目录, 任务库)。create=True（import / download / run）时建好输出目录。"""
    pool = _pool_cfg(args)
    out = _resolve_out(args, pool)
    path = config_mod.task_db_path(str(out), getattr(args, "db", None), pool.db_dir)
    if create:
        out.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        raise SystemExit(f"任务库不存在: {path}（输出目录 {out}）；先 import，或检查 --out / --db / db_dir")
    logger.debug("输出目录 %s，任务库 %s", out, path)
    return out, TaskDB(path)


def _read_url_file(path: str):
    """txt：一行一个链接；CSV：表头含 pdf_url（可选 paper_uid / source / batch_no），与 paper_downloader 导入格式相同。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        first = f.readline()
        f.seek(0)
        if "pdf_url" in first:
            yield from (r for r in csv.DictReader(f) if (r.get("pdf_url") or "").strip())
        else:
            yield from ({"pdf_url": line.strip()} for line in f if line.strip().startswith("http"))


def _import(db: TaskDB, path: str, source: str = "", batch: str = "") -> None:
    rows = ({**r, "source": r.get("source") or source, "batch_no": r.get("batch_no") or batch}
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
    out, db = _open_site(args, create=True)
    if args.urls:
        _import(db, args.urls)
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
    t0 = time.monotonic()
    try:
        dl.run(stop)
    except KeyboardInterrupt:
        logger.info("中断：在途任务已放回 pending，可重跑继续")
    finally:
        stop.set()
        if producer is not None:
            producer.shutdown(cfg.pass_timeout_s + 30)
        db.close()
    s = dl.stats
    logger.info("结束: 用时 %.1f 分钟，success %d，non_pdf %d，failed %d，用身份 %d 个，丢弃身份 %d 次，"
                "URL 被单独挑战 %d 次，停掉卷期 %d 个（%d 篇未请求）", (time.monotonic() - t0) / 60, s.success,
                s.non_pdf, s.failed, s.identities, s.rejected, s.challenged, s.volumes_stopped, s.inferred)


def _report(dl: Downloader, store: IdentityStore, cfg: PoolConfig, stop: threading.Event) -> None:
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
    host = args.host
    try:
        _, db = _open_site(args)
    except SystemExit as e:
        if not host:                      # 只给了 --host 而本地没有这个站点的任务库：只看身份池
            raise
        print(e)
        db = None
    if db is not None:
        host = host or db.host()
        c = db.counts()
        print(f"任务库 {db.path}（{host or '空'}）: 共 {sum(c.values())}  " +
              "  ".join(f"{s} {c[s]}" for s in Status.ALL))
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
    _, db = _open_site(args)
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


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def main() -> None:
    ap = argparse.ArgumentParser(prog="paperlite", description="浏览器过盾 cookie 池 + curl_cffi 批量下载")
    ap.add_argument("--config", help=f"配置文件，默认取环境变量 {config_mod.CONFIG_ENV}，再默认当前目录的 config.toml")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def site_args(p, out_required: bool = False):
        p.add_argument("--out", required=out_required,
                       help="站点输出目录（PDF、non_pdf/；默认 <out_root>/<主机>，见配置 out_root）")
        p.add_argument("--host", help="站点主机，如 dl.acm.org（输出目录取 <out_root>/<主机>）")
        p.add_argument("--db", help="任务库路径（优先于配置 db_dir；默认 <输出目录>/tasks.db）；"
                                    "输出目录在 goofys 等对象存储挂载上时须在本地盘")

    p = sub.add_parser("init", help="生成配置模板（默认 ./config.toml，或 --config 指定的路径）")
    p.add_argument("--force", action="store_true", help="已存在也覆盖")
    p = sub.add_parser("import", help="导入任务")
    p.add_argument("--urls", required=True, help="txt 一行一个链接，或带 pdf_url 列的 CSV；须同一主机")
    site_args(p)
    p.add_argument("--source", default="")
    p.add_argument("--batch", default="", help="写入 batch_no")
    p = sub.add_parser("produce", help="只生产身份（常驻，Ctrl+C / SIGTERM 退出）")
    site_args(p)
    p.add_argument("--keep", type=int, help="没有下载线程时也养着这么多身份（覆盖 min_identities），"
                                            "下载前一两分钟先启动可省掉冷启动的过盾等待")
    for name in ("download", "run"):
        p = sub.add_parser(name, help="只消费" if name == "download" else "生产 + 消费，下载完退出")
        site_args(p)
        p.add_argument("--urls", help="先导入这个文件（可选）")
        p.add_argument("--workers", type=int, default=20, help="下载线程数 = 同时在用的身份数")
    p = sub.add_parser("status", help="任务统计与身份池")
    site_args(p)
    p = sub.add_parser("retry", help="把 failed / non_pdf 的任务重置为 pending")
    site_args(p)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", choices=[Status.FAILED, Status.NON_PDF])
    g.add_argument("--stopped-volumes", action="store_true",
                   help="重新核实按卷期停掉的：这些卷期的 non_pdf（样本 + 推断）全部放回 pending")
    p.add_argument("--kind", choices=ERROR_KINDS, help="配合 --status，只重置该 error_kind")
    p = sub.add_parser("export", help="导出任务为 CSV")
    site_args(p)
    p.add_argument("--status", choices=Status.ALL)
    p.add_argument("--file", default="-", help="输出文件，默认标准输出")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr if args.cmd == "export" else sys.stdout,
                        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s", datefmt="%H:%M:%S")
    for noisy in ("asyncio", "curl_cffi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

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
        logger.info("已生成 %s：填好 [proxy] 的提取接口和代理账号密码再用", path.resolve())
        return
    if args.cmd == "import":
        out, db = _open_site(args, create=True)
        _import(db, args.urls, args.source, args.batch)
        logger.info("输出目录 %s，任务库 %s", out, db.path)
        db.close()
        return
    if args.cmd == "retry":
        _, db = _open_site(args)
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

    cfg, proxy = _load(args.config)
    if args.cmd == "status":
        _status(args, cfg)
        return

    store = IdentityStore(cfg.redis_url, cfg.key_prefix)
    store.ping()
    if args.cmd == "produce":
        host = args.host
        if not host:
            _, db = _open_site(args)
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
