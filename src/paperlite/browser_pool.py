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
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from paperlite.browser import BrowserSession, RenderResult
from paperlite.config import Limits
from paperlite.inspect import WindowInspector
from paperlite.logutil import log_event
from paperlite.monitor import reporter as mon
from paperlite.proxy import IpSource, Lease, ProxySettings, ProxyUnavailable
from paperlite.tasks import Status, Task, TaskDB, UrlType, pdf_path

logger = logging.getLogger(__name__)

# 代理链路断了（IP 中途失效）：不是页面/站点的问题。这类错误不计挑战次数，连续 _NET_DISCARD_AFTER 次就换 IP，
# 不再拿死 IP 一直撞到寿命结束。
_PROXY_NET_ERR = re.compile(r"net::ERR_(TUNNEL_CONNECTION_FAILED|PROXY_[A-Z_]+|CONNECTION_(CLOSED|RESET|REFUSED)|EMPTY_RESPONSE)")
_NET_DISCARD_AFTER = 2


def is_proxy_net_error(error: str) -> bool:
    return bool(_PROXY_NET_ERR.search(error or ""))

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
                 no_proxy: bool = False, rules: Optional[str] = None) -> None:
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
        self._live: set[str] = set()           # 当前开着浏览器的 worker（监控：在线浏览器数）
        self.inspector = (WindowInspector(db, cfg.title_dup_trigger, cfg.rule_min_precision,
                                          cfg.rule_min_support, cfg.title_window_size)
                          if cfg.title_dup_trigger else None)
        self.rules_path = rules
        self.routes: dict[str, str] = {}
        if self.rules_path and Path(self.rules_path).exists():
            from paperlite.preprocess.store import PreprocessStore
            self.routes = PreprocessStore.load_route_cache(self.rules_path)
            logger.info("⚡️ 自适应模式就绪：已加载 %d 条出版商域名路由规则", len(self.routes))

    def online(self) -> int:
        """当前开着（已过盾可用）的浏览器数。"""
        return len(self._live)

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
        net_errors = 0                                      # 当前 IP 连续代理网络错误次数
        pending_chunk: list[Task] = []                     # 当前 Worker 本地持有的切片缓冲区
        try:
            while not stop.is_set() and not self._drained.is_set():
                # 没有浏览器，或 IP 快到期：换新 IP 重开
                if (session is None or lease is None or lease.left() < self.cfg.min_left_s
                        or net_errors >= _NET_DISCARD_AFTER):
                    # 换 IP / 重开浏览器前，将本地尚未执行的切片任务瞬间放回待下载池
                    if pending_chunk:
                        released = await asyncio.to_thread(self.db.release_tasks, pending_chunk)
                        if released:
                            logger.info("%s 换 IP 前将 %d 个未消费任务安全放回待下载池", name, released)
                        pending_chunk.clear()

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
                    self._live.add(name)

                # 本地切片为空时，以浏览器/IP当前生命周期为准，动态认领切片大小
                if not pending_chunk:
                    left_s = lease.left()
                    if left_s < self.cfg.min_left_s:
                        continue                       # 寿命不足跑新切片，触发下轮换 IP

                    # 若启用了巡检实时归纳 (title_dup_trigger)，单篇认领以便规则实时拦截；
                    # 否则动态适配生命周期批量认领切片（按每篇 8s 估算），上限 20 篇，下限 1 篇
                    if self.cfg.title_dup_trigger:
                        batch_size = 1
                    else:
                        batch_size = min(20, max(1, int((left_s - self.cfg.min_left_s) / 8)))
                    pending_chunk = await asyncio.to_thread(self.db.claim_chunk, name, batch_size)
                    if not pending_chunk:
                        # 兜底：尝试单篇认领
                        single_task = await asyncio.to_thread(self.db.claim, name)
                        if single_task:
                            pending_chunk = [single_task]
                        else:
                            has_more = await asyncio.to_thread(self.db.has_claimable)
                            running = await asyncio.to_thread(self.db.running_count)
                            if not has_more and running == 0:
                                self._drained.set()
                                break
                            elif not has_more:
                                # 无可认领任务，但尚有 worker 在运行任务，等待可能异常放回的任务或顺利收尾
                                await asyncio.sleep(2)
                                continue
                            else:
                                await asyncio.sleep(0.5)
                                continue

                task = pending_chunk.pop(0)
                try:
                    net_err = await self._do_task(task, session, lease, name)
                    net_errors = net_errors + 1 if net_err else 0
                except Exception:
                    logger.exception("%s 处理任务异常，放回 pending", name)
                    await asyncio.to_thread(self.db.retry_later, task, count=False,
                                            error_kind="challenge_failed", error_message="浏览器 worker 异常")
                    if pending_chunk:
                        await asyncio.to_thread(self.db.release_tasks, pending_chunk)
                        pending_chunk.clear()
                    self._live.discard(name)
                    session = await self._discard(session)      # 浏览器可能已坏，下轮重开
                    continue
                lo, hi = self.cfg.page_interval_s
                await asyncio.sleep(random.uniform(lo, hi))     # 同域名限速
        finally:
            if pending_chunk:
                await asyncio.to_thread(self.db.release_tasks, pending_chunk)
                pending_chunk.clear()
            self._live.discard(name)
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
            session = BrowserSession(proxy_arg, self.cfg.headed, self.cfg.geoip, self.cfg.webrtc_spoof)
            try:
                await session.start()
                r = await session.render(self.landing, self.cfg.pass_timeout_s)
            except Exception as e:
                mon.browser_start(False, "exception", host=self.host, error=f"{type(e).__name__}: {e}")
                logger.error("浏览器启动/过盾异常", extra={
                    "event": "browser_start_failed",
                    "exit_ip": lease.exit_ip,
                    "error_type": type(e).__name__,
                    "error": str(e),
                }, exc_info=True)
                await session.close()
                continue
            if not r.ok:
                mon.browser_start(False, "challenge_failed", host=self.host, waf=r.waf, seconds=r.seconds,
                                  error=r.error)
                logger.info("过盾落地页失败 ip=%s %.0fs: %s", lease.exit_ip, r.seconds, r.error)
                await session.close()
                continue
            self.stats.add("browsers")
            mon.browser_start(True, "ok", host=self.host, waf=r.waf, seconds=r.seconds)
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

    def _resolve_task_host(self, task: Task) -> str:
        """从数据库映射表或 URL 提取真实落地域名"""
        try:
            from paperlite.preprocess.cluster import PatternExtractor
            _, pk = PatternExtractor.extract(task.doi)
            with self.db._lock:
                row = self.db._conn.execute(
                    "SELECT real_host FROM doi_domain_routes WHERE pattern_key = ?", (pk,)
                ).fetchone()
                if row and row[0]:
                    return row[0].lower()
        except Exception:
            pass
        return (urlsplit(task.url).hostname or "").lower()

    async def _fetch_curl_direct(self, url: str, lease: Lease) -> RenderResult:
        """对判定为 curl_direct 的站点执行秒级 cURL 抓取，免除启动/渲染无头浏览器的开销"""
        import re
        from curl_cffi.requests import AsyncSession
        t0 = time.monotonic()
        proxy_url = None
        if not self.no_proxy and lease and lease.server and self.proxy:
            proxy_url = self.proxy.url_for(lease.server)
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        try:
            async with AsyncSession(impersonate="chrome120") as s:
                resp = await s.get(url, proxies=proxies, timeout=20, allow_redirects=True)
                m = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.I | re.S)
                title = m.group(1).strip() if m else ""
                elapsed = round(time.monotonic() - t0, 1)
                return RenderResult(
                    ok=resp.status_code < 400,
                    seconds=elapsed,
                    url=str(resp.url),
                    status=resp.status_code,
                    title=title,
                    html=resp.text,
                    clicks=0,
                    waf="none",
                )
        except Exception as e:
            return RenderResult(ok=False, seconds=round(time.monotonic() - t0, 1), error=str(e))

    async def _do_task(self, task: Task, session: BrowserSession, lease: Lease, name: str) -> bool:
        """下载一篇；返回 True 表示失败原因是代理网络错误（IP 可能已失效），由 worker 决定是否换 IP。"""
        t0 = time.monotonic()

        # 自适应路由分流
        target_host = self._resolve_task_host(task)
        mode = self.routes.get(target_host, "browser_only") if self.routes else "browser_only"

        if mode == "curl_direct":
            logger.info("%s ⚡️ [AUTO 分流] %s (%s) 命中 curl_direct 模式，采用轻量 cURL 直下", name, task.doi, target_host)
            r = await self._fetch_curl_direct(task.url, lease)
        else:
            r = await session.render(task.url, self.cfg.pass_timeout_s)

        elapsed = int((time.monotonic() - t0) * 1000)
        host = urlsplit(task.url).hostname or ""
        code = r.status or 200
        mon.page(host, passed=r.ok, ok=r.ok and code < 400, status=code if r.ok else None, waf=r.waf,
                 elapsed_ms=elapsed, error="" if r.ok else r.error)
        if not r.ok and is_proxy_net_error(r.error):
            # IP 的问题不是这篇的问题：放回 pending，不计挑战/重试次数
            await asyncio.to_thread(self.db.retry_later, task, count=False, error_kind="network",
                                    error_message=f"代理网络错误: {r.error}"[:500], exit_ip=lease.exit_ip or None)
            logger.info("%s 代理网络错误，任务放回 %s: %s", name, task.url, r.error)
            return True
        if not r.ok:
            failed = await asyncio.to_thread(
                self.db.challenged, task, self.cfg.max_url_challenges, lease.exit_ip,
                f"浏览器打开失败: {r.error}")
            if failed:
                self.stats.add("failed")      # 满次数判死；否则放回 pending 等后面重试，不计数
            logger.info("%s 渲染失败（第 %d 次%s）%s: %s", name, task.challenge_count + 1,
                        "，判失败" if failed else "，稍后重试", task.url, r.error)
            return

        dest = pdf_path(self.out, task.doi, ".html")
        if task.url_type == UrlType.PDF and not getattr(self, "_warned_pdf_type", False):
            self._warned_pdf_type = True
            logger.warning("⚠️ 任务 url_type 为 'pdf'，但 BrowserPool 浏览器渲染管线保存的是页面 HTML 快照 (.html)。若需要真实 PDF 二进制文件，请使用 curl 模式或在导入时显式指定 --type html")
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
