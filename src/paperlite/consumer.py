#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/consumer.py
───────────────────────
消费者：从 SQLite 任务库领任务、从 Redis 领身份，用 curl_cffi 带 cookie、经同一出口 IP 下载 PDF。

- 每个下载线程独占一个身份（IP+cookie），同一 IP 两次请求随机间隔 request_gap_s；
- 线程只在有活可干（有 pending 任务）时登记心跳，生产者据此决定要多少身份；只剩别的线程的在途任务时
  注销并把身份还回池里空等（收尾阶段不再让生产者白白补身份）；没有 pending 也没有在途任务时退出；
- 遇到挑战先用落地页验身份：落地页也被挑战 → 身份坏了（cookie 被拒），丢弃换下一个，任务放回不计次数；
  落地页正常 → 站点单独盯上了这条 URL，任务 challenge_count+1 排到后面，满 max_url_challenges 判 failed；
  （站点会成批盯上相邻论文，如 ACM 1005062.* 的 14 篇；只按"下一条也被挑战"判身份会把好身份接连误杀）
- 连接被断（IP 掉线）/ IP 被站点封禁 / 寿命不足 min_left_s → 丢弃身份换下一个，任务放回且不计重试；
- 连接被断（curl 56）先用同一身份重试一次（代理偶尔成批断连，见 README）；
- 超时、5xx 计一次重试，满 max_tries 判 failed；
- 返回 HTML 的判 non_pdf（error_kind 见 classify.py），原始页面存到 <out>/non_pdf/；
  同一卷期接连 volume_stop_after 篇没有 PDF 就停掉该卷期其余任务（逻辑在 tasks.py）。
