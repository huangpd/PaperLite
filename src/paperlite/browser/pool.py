#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/browser/pool.py
─────────────────────────
浏览器下载模式：每个 worker = 一个独立 CloakBrowser + 一个独立出口 IP。
浏览器启动后不热身，直接打开领到的第一篇（探针）：打开了就说明这个 IP 能用，之后在同一浏览器里连续下多篇，
每篇之间按 page_interval_s 随机间隔（默认 10–30s）以避开同域名频率限制。IP 临近过期就关掉浏览器、换新 IP 重开。

探针失败算谁的：新浏览器第一篇就打不开，多半是 IP 的问题——任务原样放回、不计次数，换 IP。
但整站打不开时每个新 IP 都会失败：同一主机连续 _PROBE_STREAK 个新浏览器首篇都失败，就认定是站点的问题，
之后按普通失败计次（与有热身时一样），不再为它换 IP；该主机任何一篇成功就清零。
不走代理时规则相同，"换 IP"就是在本机 IP 上重开浏览器。

适用于 ScienceDirect 这类"每个请求都重新挑战、cf_clearance 无法交给 curl 复用"的站：
请求必须整篇留在浏览器里。不走身份池，也不用 curl —— 浏览器本身就是 worker。

url_type=html 的任务整页存成 .html。url_type=pdf 的任务开 CDP 拦截（pdf_capture.py）：途中任一响应是 PDF
就流式写盘（校验 %PDF-、大小、长度）记 success；最后停在网页上（摘要页、订阅页）记 non_pdf
（error_kind 按 core/page.py 分类），网页存到 <out>/non_pdf/，与 curl 模式一致。worker 数就是同时打开的浏览器数，受 max_browsers 约束。
"""

import asyncio
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from paperlite.browser.session import BrowserSession
from paperlite.core.page import classify_html
from paperlite.config import Limits, ProxySettings
from paperlite.taskq.title_dup import WindowInspector
from paperlite.logutil import log_event
from paperlite.monitor import reporter as mon
from paperlite.proxy import IpSource, Lease, ProxyUnavailable
from paperlite.taskq import PgTaskSource
from paperlite.core.tasks import Status, Task, UrlType, pdf_path

logger = logging.getLogger(__name__)

# 代理链路断了（IP 中途失效）：不是页面/站点的问题。这类错误不计挑战次数，连续 _NET_DISCARD_AFTER 次就换 IP，
# 不再拿死 IP 一直撞到寿命结束。
_PROXY_NET_ERR = re.compile(r"net::ERR_(TUNNEL_CONNECTION_FAILED|PROXY_[A-Z_]+|CONNECTION_(CLOSED|RESET|REFUSED)|EMPTY_RESPONSE)")
_NET_DISCARD_AFTER = 2
_PROBE_STREAK = 3          # 同一主机连续这么多个新浏览器首篇失败 → 认定是站点问题，不再算 IP 的


def is_proxy_net_error(error: str) -> bool:
    return bool(_PROXY_NET_ERR.search(error or ""))

_NO_IP_WAIT_S = 5.0


@dataclass
class BrowserStats:
    success: int = 0
    failed: int = 0
    non_pdf: int = 0         # 要 PDF 却打开的是网页
    pages: int = 0           # 成功保存的页数（= success）
    browsers: int = 0        # 首篇打开成功（验证可用）的浏览器数
    probe_failed: int = 0    # 新浏览器首篇失败、换掉 IP 的次数

    def add(self, name: str) -> None:
        setattr(self, name, getattr(self, name) + 1)


class BrowserPool:
    """N 个 worker 协程并发，每个 worker 独占一个 浏览器+IP，串行领取任务下载。"""

    def __init__(self, cfg: Limits, proxy: Optional[ProxySettings], db: PgTaskSource, out: Path, workers: int,
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
            raise ValueError("任务队列里没有任务，无法确定主机")
        self.host = db.host() or self.hosts[0]      # 任务最多的主机：只用于日志 / 监控里的主机标签
        self._probe_streak: dict[str, int] = {}       # 主机 → 连续首篇失败的新浏览器数
        self.stats = BrowserStats()
        self._drained = threading.Event()      # 所有任务领完
        self._live: set[str] = set()           # 当前开着浏览器的 worker（监控：在线浏览器数）
        # db 是 taskq.PgTaskSource：巡检只跳本机领到的块
        self.inspector = (WindowInspector(db, cfg.title_dup_trigger, cfg.rule_min_precision,
                                          cfg.rule_min_support, cfg.title_window_size)
                          if cfg.title_dup_trigger else None)

    def online(self) -> int:
        """当前开着（已过盾可用）的浏览器数。"""
        return len(self._live)

    def run(self, stop: Optional[threading.Event] = None) -> BrowserStats:
        asyncio.run(self._run(stop or threading.Event()))
        return self.stats

    async def _run(self, stop: threading.Event) -> None:
        logger.info("浏览器下载模式：主机 %s%s，浏览器 %d%s，单篇间隔 %.0f–%.0fs（不热身，首篇即探针）",
                    "、".join(self.hosts[:3]), " 等" if len(self.hosts) > 3 else "", self.workers,
                    "（本机直连）" if self.no_proxy else "", *self.cfg.page_interval_s)
        tasks = [asyncio.create_task(self._worker(i, stop)) for i in range(self.workers)]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _worker(self, n: int, stop: threading.Event) -> None:
        name = f"browser-{n}"
        session: Optional[BrowserSession] = None
        lease: Optional[Lease] = None
        net_errors = 0                                      # 当前 IP 连续代理网络错误次数
        fresh = False                                       # 新浏览器还没打开过任何一篇：下一篇是探针
        try:
            while not stop.is_set() and not self._drained.is_set():
                # 没有浏览器，或 IP 快到期：换新 IP 重开
                if (session is None or lease is None or lease.left() < self.cfg.min_left_s
                        or net_errors >= _NET_DISCARD_AFTER):
                    if net_errors >= _NET_DISCARD_AFTER and lease is not None:
                        logger.warning("%s IP %s 连续 %d 次代理网络错误，提前换 IP（剩余寿命 %.0fs）",
                                       name, lease.exit_ip or lease.server, net_errors, lease.left())
                    net_errors = 0
                    if session is not None:
                        self._live.discard(name)
                        await session.close()
                    session, lease = await self._new_session(stop)
                    if session is None:
                        continue                       # 停了或一直取不到 IP
                    fresh = True
                task = await asyncio.to_thread(self.db.claim, name)
                if task is None:
                    self._drained.set()                # 没任务了，通知其它 worker 收工
                    break
                try:
                    outcome = await self._do_task(task, session, lease, name, probe=fresh)
                except Exception:
                    logger.exception("%s 处理任务异常，放回 pending", name)
                    await asyncio.to_thread(self.db.retry_later, task, count=False,
                                            error_kind="challenge_failed", error_message="浏览器 worker 异常")
                    self._live.discard(name)
                    session = await self._discard(session)      # 浏览器可能已坏，下轮重开
                    continue
                if outcome == "probe_failed":
                    self._live.discard(name)
                    session = await self._discard(session)      # 换 IP（不走代理时就是重开浏览器）
                    continue
                if fresh:
                    fresh = False
                    self._live.add(name)
                net_errors = net_errors + 1 if outcome == "net" else 0
                lo, hi = self.cfg.page_interval_s
                await asyncio.sleep(random.uniform(lo, hi))     # 同域名限速
        finally:
            self._live.discard(name)
            if session is not None:
                await session.close()

    async def _discard(self, session: Optional[BrowserSession]) -> None:
        if session is not None:
            await session.close()
        return None

    async def _new_session(self, stop: threading.Event):
        """取一个寿命够长的新 IP → 起浏览器（不热身：领到的第一篇就是探针，见 _do_task）。启动失败则换 IP 重试。
        no_proxy：用本机 IP，不取代理、不轮换；启动失败重试几次仍不成就放弃。"""
        tries = 0
        while not stop.is_set() and not self._drained.is_set():
            if self.no_proxy:
                tries += 1
                if tries > 3:
                    logger.warning("直连本机 IP 连续 %d 次启动浏览器失败，放弃该 worker", tries - 1)
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
            session = BrowserSession(proxy_arg, self.cfg.headed, self.cfg.geoip, self.cfg.webrtc_spoof)
            try:
                await session.start()
            except Exception as e:
                mon.browser_start(False, "exception", host=self.host, error=f"{type(e).__name__}: {e}")
                logger.error("浏览器启动异常", extra={
                    "event": "browser_start_failed",
                    "exit_ip": lease.exit_ip,
                    "error_type": type(e).__name__,
                    "error": str(e),
                }, exc_info=True)
                await session.close()
                continue
            return session, lease
        return None, None

    async def _observe(self, task: Task, r) -> None:
        """把真正加载出来的页面标题喂给巡检器——成功页和 404 等失效页都算（DOI 唯一却同标题
        ≈ 整批失效或整批封面）。只排除 r.ok=False 的挑战/超时页（标题是瞬时的 'Just a moment'）。"""
        if self.inspector is not None and r.title:
            await asyncio.to_thread(self.inspector.observe, task.doi, task.url, r.title)

    async def _do_task(self, task: Task, session: BrowserSession, lease: Lease, name: str,
                       probe: bool = False) -> str:
        """下载一篇。返回 "ok"（含 HTTP 错误等已记结果的情况）、"net"（代理网络错误，IP 可能已失效）
        或 "probe_failed"（新浏览器首篇失败，归咎于 IP：任务已放回、不计次数，worker 应换 IP）。"""
        t0 = time.monotonic()
        want_pdf = task.url_type == UrlType.PDF
        r = await session.render(task.url, self.cfg.pass_timeout_s,
                                 pdf_dest=pdf_path(self.out, task.doi) if want_pdf else None,
                                 max_bytes=self.cfg.max_file_size_mb * 1024 * 1024, min_bytes=self.cfg.min_file_size,
                                 stall_s=self.cfg.download_timeout_s)
        elapsed = int((time.monotonic() - t0) * 1000)
        host = urlsplit(task.url).hostname or ""
        code = r.status or 200
        mon.page(host, passed=r.ok, ok=r.ok and code < 400, status=code if r.ok else None, waf=r.waf,
                 elapsed_ms=elapsed, error="" if r.ok else r.error)
        if probe:
            if r.ok:                                   # 页面打开了（不管 HTTP 状态）：这个浏览器 + IP 可用
                self._probe_streak[host] = 0
                self.stats.add("browsers")
                mon.browser_start(True, "ok", host=host, waf=r.waf, seconds=r.seconds)
                logger.info("%s 浏览器就绪 %s 首篇过盾 %.0fs（点击 %d）%s", name,
                            "本机直连" if self.no_proxy else f"ip={lease.exit_ip}", r.seconds, r.clicks,
                            "" if self.no_proxy else f"，身份寿命 {lease.left():.0f}s")
            elif self._probe_streak.get(host, 0) < _PROBE_STREAK:
                self._probe_streak[host] = self._probe_streak.get(host, 0) + 1
                self.stats.add("probe_failed")
                mon.browser_start(False, "challenge_failed", host=host, waf=r.waf, seconds=r.seconds, error=r.error)
                await asyncio.to_thread(self.db.retry_later, task, count=False,
                                        error_message=f"新浏览器首篇失败（换 IP，不计次数）: {r.error}"[:500],
                                        exit_ip=lease.exit_ip or None)
                logger.info("%s 新浏览器首篇失败，换 IP、任务放回不计次数 %s: %s", name, task.url, r.error)
                return "probe_failed"
            else:
                logger.info("%s %s 已连续 %d 个新浏览器首篇失败，按站点问题计次，不再换 IP",
                            name, host, self._probe_streak[host])
        if not r.ok and is_proxy_net_error(r.error):
            # IP 的问题不是这篇的问题：放回 pending，不计挑战/重试次数
            await asyncio.to_thread(self.db.retry_later, task, count=False, error_kind="network",
                                    error_message=f"代理网络错误: {r.error}"[:500], exit_ip=lease.exit_ip or None)
            logger.info("%s 代理网络错误，任务放回 %s: %s", name, task.url, r.error)
            return "net"
        if not r.ok:
            failed = await asyncio.to_thread(
                self.db.challenged, task, self.cfg.max_url_challenges, lease.exit_ip,
                f"浏览器打开失败: {r.error}")
            if failed:
                self.stats.add("failed")      # 满次数判死；否则放回 pending 等后面重试，不计数
            logger.info("%s 渲染失败（第 %d 次%s）%s: %s", name, task.challenge_count + 1,
                        "，判失败" if failed else "，稍后重试", task.url, r.error)
            return "ok"

        self._probe_streak[host] = 0                   # 页面打开了：这个站点能访问

        dest = pdf_path(self.out, task.doi, ".html")
        if code >= 400:
            kind = "not_found" if code in (404, 410) else None
            await asyncio.to_thread(self.db.finish, task, Status.FAILED, http_status=code, elapsed_ms=elapsed,
                                    exit_ip=lease.exit_ip, error_kind=kind, error_message=f"HTTP {code}",
                                    final_url=r.url or None, title=r.title or None)
            await self._observe(task, r)      # 失效页也进巡检：整批 404 同标题可归纳规则、跳过其余 pending
            self.stats.add("failed")
            logger.info("网页 HTTP %d %s %s", code, task.url, r.title)
            return "ok"

        if r.pdf is not None:                          # 截到 PDF（流式写盘、已校验）
            await asyncio.to_thread(self.db.finish, task, Status.SUCCESS, http_status=r.status,
                                    file_path=str(r.pdf.path), file_size=r.pdf.size, elapsed_ms=elapsed,
                                    exit_ip=lease.exit_ip, final_url=r.pdf.final_url or None)
            self.stats.add("success")
            log_event(logger, logging.INFO, "pdf_saved", "PDF 保存成功", doi=task.doi, size=r.pdf.size,
                      seconds=round(r.seconds, 1), clicks=r.clicks)
            return "ok"

        if want_pdf and r.pdf_error:
            # 截到了 PDF 响应但校验没过（过小、过大、不完整、其实不是 PDF）：按网络/提取错误重试，用完次数判死
            if task.retry_count + 1 < self.cfg.max_tries and "超过大小上限" not in r.pdf_error:
                await asyncio.to_thread(self.db.retry_later, task, count=True, error_kind="extract_failed",
                                        error_message=r.pdf_error[:500], exit_ip=lease.exit_ip or None)
                logger.info("%s PDF 校验没过，稍后重试 %s: %s", name, task.url, r.pdf_error)
            else:
                await asyncio.to_thread(self.db.finish, task, Status.FAILED, http_status=code, elapsed_ms=elapsed,
                                        exit_ip=lease.exit_ip, error_kind="extract_failed",
                                        error_message=r.pdf_error[:500], final_url=r.url or None)
                self.stats.add("failed")
                logger.info("%s PDF 校验没过，判失败 %s: %s", name, task.url, r.pdf_error)
            return "ok"

        if want_pdf:
            # 要 PDF 却停在网页上（摘要页、订阅页……）：记 non_pdf，不记成功；网页留在 non_pdf/ 里备查
            kind = classify_html(r.html)
            body = r.html.encode("utf-8")
            keep = self.out / "non_pdf" / dest.name
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.write_bytes(body)
            await asyncio.to_thread(self.db.finish, task, Status.NON_PDF, http_status=code, file_size=len(body),
                                    elapsed_ms=elapsed, exit_ip=lease.exit_ip, error_kind=kind,
                                    title=r.title or None, final_url=r.url or None)
            await self._observe(task, r)
            self.stats.add("non_pdf")
            logger.info("non_pdf %s %s %s", kind, task.url, r.title)
            return "ok"

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
        return "ok"
