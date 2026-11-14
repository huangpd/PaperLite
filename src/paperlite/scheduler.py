#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/scheduler.py
────────────────────────
PaperLite 自适应总调度引擎（Auto Scheduler）：
基于预处理生成的 domain_rules.toml 规则快照，将混合任务库按出版商反爬特征三路分流：

  1. curl_direct:
     - 纯静态无盾直通站点（如 link.springer.com、linkinghub.elsevier.com）；
     - 无需起浏览器、无需 Redis，直接采用轻量异步 cURL 秒级并发直下。

  2. curl_pool (下游消费驱动生产 / Pull-Demand 反压):
     - 过盾一次后 Cookie 可被 cURL 多次复用的站点（如 dl.acm.org、academic.oup.com、onlinelibrary.wiley.com）；
     - 下游 cURL 消费者 (Downloader) 实时计算待下载任务量与消费速率，动态向 Redis 发布需求水位 (Demand)；
     - 上游无头浏览器 (Producer) 严格受 Demand 驱动制备身份，达到水位即休眠，待办清空即停机，绝不浪费代理 IP；
     - 若当前未启动/配置 Redis，自动平滑降级至 BrowserPool 浏览器渲染。

  3. browser_only:
     - 强风控行为检测 / SPA 前端假空壳 / 每次请求单独质询的站点（如 cell.com、ajog.org、sciencedirect.com）；
     - 由 BrowserPool 独占无头浏览器环境连续渲染与自动解盾下载。
