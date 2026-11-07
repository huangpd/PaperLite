#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/browser_pool.py
───────────────────────────
浏览器下载模式：每个 worker = 一个独立 CloakBrowser + 一个独立出口 IP。
过盾一次后浏览器保持打开，在同一浏览器里连续下同域名多篇，每篇之间按 page_interval_s 随机间隔
（默认 10–30s，可配）以避开同域名频率限制。IP 临近过期就关掉浏览器、换新 IP 重开。

适用于 ScienceDirect 这类"每个请求都重新挑战、cf_clearance 无法交给 curl 复用"的站：
请求必须整篇留在浏览器里。不走 Redis 身份池，也不用 curl —— 浏览器本身就是 worker。

只处理 url_type=html 的任务：整页存为 .html。worker 数就是同时打开的浏览器数，受 max_browsers 约束。
"""

import asyncio
import logging
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from paperlite.browser import BrowserSession
from paperlite.config import Limits
from paperlite.inspect import WindowInspector
from paperlite.logutil import log_event
from paperlite.proxy import IpSource, Lease, ProxySettings, ProxyUnavailable
from paperlite.tasks import Status, Task, TaskDB, UrlType, pdf_path

logger = logging.getLogger(__name__)

_NO_IP_WAIT_S = 5.0


@dataclass
class BrowserStats:
    success: int = 0
    failed: int = 0
    pages: int = 0           # 成功保存的页数（= success）
    browsers: int = 0        # 累计（重新）过盾启动的浏览器数

    def add(self, name: str) -> None:
        setattr(self, name, getattr(self, name) + 1)


class BrowserPool:
    """N 个 worker 协程并发，每个 worker 独占一个 浏览器+IP，串行领取任务下载。"""

    def __init__(self, cfg: Limits, proxy: Optional[ProxySettings], db: TaskDB, out: Path, workers: int,
                 no_proxy: bool = False) -> None:
        if workers > cfg.max_browsers:
            logger.info("worker 数 %d 超过 max_browsers %d，按 %d 运行", workers, cfg.max_browsers, cfg.max_browsers)
            workers = cfg.max_browsers
        self.cfg = cfg
        self.proxy = proxy
        self.db = db
        self.out = out
        self.no_proxy = no_proxy                       # 直连本机 IP，不取代理、不轮换 IP
        self.workers = max(1, workers)
        self.ips = None if no_proxy else IpSource(proxy)
        self.hosts = db.hosts()
        if not self.hosts:
            raise ValueError("任务库里没有任务，无法确定主机")
        self.host = db.host() or self.hosts[0]      # 热身落地页用第一个主机；其它主机的页面在首次渲染时各自过盾
        self.landing = cfg.landing_for(self.host)
        self.stats = BrowserStats()
        self._drained = threading.Event()      # 所有任务领完
        self.inspector = (WindowInspector(db, cfg.title_dup_trigger, cfg.rule_min_precision,
                                          cfg.rule_min_support, cfg.title_window_size)
                          if cfg.title_dup_trigger else None)

    def run(self, stop: Optional[threading.Event] = None) -> BrowserStats:
        asyncio.run(self._run(stop or threading.Event()))
        return self.stats

    async def _run(self, stop: threading.Event) -> None:
        logger.info("浏览器下载模式：主机 %s%s，热身落地页 %s，浏览器 %d%s，单篇间隔 %.0f–%.0fs",
                    "、".join(self.hosts[:3]), " 等" if len(self.hosts) > 3 else "", self.landing, self.workers,
                    "（本机直连）" if self.no_proxy else "", *self.cfg.page_interval_s)
        tasks = [asyncio.create_task(self._worker(i, stop)) for i in range(self.workers)]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _worker(self, n: int, stop: threading.Event) -> None:
        name = f"browser-{n}"
        session: Optional[BrowserSession] = None
        lease: Optional[Lease] = None
        try:
            while not stop.is_set() and not self._drained.is_set():
                # 没有浏览器，或 IP 快到期：换新 IP 重开
                if session is None or lease is None or lease.left() < self.cfg.min_left_s:
                    if session is not None:
                        await session.close()
                    session, lease = await self._new_session(stop)
                    if session is None:
                        continue                       # 停了或一直取不到 IP
                task = await asyncio.to_thread(self.db.claim, name)
                if task is None:
                    self._drained.set()                # 没任务了，通知其它 worker 收工
                    break
                try:
                    await self._do_task(task, session, lease, name)
                except Exception:
                    logger.exception("%s 处理任务异常，放回 pending", name)
                    await asyncio.to_thread(self.db.retry_later, task, count=False,
                                            error_kind="challenge_failed", error_message="浏览器 worker 异常")
                    session = await self._discard(session)      # 浏览器可能已坏，下轮重开
                    continue
                lo, hi = self.cfg.page_interval_s
                await asyncio.sleep(random.uniform(lo, hi))     # 同域名限速
        finally:
            if session is not None:
                await session.close()

    async def _discard(self, session: Optional[BrowserSession]) -> None:
        if session is not None:
            await session.close()
        return None

    async def _new_session(self, stop: threading.Event):
        """取一个寿命够长的新 IP → 起浏览器 → 过落地页的盾热身一次。失败则换 IP 重试。
        no_proxy：用本机 IP，不取代理、不轮换；过盾失败重试几次仍不成就放弃（没别的 IP 可换）。"""
        tries = 0
        while not stop.is_set() and not self._drained.is_set():
            if self.no_proxy:
                tries += 1
                if tries > 3:
                    logger.warning("直连本机 IP 连续 %d 次过盾失败，放弃该 worker", tries - 1)
                    return None, None
                lease = Lease(server="", exit_ip="local", area="", expires_at=time.time() + 10 ** 9)
                proxy_arg = None
            else:
                try:
                    lease = await asyncio.to_thread(self.ips.extract_checked)
                except ProxyUnavailable as e:
                    logger.warning("提取 IP 失败: %s", e)
                    await asyncio.sleep(_NO_IP_WAIT_S)
                    continue
                if lease.left() < self.cfg.min_left_s + self.cfg.pass_timeout_s:
                    logger.info("IP %s 剩余寿命太短，丢弃", lease.exit_ip)
                    continue
                proxy_arg = self.proxy.browser_proxy_for(lease.server)
            session = BrowserSession(proxy_arg, self.cfg.headed)
            try:
                await session.start()
                r = await session.render(self.landing, self.cfg.pass_timeout_s)
            except Exception as e:
                logger.error("浏览器启动/过盾异常", extra={
                    "event": "browser_start_failed",
                    "exit_ip": lease.exit_ip,
                    "error_type": type(e).__name__,
                    "error": str(e),
                }, exc_info=True)
                await session.close()
                continue
            if not r.ok:
                logger.info("过盾落地页失败 ip=%s %.0fs: %s", lease.exit_ip, r.seconds, r.error)
                await session.close()
                continue
            self.stats.add("browsers")
            if self.no_proxy:
                logger.info("浏览器就绪（本机直连）过盾 %.0fs（点击 %d）", r.seconds, r.clicks)
            else:
                logger.info("浏览器就绪 ip=%s 过盾 %.0fs（点击 %d），身份寿命 %.0fs",
                            lease.exit_ip, r.seconds, r.clicks, lease.left())
            return session, lease
        return None, None

    async def _observe(self, task: Task, r) -> None:
        """把真正加载出来的页面标题喂给巡检器——成功页和 404 等失效页都算（DOI 唯一却同标题
        ≈ 整批失效或整批封面）。只排除 r.ok=False 的挑战/超时页（标题是瞬时的 'Just a moment'）。"""
        if self.inspector is not None and r.title:
            await asyncio.to_thread(self.inspector.observe, task.doi, task.url, r.title)

    async def _do_task(self, task: Task, session: BrowserSession, lease: Lease, name: str) -> None:
        t0 = time.monotonic()
        r = await session.render(task.url, self.cfg.pass_timeout_s)
        elapsed = int((time.monotonic() - t0) * 1000)
        if not r.ok:
            failed = await asyncio.to_thread(
                self.db.challenged, task, self.cfg.max_url_challenges, lease.exit_ip,
                f"浏览器打开失败: {r.error}")
            if failed:
                self.stats.add("failed")      # 满次数判死；否则放回 pending 等后面重试，不计数
            logger.info("%s 渲染失败（第 %d 次%s）%s: %s", name, task.challenge_count + 1,
                        "，判失败" if failed else "，稍后重试", task.url, r.error)
            return

        code = r.status or 200
        dest = pdf_path(self.out, task.doi, ".html")
        if code >= 400:
            kind = "not_found" if code in (404, 410) else None
            await asyncio.to_thread(self.db.finish, task, Status.FAILED, http_status=code, elapsed_ms=elapsed,
                                    exit_ip=lease.exit_ip, error_kind=kind, error_message=f"HTTP {code}",
                                    final_url=r.url or None, title=r.title or None)
            await self._observe(task, r)      # 失效页也进巡检：整批 404 同标题可归纳规则、跳过其余 pending
            self.stats.add("failed")
            logger.info("网页 HTTP %d %s %s", code, task.url, r.title)
            return

        body = r.html.encode("utf-8")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            tmp.write_bytes(body)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
        await asyncio.to_thread(self.db.finish, task, Status.SUCCESS, http_status=code, file_path=str(dest),
                                file_size=len(body), elapsed_ms=elapsed, exit_ip=lease.exit_ip,
                                title=r.title or None, final_url=r.url or None)
        await self._observe(task, r)
        self.stats.add("success")
        self.stats.add("pages")
        log_event(logger, logging.INFO, "page_saved", "页面保存成功",
                  doi=task.doi, target=r.url.rsplit("/", 1)[-1][:40] or r.url,
                  seconds=round(r.seconds, 1), clicks=r.clicks)
