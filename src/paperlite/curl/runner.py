"""
paperlite/curl/runner.py
────────────────────────
跑 curl 模式（命令行 run --curl / produce / download）：生产者线程攒身份、下载线程领身份下载、
每 30s 打一行进度、挂监控心跳、结束打汇总。须单主机。任务队列 db 交给这里，结束（包括出错）时由这里关闭。
"""

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from paperlite.config import Limits, ProxySettings
from paperlite.curl.downloader import Downloader
from paperlite.curl.identity import IdentityStore
from paperlite.curl.producer import Producer
from paperlite.errors import UserError
from paperlite.monitor.reporter import task_snapshot
from paperlite.proxy import IpSource
from paperlite.taskq import PgTaskSource
from paperlite.tasks import Status

logger = logging.getLogger("paperlite")

# 传入心跳快照函数，返回带 close() 的句柄；未启用监控时返回 None
AttachMonitor = Callable[[Callable[[], dict]], Optional[Any]]


class ProducerThread:
    """后台线程跑生产者。结束用 finished 事件判断：信号打断 Thread.join() 后 is_alive() 可能误报已结束。"""

    def __init__(self, cfg, store, proxy, host, sample_url=None) -> None:
        self.stop = threading.Event()
        self.finished = threading.Event()
        self._producer = Producer(cfg, store, IpSource(proxy), host, sample_url=sample_url)
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


def run_producer(cfg: Limits, proxy: ProxySettings, store: IdentityStore, db: PgTaskSource) -> None:
    """只生产身份（produce 命令），常驻到 Ctrl+C / SIGTERM。"""
    try:
        host = db.host()
        if not host:
            raise UserError("任务队列是空的，先 import")
        producer = ProducerThread(cfg, store, proxy, host, sample_url=lambda: db.q.sample_pending_url(host))
        try:
            while not producer.finished.wait(1):
                pass
        except KeyboardInterrupt:
            producer.shutdown(cfg.pass_timeout_s + 30)
    finally:
        db.close()


def run_curl(cfg: Limits, proxy: ProxySettings, store: IdentityStore, db: PgTaskSource, out: Path, workers: int, *,
             with_producer: bool, attach_monitor: Optional[AttachMonitor] = None) -> None:
    """下载线程跑完返回（with_producer=True 时同进程带一个生产者，即 run --curl；False 即 download）。"""
    hs = db.hosts()
    if len(hs) > 1:
        db.close()
        raise UserError(f"curl 模式需单主机，本库有 {len(hs)} 个：{'、'.join(hs[:5])}。"
                        f"混多主机请去掉 --curl 用浏览器模式")
    try:
        dl = Downloader(cfg, store, proxy, db, out, workers)
    except ValueError as e:
        db.close()
        raise UserError(str(e)) from e
    c = db.counts()
    logger.info("输出目录 %s，任务队列 %s", out, db.path)
    logger.info("%s：待下载 %d，已成功 %d，non_pdf %d，失败 %d；下载线程 %d", dl.host, c[Status.PENDING],
                c[Status.SUCCESS], c[Status.NON_PDF], c[Status.FAILED], workers)
    if not c[Status.PENDING]:
        db.close()
        return
    producer = (ProducerThread(cfg, store, proxy, dl.host, sample_url=lambda: db.q.sample_pending_url(dl.host))
                if with_producer else None)
    stop = threading.Event()
    threading.Thread(target=_report, args=(dl, store, cfg, stop), daemon=True, name="report").start()
    monitor = attach_monitor(lambda: task_snapshot("curl", dl.db.counts(), 0, workers)) if attach_monitor else None
    t0 = time.monotonic()
    try:
        dl.run(stop)
    except KeyboardInterrupt:
        logger.info("中断：结果已写回，没做完的块已交还，可重跑继续")
    finally:
        stop.set()
        if producer is not None:
            producer.shutdown(cfg.pass_timeout_s + 30)
        if monitor is not None:
            monitor.close()
        db.close()
    s = dl.stats
    logger.info("结束: 用时 %.1f 分钟，success %d，non_pdf %d，failed %d，用身份 %d 个，丢弃身份 %d 次，"
                "URL 被单独挑战 %d 次", (time.monotonic() - t0) / 60, s.success,
                s.non_pdf, s.failed, s.identities, s.rejected, s.challenged)


def _report(dl: Downloader, store: IdentityStore, cfg: Limits, stop: threading.Event) -> None:
    """每 30s 一行。速度按"已处理"（成功 + non_pdf + failed）算：non_pdf 成片时只看成功数会误以为变慢。
    成功含 PDF 任务和网页任务（url_type=html 存 .html），文案不写 PDF。"""
    s = dl.stats
    last_done, last_pdf, last_t = 0, 0, time.monotonic()
    while not stop.wait(30):
        st = store.stats(dl.host, cfg.refill_before_s)
        c = dl.db.counts()
        now = time.monotonic()
        done = s.success + s.non_pdf + s.failed
        minutes = (now - last_t) / 60
        logger.info("进度: 已处理 %d（成功 %d，non_pdf %d，失败 %d），待下载 %d | 身份 空闲 %d 占用 %d "
                    "| 近 30s 处理 %.0f 条/分钟，其中成功 %.0f 篇/分钟",
                    done, s.success, s.non_pdf, s.failed, c[Status.PENDING] + c[Status.RUNNING],
                    st.ready, st.inuse, (done - last_done) / minutes, (s.success - last_pdf) / minutes)
        last_done, last_pdf, last_t = done, s.success, now
