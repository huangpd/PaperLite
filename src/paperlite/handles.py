"""
doi.org Handle API 批量查询（paperlite handles）。

客户只给 DOI 时，任务链接是拼出来的 https://doi.org/{doi}，浏览器每篇都要先过 doi.org 的 302。
这里逐个 DOI 查 https://doi.org/api/handles/{doi}?type=URL，拿到出版商登记的落地 URL（如
linkinghub.elsevier.com/retrieve/pii/...），由 TaskDB.apply_handles 就地替换任务链接，少一跳；
同时把 DOI 未注册（404 / responseCode 100）或未登记 URL 的任务标 failed，不再下载。

只有确定结果（ok / not_found / no_url）写进 doi_handles；429 / 5xx / 网络错误按退避重试，
重试用尽也不落库，下次运行会重查——绝不把暂时性失败当成"DOI 失效"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from paperlite.tasks import TaskDB

logger = logging.getLogger(__name__)

HANDLE_API = "https://doi.org/api/handles/{}?type=URL"
_PAGE = 2000          # 每次从库里取多少条待查任务
_FLUSH = 500          # 攒多少条结果写一次库
_MAX_BACKOFF_S = 60.0


def handle_api_url(doi: str) -> str:
    """DOI 里的 # ? ; 空格等必须编码，否则被截断成另一个 DOI、误判 404。"""
    return HANDLE_API.format(quote(doi, safe="/()"))


def parse_handle(code: int, body: str) -> tuple[str, Optional[str]]:
    """
    返回 (kind, url)。kind：ok / not_found / no_url 为确定结果；retry 为暂时性失败（限流、5xx、非 JSON 等）。
    Handle API：responseCode 1 = 成功，100 = 未找到（HTTP 404），200 = 句柄存在但没有所查类型的值。
    """
    if code not in (200, 404):
        return "retry", None
    try:
        data = json.loads(body)
    except ValueError:
        return "retry", None             # 例如代理/网关返回的 HTML 错误页
    rc = data.get("responseCode") if isinstance(data, dict) else None
    if rc == 100:
        return "not_found", None
    if code != 200:
        return "retry", None
    if rc == 200:
        return "no_url", None
    if rc != 1:
        return "retry", None
    for v in data.get("values") or ():
        value = (v.get("data") or {}).get("value") if v.get("type") == "URL" else None
        if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")):
            return "ok", value.strip()
    return "no_url", None


def _retry_after(headers) -> Optional[float]:
    try:
        return float((headers or {}).get("retry-after") or "")
    except ValueError:
        return None


@dataclass
class HandleStats:
    ok: int = 0
    not_found: int = 0
    no_url: int = 0
    gave_up: int = 0          # 重试用尽，未落库，下次再查
    requests: int = 0

    @property
    def done(self) -> int:
        return self.ok + self.not_found + self.no_url + self.gave_up


async def resolve_handles(db: TaskDB, concurrency: int = 50, interval_s: float = 0.2,
                          proxy: Optional[str] = None, limit: Optional[int] = None,
                          max_attempts: int = 5, timeout_s: float = 20.0,
                          stop: Optional[threading.Event] = None) -> HandleStats:
    """
    concurrency 个协程并发查询，每个协程两次请求之间间隔 interval_s 秒
    （理论峰值约 concurrency / interval_s 次/秒）。limit 限制本次查询的 DOI 数（试跑用）。
    """
    from curl_cffi.requests import AsyncSession

    stop = stop or threading.Event()
    concurrency = max(1, concurrency)
    stats = HandleStats()
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=concurrency * 4)
    buffer: list[tuple[str, str, Optional[str]]] = []
    flush_lock = asyncio.Lock()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    t0 = time.monotonic()

    async def flush(force: bool = False) -> None:
        async with flush_lock:
            if not buffer or (len(buffer) < _FLUSH and not force):
                return
            rows = buffer[:]
            buffer.clear()
            await asyncio.to_thread(db.save_handles, rows)

    async def feed() -> None:
        after_id, queued, seen = 0, 0, set()
        try:
            while not stop.is_set() and (limit is None or queued < limit):
                page = await asyncio.to_thread(db.unresolved_doi_tasks, after_id, _PAGE)
                if not page:
                    break
                after_id = page[-1][0]
                for _, doi in page:
                    if doi in seen:          # 同一 DOI 的多条任务（pdf + html）只查一次
                        continue
                    seen.add(doi)
                    await queue.put(doi)
                    queued += 1
                    if limit is not None and queued >= limit:
                        break
        finally:
            for _ in range(concurrency):
                await queue.put(None)

    async def work(session) -> None:
        while True:
            doi = await queue.get()
            if doi is None:
                return
            kind, url = "retry", None
            for attempt in range(1, max_attempts + 1):
                if stop.is_set():
                    break
                wait = None
                try:
                    stats.requests += 1
                    resp = await session.get(handle_api_url(doi), proxies=proxies, timeout=timeout_s,
                                             headers={"Accept": "application/json"})
                    kind, url = parse_handle(resp.status_code, resp.text)
                    if kind == "retry" and resp.status_code == 429:
                        wait = _retry_after(resp.headers)
                except Exception as e:
                    kind, url = "retry", None
                    logger.debug("handle 查询异常 %s（第 %d 次）: %s", doi, attempt, e)
                if kind != "retry":
                    break
                await asyncio.sleep(min(_MAX_BACKOFF_S, wait if wait is not None else 2.0 ** attempt))
            if kind == "retry":
                stats.gave_up += 1
            else:
                setattr(stats, kind, getattr(stats, kind) + 1)
                buffer.append((doi, kind, url))
                await flush()
            if stats.done % 5000 == 0:
                rate = stats.done / max(1e-6, time.monotonic() - t0)
                logger.info("handle 查询进度 %d（ok %d / 未注册 %d / 无 URL %d / 放弃 %d），%.1f 个/秒",
                            stats.done, stats.ok, stats.not_found, stats.no_url, stats.gave_up, rate)
            await asyncio.sleep(interval_s)

    logger.info("开始查询 doi.org Handle API：并发 %d，每协程间隔 %.2fs%s%s", concurrency, interval_s,
                f"，最多 {limit} 个 DOI" if limit else "", "，走代理" if proxy else "")
    try:
        async with AsyncSession(max_clients=concurrency) as session:
            await asyncio.gather(feed(), *(work(session) for _ in range(concurrency)))
    finally:
        if buffer:                               # 中断时也把已拿到的确定结果写下，避免重查
            db.save_handles(buffer[:])
            buffer.clear()
    logger.info("handle 查询完成：%d 个 DOI，ok %d，未注册 %d，无 URL %d，重试用尽 %d（下次重查），请求 %d 次，用时 %.1f 分钟",
                stats.done, stats.ok, stats.not_found, stats.no_url, stats.gave_up, stats.requests,
                (time.monotonic() - t0) / 60)
    return stats
