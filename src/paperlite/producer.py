#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/producer.py
───────────────────────
生产者：按需提取新 IP → 浏览器过盾 → 身份写入 Redis。

目标健康身份数 = clamp(在线下载线程数, min_identities, max_identities)。
健康 = 剩余寿命 > refill_before_s，所以在用身份临近过期前就会开始生产替补；
下载线程全部退出后目标降为 min_identities，不再白白过盾。
过盾失败的 IP 直接丢弃：同一 IP 重试很少能过，换 IP 更快。
对冲：实测成功的过盾多在 30s 内，卡住的（Turnstile 一直 "Verifying…"）会耗满超时；
所以过盾超过 hedge_after_s 还没成功就不再算"在路上的替补"，立刻另开一个补位，旧的继续跑到超时。
"""

import asyncio
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from paperlite.browser import pass_challenge
from paperlite.config import Limits
from paperlite.consumer import is_cf_denied, is_challenge, rate_limit_wait, session_for_identity
from paperlite.cookies import jar_from_records
from paperlite.proxy import IpSource, ProxyUnavailable
from paperlite.store import Identity, IdentityStore

logger = logging.getLogger(__name__)

_TICK_S = 1.0
_LOCK_TTL_S = 15          # 生产者锁 TTL；每个 tick 续期


@dataclass
class ProducerStats:
    passed: int = 0
    failed: int = 0
    no_ip: int = 0


class Producer:
    def __init__(self, cfg: Limits, store: IdentityStore, ips: IpSource, host: str) -> None:
        self.cfg, self.store, self.ips, self.host = cfg, store, ips, host
        self.landing = cfg.landing_for(host)
        self.stats = ProducerStats()
        self._started: dict[int, float] = {}       # 过盾任务编号 → 开始时间（monotonic）
        self._seq = 0
        self._last_pause_log = 0.0
        self.holder = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"

    def target(self, workers: int, demand: Optional[int] = None) -> int:
        """
        目标健康身份数：
        - 若下游消费者通过 Redis 显式上报了需求水位 demand（拉动式/背压模式）：
          - 若 demand <= 0（待办为空或已做完），目标坚决归零，不生产任何新身份；
          - 若 demand > 0，目标为 min(demand, workers, max_identities)；
        - 若未设置 demand（兼容老模式），目标为 clamp(workers, min_identities, max_identities)。
        """
        if demand is not None:
            if demand <= 0:
                return 0
            return max(1, min(demand, self.cfg.max_identities, max(1, workers)))
        return max(self.cfg.min_identities, min(self.cfg.max_identities, workers))

    async def run(self, stop: threading.Event | None = None) -> None:
        """一直运行到 stop 被置位（或被取消）。"""
        logger.info("生产者启动: %s，落地页 %s，浏览器上限 %d，身份上限 %d",
                    self.host, self.landing, self.cfg.max_browsers, self.cfg.max_identities)
        tasks: set[asyncio.Task] = set()
        last_log = 0.0
        standby = False
        try:
            while not (stop and stop.is_set()):
                if not await asyncio.to_thread(self.store.hold_producer, self.host, self.holder, _LOCK_TTL_S):
                    if not standby:
                        logger.info("%s 已有生产者在运行（%s），本进程待命", self.host,
                                    self.store.producer_holder(self.host))
                        standby = True
                    await asyncio.sleep(_LOCK_TTL_S / 3)
                    continue
                if standby:
                    logger.info("接手 %s 的生产", self.host)
                    standby = False
                await asyncio.to_thread(self.store.reap, self.host)
                if await asyncio.to_thread(self._breaker_open):
                    await asyncio.sleep(_TICK_S * 5)
                    continue
                demand = await asyncio.to_thread(self.store.get_demand, self.host)
                st = await asyncio.to_thread(self.store.stats, self.host, self.cfg.refill_before_s)
                now = time.monotonic()
                fresh = sum(1 for t0 in self._started.values() if now - t0 < self.cfg.hedge_after_s)
                tgt = self.target(st.workers, demand)
                want = tgt - st.healthy - fresh
                room = self.cfg.max_browsers - len(self._started)
                if tgt > 0:
                    for _ in range(max(0, min(want, room))):
                        self._seq += 1
                        self._started[self._seq] = now
                        t = asyncio.create_task(self._produce_one(self._seq))
                        tasks.add(t)
                        t.add_done_callback(tasks.discard)
                if time.monotonic() - last_log > 30:
                    last_log = time.monotonic()
                    stale = sum(1 for t0 in self._started.values() if now - t0 >= self.cfg.hedge_after_s)
                    demand_str = f"需求 {demand}" if demand is not None else "无需求限制"
                    logger.info("身份池 %s: 空闲 %d 占用 %d 健康 %d / 目标 %d (%s) | 浏览器过盾中 %d（超 %.0fs 的 %d）"
                                "| 累计过盾成功 %d 次、失败 %d 次",
                                self.host, st.ready, st.inuse, st.healthy, tgt, demand_str,
                                len(self._started), self.cfg.hedge_after_s, stale,
                                self.stats.passed, self.stats.failed)
                await asyncio.sleep(_TICK_S)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.store.release_producer(self.host, self.holder)
            logger.info("生产者退出: %s，过盾成功 %d 失败 %d", self.host, self.stats.passed, self.stats.failed)

    def _breaker_open(self) -> bool:
        left = self.store.paused_for(self.host)
        if not left and self.store.fresh_rejects(self.host) >= self.cfg.max_fresh_rejects:
            self.store.trip(self.host, self.cfg.breaker_pause_s, self.cfg.max_fresh_rejects)
            logger.warning("熔断：连续 %d 个新身份一篇没下成就被拒（站点规则可能变了），暂停生产 %.0fs",
                           self.cfg.max_fresh_rejects, self.cfg.breaker_pause_s)
            return True
        if left and time.monotonic() - self._last_pause_log > 60:
            self._last_pause_log = time.monotonic()
            logger.warning("熔断中，%ds 后放一个身份试探", left)
        return bool(left)

    async def _produce_one(self, seq: int) -> None:
        try:
            try:
                lease = await asyncio.to_thread(self.ips.extract_checked)
            except ProxyUnavailable as e:
                self.stats.no_ip += 1
                logger.warning("提取 IP 失败: %s", e)
                await asyncio.sleep(5)
                return
            logger.debug("拿到 IP %s（%s），开始过盾", lease.exit_ip, lease.area)
            if lease.expires_at - time.time() < self.cfg.refill_before_s + self.cfg.pass_timeout_s:
                logger.info("IP %s 剩余寿命太短，丢弃", lease.exit_ip)
                return
            r = await pass_challenge(self.landing, lease, self.ips.s, self.cfg.pass_timeout_s,
                                     self.cfg.headed, self.cfg.debug_dir, geoip=self.cfg.geoip,
                                     webrtc_spoof=self.cfg.webrtc_spoof)
            if not r.ok:
                self.stats.failed += 1
                logger.info("过盾失败 %s %.0fs: %s", lease.exit_ip, r.seconds, r.error)
                return
            clearance_expiry = r.clearance_expires()
            ident = Identity(
                host=self.host, exit_ip=lease.exit_ip, proxy_server=lease.server,
                user_agent=r.user_agent, cookies=jar_from_records(r.cookies),
                fingerprint=r.fingerprint, pass_s=r.seconds,
                expires_at=min(clearance_expiry, lease.expires_at) if clearance_expiry else lease.expires_at,
            )
            if ident.left() < self.cfg.min_left_s:
                self.stats.failed += 1
                logger.info("过盾后身份 %s 剩余寿命不足，不入池", lease.exit_ip)
                return
            usable = await asyncio.to_thread(self._curl_usable, ident)
            if usable is not True:
                self.stats.failed += 1
                if usable is False:
                    await asyncio.to_thread(self.store.note_fresh_reject, self.host)
                return
            if ident.left() < self.cfg.min_left_s:
                self.stats.failed += 1
                logger.info("curl 验收后身份 %s 剩余寿命不足，不入池", lease.exit_ip)
                return
            await asyncio.to_thread(self.store.add, ident)
            self.stats.passed += 1
            logger.info("过盾成功 %s %.0fs，身份寿命 %.0fs", lease.exit_ip, r.seconds, ident.left())
        except asyncio.CancelledError:
            raise
        except Exception:
            self.stats.failed += 1
            logger.exception("生产身份异常")
        finally:
            self._started.pop(seq, None)

    def _curl_usable(self, ident: Identity) -> bool | None:
        try:
            with session_for_identity(ident) as session:
                for attempt in (1, 2):
                    response = session.get(self.landing, proxy=self.ips.s.url_for(ident.proxy_server),
                                           allow_redirects=True, stream=True,
                                           timeout=(self.cfg.connect_timeout_s, min(self.cfg.download_timeout_s, 30)))
                    try:
                        body = bytearray()
                        for chunk in response.iter_content(chunk_size=8192):
                            body.extend(chunk)
                            if len(body) >= 20000:
                                break
                        code, headers = response.status_code, response.headers
                    finally:
                        response.close()
                    # Cloudflare Access denied / Attention Required（403 或 429 触发）：
                    # 该 IP 被 CF 封禁，身份不可用（计入新身份拒绝），应 return False 而非 None
                    if is_cf_denied(code, headers, bytes(body)):
                        logger.info("curl 身份验收 %s HTTP %d Cloudflare 封禁", ident.exit_ip, code)
                        return False
                    if code == 429:
                        delay = rate_limit_wait(headers, ident.left(), self.cfg.min_left_s)
                        if attempt == 1 and delay is not None:
                            logger.info("curl 身份验收 %s HTTP 429，等待 %.0fs 重试", ident.exit_ip, delay)
                            time.sleep(delay)
                            continue
                        # 等待后仍 429（非 CF 封禁），视为临时限流，不计入拒绝（return None）
                        logger.info("curl 身份验收 %s HTTP 429，限流未恢复", ident.exit_ip)
                        return None
                    accepted = code < 400 and not is_challenge(headers, bytes(body))
                    logger.info("curl 身份验收 %s HTTP %d %s", ident.exit_ip, code,
                                "通过" if accepted else "拒绝")
                    return accepted
        except Exception as error:
            logger.info("curl 身份验收 %s 出错: %s", ident.exit_ip, type(error).__name__)
            return None