"""

import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from paperlite.browser import RenderResult
from paperlite.browser_pool import BrowserPool
from paperlite.config import Limits
from paperlite.consumer import Downloader
from paperlite.preprocess.store import PreprocessStore
from paperlite.producer import Producer
from paperlite.proxy import IpSource, Lease, ProxySettings
from paperlite.store import IdentityStore
from paperlite.tasks import Status, Task, TaskDB, pdf_path

logger = logging.getLogger(__name__)


class _ProducerThread:
    """按主机隔离的局部生产者线程。"""

    def __init__(self, cfg: Limits, store: IdentityStore, proxy: Optional[ProxySettings], host: str) -> None:
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.host = host
        self._producer = Producer(cfg, store, IpSource(proxy) if proxy else None, host)
        threading.Thread(target=self._run, name=f"prod-{host}", daemon=True).start()

    def _run(self) -> None:
        try:
            asyncio.run(self._producer.run(self.stop))
        finally:
            self.finished.set()

    def shutdown(self, timeout_s: float = 30) -> None:
        self.stop.set()
        if not self.finished.wait(timeout_s):
            logger.warning("主机 %s 的生产者 %.0fs 内未完全退出", self.host, timeout_s)


class AutoScheduler:
    """自适应多管线混合调度器。"""

    def __init__(self, cfg: Limits, proxy: Optional[ProxySettings], db: TaskDB, out: Path,
                 workers: int, rules_path: str = "domain_rules.toml",
                 store: Optional[IdentityStore] = None, no_proxy: bool = False,
                 routes: Optional[dict[str, str]] = None) -> None:
        self.cfg = cfg
        self.proxy = proxy
        self.db = db
        self.out = out
        self.workers = max(1, workers)
        self.rules_path = rules_path
        self.no_proxy = no_proxy
        self.store = store
        self.routes: dict[str, str] = dict(routes) if routes is not None else {}
        if not self.routes and Path(self.rules_path).exists():
            self.routes = PreprocessStore.load_route_cache(self.rules_path)
        self._ip_source: Optional[IpSource] = IpSource(self.proxy) if (self.proxy and not self.no_proxy) else None
        self._direct_lease: Optional[Lease] = None
        self._lease_lock = threading.Lock()

    def _get_direct_proxy_url(self) -> Optional[str]:
        """为 curl_direct 维持一个可用的代理租约（若没有或快到期则从 IpSource 提取一次）。"""
        if self.no_proxy or not self.proxy or not self._ip_source:
            return None
        with self._lease_lock:
            now = time.time()
            if self._direct_lease is None or self._direct_lease.left(now) < 30:
                try:
                    self._direct_lease = self._ip_source.extract_checked()
                    logger.info("⚡️ [curl_direct] 提取代理成功: %s (出口 IP: %s, 寿命 %.0fs)",
                                self._direct_lease.server, self._direct_lease.exit_ip,
                                self._direct_lease.left())
                except Exception as e:
                    logger.warning("⚡️ [curl_direct] 提取代理失败 (%s)，降级为本机直连", e)
                    return None
            return self.proxy.url_for(self._direct_lease.server)

    def run(self, stop: Optional[threading.Event] = None) -> None:
        stop = stop or threading.Event()
        t0 = time.monotonic()

        # 1. 回填任务的真实落地域名 real_host
        backfilled = self.db.backfill_real_hosts()
        if backfilled:
            logger.info("⚡️ [AUTO 调度] 依据映射库为 %d 个任务回填了真实落地域名", backfilled)

        # 统计待下载任务分布
        by_host = self.db.pending_by_host()
        total_pending = sum(by_host.values())
        if total_pending == 0:
            logger.info("任务库无待下载任务")
            return

        logger.info("⚡️ [AUTO 调度] 启动自适应调度：待下载 %d 篇，分布在 %d 个独立域名",
                    total_pending, len(by_host))

        # 2. 管线一：curl_direct 纯静态秒级直出
        direct_hosts = [h for h in by_host if self.routes.get(h) == "curl_direct"]
        if direct_hosts and not stop.is_set():
            self._run_curl_direct(direct_hosts, stop)

        # 3. 管线二：curl_pool 下游拉动式身份池（需要过盾但 Cookie 可复用）
        pool_hosts = [h for h in by_host if self.routes.get(h) == "curl_pool"]
        fallback_to_browser: list[str] = []
        if pool_hosts and not stop.is_set():
            for host in pool_hosts:
                if stop.is_set():
                    break
                pending_count = self.db.count_pending_host(host)
                if pending_count <= 0:
                    continue
                ok = self._run_curl_pool_host(host, stop)
                if not ok:
                    fallback_to_browser.append(host)

        # 4. 管线三：browser_only 强风控无头浏览器独占渲染（包含所有剩余未采任务）
        if self.db.has_claimable() and not stop.is_set():
            rem_count = self.db.count_pending_host()
            logger.info("⚡️ [AUTO 调度] 启动 BrowserPool 浏览器渲染管线 (共 %d 篇待采)", rem_count)
            pool = BrowserPool(self.cfg, self.proxy, self.db, self.out, self.workers,
                               no_proxy=self.no_proxy, rules=self.rules_path)
            pool.routes.update(self.routes)
            pool.run(stop)

        c = self.db.counts()
        elapsed = (time.monotonic() - t0) / 60
        logger.info("⚡️ [AUTO 调度] 运行结束: 用时 %.1f 分钟，成功 %d 篇，失败 %d 篇，剩余待办 %d 篇",
                    elapsed, c[Status.SUCCESS], c[Status.FAILED], c[Status.PENDING])

    def _run_curl_direct(self, hosts: list[str], stop: threading.Event) -> None:
        """纯静态直通站点：轻量 AsyncSession 高并发秒下。"""
        total = sum(self.db.count_pending_host(h) for h in hosts)
        logger.info("⚡️ [AUTO 调度] 启动 curl_direct 直通管线 (共 %d 个域名，%d 篇待采)", len(hosts), total)
        asyncio.run(self._direct_worker_pool(hosts, stop))

    async def _direct_worker_pool(self, hosts: list[str], stop: threading.Event) -> None:
        from curl_cffi.requests import AsyncSession

        semaphore = asyncio.Semaphore(self.workers * 2)

        async with AsyncSession(impersonate="chrome120") as session:
            for host in hosts:
                consecutive_blocks = 0
                while not stop.is_set():
                    chunk = await asyncio.to_thread(self.db.claim_chunk, "curl-direct", limit=20, host=host)
                    if not chunk:
                        break

                    async def fetch_one(task: Task) -> bool:
                        nonlocal consecutive_blocks
                        t0 = time.monotonic()
                        proxy_url = self._get_direct_proxy_url()
                        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
                        dest = pdf_path(self.out, task.doi, ".html" if task.url_type == "html" else ".pdf")
                        try:
                            resp = await session.get(task.url, proxies=proxies, timeout=20, allow_redirects=True)
                            code = resp.status_code
                            elapsed = int((time.monotonic() - t0) * 1000)
                            m = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.I | re.S)
                            title = m.group(1).strip() if m else ""
                            is_waf = code in (403, 429) or any(p in title.lower() for p in ("just a moment", "attention required", "security check", "cf-mitigated"))
                            if is_waf:
                                consecutive_blocks += 1
                                await asyncio.to_thread(
                                    self.db.retry_later, task, count=False,
                                    error_kind="waf_challenge", error_message=f"HTTP {code} / WAF质询: {title[:60]}"
                                )
                                return False
                            elif code < 400:
                                consecutive_blocks = 0
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_bytes(resp.content)
                                await asyncio.to_thread(
                                    self.db.finish, task, Status.SUCCESS, http_status=code,
                                    file_path=str(dest), file_size=len(resp.content),
                                    elapsed_ms=elapsed, title=title or None, final_url=str(resp.url)
                                )
                                logger.info("⚡️ [直连秒下] %s (%d ms) %s", task.doi, elapsed, title[:30])
                                return True
                            else:
                                await asyncio.to_thread(
                                    self.db.finish, task, Status.FAILED, http_status=code,
                                    elapsed_ms=elapsed, error_kind="not_found" if code in (404, 410) else None,
                                    error_message=f"HTTP {code}", title=title or None
                                )
                                return False
                        except Exception as e:
                            await asyncio.to_thread(
                                self.db.retry_later, task, count=True,
                                error_kind="network", error_message=str(e)[:200]
                            )
                            return False

                    tasks = [fetch_one(t) for t in chunk]
                    await asyncio.gather(*tasks)

                    if consecutive_blocks >= 3:
                        logger.warning(
                            "🚨 [自愈降级] %s 在 curl_direct 连续触发 %d 次 WAF 拦截/质询，提前熔断并动态降级为 browser_only！",
                            host, consecutive_blocks,
                        )
                        self.routes[host] = "browser_only"
                        break

    def _run_curl_pool_host(self, host: str, stop: threading.Event) -> bool:
        """针对单个 curl_pool 域名，启动 demand-driven 的 Producer + Downloader 对。"""
        if not self.store:
            try:
                self.store = IdentityStore(self.cfg.redis_url, self.cfg.key_prefix)
                self.store.ping()
            except Exception as e:
                logger.warning("Redis 未连接 (%s)，域名 %s 降级至浏览器池渲染", e, host)
                self.routes[host] = "browser_only"
                return False

        pending = self.db.count_pending_host(host)
        logger.info("⚡️ [AUTO 调度] 启动 %s 的需求驱动 cURL 身份池管线 (待办 %d 篇)", host, pending)

        # 启动按需生产者（严格由下游 Downloader 的 demand 反压控制）
        producer = _ProducerThread(self.cfg, self.store, self.proxy, host)
        try:
            dl = Downloader(self.cfg, self.store, self.proxy, self.db, self.out, self.workers, host=host)
            dl.run(stop)
            if dl.circuit_broken:
                logger.warning(
                    "🚨 [自愈降级] %s 触发 cURL 身份池熔断，动态改写为 browser_only 并移交真实浏览器接管",
                    host,
                )
                self.routes[host] = "browser_only"
                return False
        finally:
            producer.shutdown(timeout_s=self.cfg.pass_timeout_s + 10)
            self.store.clear_demand(host)

        return True