"""

import logging
import os
import random
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import certifi
from curl_cffi import requests as curl_requests

from paperlite.classify import classify_html, page_title
from paperlite.config import PoolConfig
from paperlite.proxy import ProxySettings
from paperlite.store import Identity, IdentityStore
from paperlite.tasks import Status, Task, TaskDB, pdf_path

logger = logging.getLogger(__name__)

_IDLE_WAIT_S = 2.0
_RESET_RETRY_WAIT_S = 2.0      # curl (56) 后同一身份重试前的等待


def impersonate_for(user_agent: str) -> str:
    """按浏览器 Chrome 主版本选 curl_cffi 指纹（不高于浏览器版本的最新一个）；cf_clearance 与 UA/TLS 指纹绑定。"""
    from curl_cffi.requests import BrowserType

    m = re.search(r"Chrome/(\d+)", user_agent)
    if m:
        major = int(m.group(1))
        candidates = sorted(
            (int(v.group(1)), b.value) for b in BrowserType
            if (v := re.fullmatch(r"chrome(\d+)a?", b.value)) and int(v.group(1)) <= major
        )
        if candidates:
            return candidates[-1][1]
    return "chrome"


def is_challenge(resp) -> bool:
    return bool(resp.headers.get("cf-mitigated")) or b"challenge-platform" in resp.content[:20000]


@dataclass
class DownloadStats:
    success: int = 0
    non_pdf: int = 0
    failed: int = 0
    rejected: int = 0        # 身份被丢弃次数（cookie 被拒、IP 封禁）
    volumes_stopped: int = 0 # 被停掉的卷期数
    inferred: int = 0        # 因卷期停掉而没请求的任务数
    challenged: int = 0      # URL 带有效 cookie 仍被挑战的次数
    identities: int = 0      # 领用过的身份数
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, name: str) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + 1)


class Downloader:
    def __init__(self, cfg: PoolConfig, store: IdentityStore, proxy: ProxySettings, db: TaskDB,
                 out_dir: Path, workers: int) -> None:
        self.cfg, self.store, self.proxy, self.db = cfg, store, proxy, db
        self.host = db.host()
        if not self.host:
            raise ValueError("任务库是空的，先 import")
        db.volume_stop_after = cfg.volume_stop_after
        self.out = Path(out_dir)
        self.non_pdf_dir = self.out / "non_pdf"
        self.workers = workers
        self.stats = DownloadStats()
        self._worker_prefix = f"{socket.gethostname()}-{os.getpid()}"

    def run(self, stop: Optional[threading.Event] = None) -> DownloadStats:
        stop = stop or threading.Event()
        # 结束用事件判断，不用 Thread.join()：信号打断 join 后 is_alive() 可能误报已结束
        finished = [threading.Event() for _ in range(self.workers)]
        for i, done in enumerate(finished):
            threading.Thread(target=self._worker, args=(i, stop, done), daemon=True, name=f"dl-{i}").start()
        try:
            for done in finished:
                while not done.wait(1):
                    pass
        except KeyboardInterrupt:
            stop.set()                       # 让线程把任务和身份还回去再退出
            deadline = time.monotonic() + 30
            for done in finished:
                done.wait(max(0.0, deadline - time.monotonic()))
            raise
        return self.stats

    # ── 下载线程 ──────────────────────────────────────────────

    def _worker(self, n: int, stop: threading.Event, finished: threading.Event) -> None:
        worker = f"{self._worker_prefix}-{n}"
        ident: Optional[Identity] = None
        session = None
        uses = 0                                      # 当前身份成功下载数
        waited_since = None
        registered = False
        try:
            while not stop.is_set():
                if not self.db.has_status(Status.PENDING):
                    if not self.db.has_status(Status.RUNNING):
                        return
                    # 只剩别的线程的在途任务（可能被放回）：不登记需求，身份还回池里，空等
                    if registered:
                        self.store.worker_gone(self.host, worker)
                        registered = False
                    if ident is not None:
                        self.store.release(ident)
                        session.close()
                        ident, session = None, None
                    stop.wait(_IDLE_WAIT_S)
                    continue
                self.store.worker_alive(self.host, worker)
                registered = True
                if ident is None:
                    ident = self.store.claim(self.host, worker, self.cfg.min_left_s, self.cfg.lease_ttl_s)
                    if ident is None:
                        waited_since = waited_since or time.monotonic()
                        if time.monotonic() - waited_since > 60:
                            logger.info("线程 %d 已等待身份 %.0fs（生产者是否在运行？）", n,
                                        time.monotonic() - waited_since)
                            waited_since = time.monotonic()
                        stop.wait(_IDLE_WAIT_S)
                        continue
                    waited_since = None
                    self.stats.add("identities")
                    session = self._session(ident)
                    uses = 0
                task = self.db.claim(worker)
                if task is None:
                    # 被别的线程抢先领走，或剩下的都在"卷期待核实"（同卷期已有 N 篇在途），稍等再看
                    stop.wait(0.5)
                    continue
                keep, outcome = self._download(task, ident, session)
                if outcome == "pdf":
                    if uses == 0:
                        self.store.note_success(self.host)
                    uses += 1
                elif outcome == "rejected" and uses == 0:
                    self.store.note_fresh_reject(self.host)      # 新身份一篇没下成就被拒，计入熔断
                if keep:
                    self.store.heartbeat(ident, self.cfg.lease_ttl_s)
                if not keep or ident.left() < self.cfg.min_left_s:
                    self.store.discard(ident)
                    session.close()
                    ident, session = None, None
                else:
                    stop.wait(random.uniform(*self.cfg.request_gap_s))
        finally:
            try:
                self.store.worker_gone(self.host, worker)
                if ident is not None:
                    self.store.release(ident)
                    session.close()
            finally:
                finished.set()

    def _session(self, ident: Identity):
        s = curl_requests.Session(trust_env=False, verify=certifi.where(),
                                  impersonate=impersonate_for(ident.user_agent))
        for c in ident.cookies:
            s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
        s.headers["user-agent"] = ident.user_agent
        return s

    def _download(self, task: Task, ident: Identity, session) -> tuple[bool, str]:
        """下载一个任务并写结果；返回 (身份是否还能继续用, 结果: pdf / rejected / challenged / other)。"""
        t0 = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)
        for attempt in (1, 2):
            try:
                resp = session.get(task.pdf_url, proxy=self.proxy.url_for(ident.proxy_server),
                                   timeout=self.cfg.download_timeout_s, allow_redirects=True)
                break
            except Exception as e:
                err = f"{type(e).__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}"
                if attempt == 1 and "(56)" in err:
                    logger.debug("连接被断 %s，同一身份重试一次: %s", ident.exit_ip, task.pdf_url)
                    time.sleep(_RESET_RETRY_WAIT_S)
                    continue
                self._retry_or_fail(task, ident, "network", err)
                return "(28)" in err, "other"       # 超时保留身份；连接再次被断/被拒多半是 IP 掉线，换身份

        code, body = resp.status_code, resp.content
        if body[:4] == b"%PDF":
            path = pdf_path(self.out, task.paper_uid)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".part")
            tmp.write_bytes(body)
            tmp.replace(path)
            self.db.finish(task, Status.SUCCESS, http_status=code, file_path=str(path), file_size=len(body),
                           elapsed_ms=elapsed_ms(), exit_ip=ident.exit_ip)
            self.stats.add("success")
            return True, "pdf"
        if is_challenge(resp):
            if not self._identity_alive(ident, session):
                return self._drop_identity(task, ident, f"HTTP {code} 挑战，落地页也被挑战，cookie 被拒"), "rejected"
            self.stats.add("challenged")
            if self.db.challenged(task, self.cfg.max_url_challenges, ident.exit_ip, f"HTTP {code} 挑战（cookie 有效）"):
                self.stats.add("failed")
                logger.warning("URL 被站点单独挑战 %d 次，判失败: %s", self.cfg.max_url_challenges, task.pdf_url)
            else:
                logger.info("URL 被挑战但身份正常（落地页可访问），排到后面: %s", task.pdf_url)
            return True, "challenged"
        if code >= 500:
            self._retry_or_fail(task, ident, "server_error", f"HTTP {code}", http_status=code)
            return True, "other"

        html = body.decode("utf-8", "replace")
        kind, title = classify_html(html), page_title(html)[:200]
        if kind == "blocked":
            return self._drop_identity(task, ident, f"IP 被站点封禁: {title}"), "rejected"
        self.non_pdf_dir.mkdir(parents=True, exist_ok=True)
        (self.non_pdf_dir / (pdf_path(self.out, task.paper_uid).stem + ".html")).write_bytes(body)
        stopped = self.db.finish(task, Status.NON_PDF, http_status=code, file_size=len(body),
                                 elapsed_ms=elapsed_ms(), exit_ip=ident.exit_ip, error_kind=kind,
                                 error_message=title or None)
        self.stats.add("non_pdf")
        logger.info("non_pdf %s %s %s", kind, task.pdf_url, title)
        if stopped:
            volume, skipped = stopped
            self.stats.add("volumes_stopped")
            with self.stats.lock:
                self.stats.inferred += skipped
            logger.info("卷期 %s 已有 %d 篇没有 PDF，停掉其余 %d 篇（记 non_pdf / inferred，不再请求）",
                        volume, self.cfg.volume_stop_after, skipped)
        return True, "other"

    def _identity_alive(self, ident: Identity, session) -> bool:
        """用过盾时的落地页验身份：能正常打开说明 cookie 有效，挑战只针对那条 URL。"""
        try:
            resp = session.get(self.cfg.landing_for(self.host), proxy=self.proxy.url_for(ident.proxy_server),
                               timeout=30, allow_redirects=True)
        except Exception as e:
            logger.info("验身份时请求落地页出错 %s: %s", ident.exit_ip, type(e).__name__)
            return False
        return resp.status_code < 400 and not is_challenge(resp)

    def _drop_identity(self, task: Task, ident: Identity, reason: str) -> bool:
        self.stats.add("rejected")
        logger.info("丢弃身份 %s（%s），任务放回", ident.exit_ip, reason)
        self.db.retry_later(task, count=False)        # 不是任务的问题，不计重试
        return False

    def _retry_or_fail(self, task: Task, ident: Identity, kind: str, err: str,
                       http_status: Optional[int] = None) -> None:
        if task.retry_count + 1 < self.cfg.max_tries:
            logger.info("下载出错 %s %s（第 %d 次）: %s", ident.exit_ip, task.pdf_url, task.retry_count + 1, err)
            self.db.retry_later(task, count=True, error_kind=kind, error_message=err, exit_ip=ident.exit_ip)
        else:
            logger.warning("下载失败 %s: %s", task.pdf_url, err)
            self.db.finish(task, Status.FAILED, http_status=http_status, exit_ip=ident.exit_ip,
                           error_kind=kind, error_message=err)
            self.stats.add("failed")
