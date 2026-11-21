"""
paperlite/browser/runner.py
───────────────────────────
跑一次浏览器模式（命令行 run 的默认模式）：开浏览器池、每 30s 打一行进度、挂监控心跳、结束打汇总。
不用身份池。任务队列 db 交给这里，结束（包括出错）时由这里关闭。
"""

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from paperlite.browser.pool import BrowserPool
from paperlite.config import Limits, ProxySettings
from paperlite.errors import UserError
from paperlite.monitor.reporter import task_snapshot
from paperlite.taskq import PgTaskSource
from paperlite.rules.tasks import Status

logger = logging.getLogger("paperlite")

# 传入心跳快照函数，返回带 close() 的句柄；未启用监控时返回 None
AttachMonitor = Callable[[Callable[[], dict]], Optional[Any]]


def run_browser(cfg: Limits, proxy: Optional[ProxySettings], db: PgTaskSource, out: Path, workers: int, *,
                no_proxy: bool = False, attach_monitor: Optional[AttachMonitor] = None) -> None:
    """每个浏览器 + IP 过盾一次后连续下多篇，下载完返回；Ctrl+C / SIGTERM 时写回结果、交还没做完的块。"""
    try:
        pool = BrowserPool(cfg, proxy, db, out, workers, no_proxy=no_proxy)
    except ValueError as e:
        db.close()
        raise UserError(str(e)) from e
    c = db.counts()
    logger.info("输出目录 %s，任务队列 %s", out, db.path)
    logger.info("%s：待下载 %d，已成功 %d，失败 %d；浏览器 %d", pool.host, c[Status.PENDING],
                c[Status.SUCCESS], c[Status.FAILED], pool.workers)
    if not c[Status.PENDING]:
        db.close()
        return
    stop = threading.Event()
    threading.Thread(target=_report, args=(pool, stop), daemon=True, name="report").start()
    monitor = attach_monitor(lambda: task_snapshot("browser", pool.db.counts(), pool.online(), pool.workers)) \
        if attach_monitor else None
    t0 = time.monotonic()
    try:
        pool.run(stop)
    except KeyboardInterrupt:
        logger.info("中断：结果已写回，没做完的块已交还，可重跑继续")
    finally:
        stop.set()
        if monitor is not None:
            monitor.close()
        db.close()
    s = pool.stats
    logger.info("结束: 用时 %.1f 分钟，success %d，non_pdf %d，failed %d，启动浏览器 %d 个",
                (time.monotonic() - t0) / 60, s.success, s.non_pdf, s.failed, s.browsers)


def _report(pool: BrowserPool, stop: threading.Event) -> None:
    """每 30s 一行进度。"""
    s = pool.stats
    last_done, last_t = 0, time.monotonic()
    while not stop.wait(30):
        c = pool.db.counts()
        now = time.monotonic()
        done = s.success + s.non_pdf + s.failed
        rate = (done - last_done) / max((now - last_t) / 60, 1e-9)
        last_done, last_t = done, now
        logger.info("进度: 成功 %d non_pdf %d 失败 %d 待下载 %d running %d | 浏览器 %d | 近 30s %.0f 篇/分钟",
                    s.success, s.non_pdf, s.failed, c[Status.PENDING], c[Status.RUNNING], pool.workers, rate)
