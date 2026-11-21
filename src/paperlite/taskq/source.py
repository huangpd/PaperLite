"""
paperlite/taskq/source.py
─────────────────────────
下载 worker 这一侧的任务来源：LeaseKeeper（后台续租）+ PgTaskSource（按块领取 → 本地队列 → 结果攒批回写）。
SQL 全在 queue.PgTaskQueue；本模块只编排。设计说明见 docs/development/pg-chunk-lease.md。
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Optional

from paperlite.taskq.queue import DEFAULT_CHUNK_SIZE, DEFAULT_LEASE_S, RETRY, SKIP, PgTaskQueue, Result
from paperlite.core.tasks import Status, Task

logger = logging.getLogger("paperlite")

_WAIT = object()                       # PgTaskSource 内部：本进程还有块在收尾，稍后再领


class LeaseKeeper:
    """后台线程按 interval 续租当前块；租约丢了置 lost，worker 看到后停止领块内剩余任务。"""

    def __init__(self, queue: PgTaskQueue, chunk_id: int, lease_s: float = DEFAULT_LEASE_S,
                 interval_s: Optional[float] = None) -> None:
        self.queue, self.chunk_id, self.lease_s = queue, chunk_id, lease_s
        self.interval_s = interval_s if interval_s is not None else lease_s / 6
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lease-{chunk_id}", daemon=True)

    def __enter__(self) -> "LeaseKeeper":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                if not self.queue.renew(self.chunk_id, self.lease_s):
                    self.lost.set()
                    return
            except Exception:          # 网络抖动：下次再试，租约留有余量
                time.sleep(1)


# ── 给下载 worker 用的任务来源 ─────────────────────────────────────

def _prefix_match(t: Task, doi_prefix: str, url_prefix: str) -> bool:
    return (not doi_prefix or t.doi.startswith(doi_prefix)) and (not url_prefix or t.url.startswith(url_prefix))


@dataclass
class _Active:
    keeper: LeaseKeeper
    left: int                         # 本块还没出结果的任务数（本地排队 + 在途）


class PgTaskSource:
    """
    给下载 worker（curl.downloader.Downloader / browser.pool.BrowserPool）用的任务来源：claim / finish / retry_later /
    challenged / count_claimable / has_status / host / hosts / counts。内部：按块领取 → 本地队列 → 结果攒批回写。

    - 本地队列空了就领下一块（上一块还有在途任务也照领，线程不空等）；没块了先 requeue_leftovers 再试
    - 一块的任务全部出结果 → 回写缓冲 → complete；租约丢了 → 丢弃本地还没领的任务，在途的结果照常上报
    - 结果每 flush_n 条或 flush_s 秒回写一次；关闭时回写剩余结果、交还没做完的块
    - 下载时的巡检只在本机领到的块里做，不查、不改全库（见 docs/development/pg-chunk-lease.md）：浏览器模式的
      WindowInspector 调 count_pending_matching / flag_suspects，只看、只跳本地队列，记 skip_reason = title_dup，
      规则记入 pl_skip_rules；跳过的任务保持 pending，不再领取，clear_skips 可放回
    """

    def __init__(self, queue: PgTaskQueue, *, chunk_size: int = DEFAULT_CHUNK_SIZE,
                 lease_s: float = DEFAULT_LEASE_S, flush_n: int = 500, flush_s: float = 5.0,
                 retry_delay_s: float = 0.0, remote_check_s: float = 5.0) -> None:
        self.q = queue
        self.path = f"pg:{queue.batch}"            # 日志里显示的任务队列名
        self.chunk_size, self.lease_s = chunk_size, lease_s
        self.flush_n, self.flush_s = flush_n, flush_s
        self.retry_delay_s = retry_delay_s
        self.remote_check_s = remote_check_s
        self._lock = threading.RLock()
        self._local: "deque[Task]" = deque()
        self._chunks: dict[int, _Active] = {}
        self._task_chunk: dict[int, int] = {}
        self._inflight: set[int] = set()
        self._buf: list[Result] = []
        self._flushed_at = time.monotonic()
        self._more, self._more_at = True, float("-inf")
        self._closed = threading.Event()
        self._flusher = threading.Thread(target=self._flush_loop, name="pg-flush", daemon=True)
        self._flusher.start()

    # ── 导入（CLI import / run --urls）───────────────────────

    def import_urls(self, rows: Iterable[dict]) -> tuple[int, int]:
        added, existed = self.q.import_urls(rows)
        self.q.build_chunks(self.chunk_size)
        return added, existed

    # ── 领取 ────────────────────────────────────────────────

    def claim(self, worker: str) -> Optional[Task]:
        """
        返回 None = 整个批次都没有可领的了。远端没块、但本进程还有块在做（别的线程的在途任务可能要重试）时，
        先等这些块收尾、把重试任务切进下一代再判断，不提前返回 None——浏览器模式拿到 None 会让所有 worker 收工。
        """
        while True:
            got = self._claim_once()
            if got is not _WAIT:
                return got
            time.sleep(0.5)

    def _claim_once(self):
        with self._lock:
            while True:
                self._drop_lost()
                if self._local:
                    t = self._local.popleft()
                    self._inflight.add(t.id)
                    return t
                ch = self.q.claim_chunk(self.lease_s)
                if ch is None:
                    if self.q.requeue_leftovers(self.chunk_size):
                        continue
                    if self._chunks:
                        return _WAIT
                    self._more, self._more_at = False, time.monotonic()
                    return None
                keeper = LeaseKeeper(self.q, ch.chunk_id, self.lease_s).__enter__()
                self._chunks[ch.chunk_id] = _Active(keeper, len(ch.tasks))
                for t in ch.tasks:
                    self._task_chunk[t.id] = ch.chunk_id
                self._local.extend(ch.tasks)
                logger.info("领到块 %d（第 %d 代，%s，%d 条，第 %d 次领取）", ch.chunk_id, ch.generation, ch.host,
                            len(ch.tasks), ch.attempts)

    def count_claimable(self, cap: Optional[int] = None) -> int:
        """本地排队数 + 远端是否还有块（远端每 remote_check_s 秒最多查一次）。"""
        with self._lock:
            n = len(self._local)
            if cap is not None and n >= cap:
                return cap
            now = time.monotonic()
            if now - self._more_at >= self.remote_check_s:
                try:
                    self._more = self.q.has_work()
                except Exception as e:
                    logger.warning("查询 PG 剩余块失败（按还有处理）: %s", e)
                    self._more = True
                self._more_at = now
            if self._more:
                n += cap if cap is not None else self.chunk_size
            return min(n, cap) if cap is not None else n

    def has_status(self, status: str) -> bool:
        if status == Status.RUNNING:
            with self._lock:
                return bool(self._inflight)
        return self.counts().get(status, 0) > 0

    # ── 结果 ────────────────────────────────────────────────

    def finish(self, task: Task, status: str, *, http_status: Optional[int] = None, file_path: str = None,
               file_size: Optional[int] = None, elapsed_ms: Optional[int] = None, exit_ip: str = None,
               error_kind: Optional[str] = None, error_message: Optional[str] = None,
               final_url: Optional[str] = None, title: Optional[str] = None) -> None:
        assert status in Status.FINAL, status
        self._done(task, Result(task.id, status, http_status=http_status, error_kind=error_kind,
                                error_message=error_message, file_path=file_path, file_size=file_size,
                                elapsed_ms=elapsed_ms, exit_ip=exit_ip, final_url=final_url, title=title))

    # ── 巡检钩子：browser.pool 的 WindowInspector 调用，只作用于本地队列 ──────

    def count_pending_matching(self, doi_prefix: str, url_prefix: str) -> int:
        with self._lock:
            return sum(1 for t in self._local if _prefix_match(t, doi_prefix, url_prefix))

    def flag_suspects(self, doi_prefix: str, url_prefix: str, title: str, support: int, precision: float) -> int:
        msg = (f"巡检（本机块内）：标题 {title[:80]!r} 重复，规则 doi~{doi_prefix!r} url~{url_prefix!r}"
               f"（precision {precision:.3f}，命中 {support}），未请求")
        with self._lock:
            n = self._skip_local(lambda t: _prefix_match(t, doi_prefix, url_prefix), "title_dup", None, msg)
        if n:
            try:
                self.q.record_rule(doi_prefix, url_prefix, title, support, precision, n, "chunk")
            except Exception as e:                  # 规则记录只供 inspect --list 查看，失败不影响跳过
                logger.warning("记录巡检规则失败: %s", e)
        return n

    def _skip_local(self, pred, reason: str, error_kind: Optional[str], message: str) -> int:
        """调用方持锁：把本地队列里满足 pred 的任务记为跳过（不请求）。返回跳过数。"""
        keep, hit = deque(), []
        for t in self._local:
            (hit if pred(t) else keep).append(t)
        self._local = keep
        for t in hit:
            self._skip_one(t, reason, error_kind, message)
        return len(hit)

    def _skip_one(self, t: Task, reason: str, error_kind: Optional[str], message: str) -> None:
        """调用方持锁：t 已不在本地队列里。"""
        self._buf.append(Result(t.id, SKIP, skip_reason=reason, error_kind=error_kind, error_message=message))
        cid = self._task_chunk.pop(t.id, None)
        if cid is not None:
            self._settle(cid)

    def retry_later(self, task: Task, *, count: bool, error_kind: Optional[str] = None,
                    error_message: Optional[str] = None, exit_ip: Optional[str] = None) -> None:
        self._done(task, Result(task.id, RETRY, error_kind=error_kind, error_message=error_message,
                                exit_ip=exit_ip, count_retry=count, retry_delay_s=self.retry_delay_s))

    def challenged(self, task: Task, max_challenges: int, exit_ip: str, message: str) -> bool:
        failed = task.challenge_count + 1 >= max_challenges
        self._done(task, Result(task.id, Status.FAILED if failed else RETRY, http_status=403,
                                error_kind="challenge_failed", error_message=message, exit_ip=exit_ip,
                                count_retry=False, challenged=True, retry_delay_s=self.retry_delay_s))
        return failed

    def _done(self, task: Task, result: Result) -> None:
        with self._lock:
            self._buf.append(result)
            self._inflight.discard(task.id)
            cid = self._task_chunk.pop(task.id, None)
            if len(self._buf) >= self.flush_n:
                self._flush()
            if cid is not None:
                self._settle(cid)

    def _settle(self, cid: int) -> None:
        """调用方持锁：一个任务出结果（或被丢弃）后，块的剩余数减一；到 0 就收尾。"""
        a = self._chunks.get(cid)
        if a is None:
            return
        a.left -= 1
        if a.left > 0:
            return
        del self._chunks[cid]
        a.keeper.__exit__(None, None, None)
        if a.keeper.lost.is_set():
            return
        if self._flush():                          # 结果先落库再 complete：否则重试任务可能被漏切
            self.q.complete(cid)
        else:
            logger.warning("块 %d 结果回写失败，不标记完成；租约到期后由别的机器重做", cid)

    def _drop_lost(self) -> None:
        """调用方持锁：租约丢了的块，本地还没领的任务不再做（别的机器在做）。"""
        lost = {cid for cid, a in self._chunks.items() if a.keeper.lost.is_set()}
        if not lost:
            return
        keep = deque()
        for t in self._local:
            cid = self._task_chunk.get(t.id)
            if cid in lost:
                self._task_chunk.pop(t.id, None)
                self._settle(cid)
            else:
                keep.append(t)
        self._local = keep
        logger.warning("块 %s 租约已丢失（被别的机器回收），本地剩余任务已放弃", sorted(lost))

    def _flush(self) -> bool:
        """调用方持锁。返回是否全部写入成功；失败时结果留在缓冲里下次再写。"""
        if not self._buf:
            return True
        try:
            self.q.report(self._buf)
        except Exception as e:
            logger.warning("结果回写 PG 失败（%d 条留在缓冲区）: %s", len(self._buf), e)
            return False
        self._buf = []
        self._flushed_at = time.monotonic()
        return True

    def _flush_loop(self) -> None:
        while not self._closed.wait(min(1.0, self.flush_s)):
            with self._lock:
                if self._buf and time.monotonic() - self._flushed_at >= self.flush_s:
                    self._flush()

    # ── 查询与生命周期（CLI 用）─────────────────────────────

    def host(self) -> Optional[str]:
        hs = self.q.hosts()
        return hs[0] if hs else None

    def hosts(self) -> list[str]:
        return self.q.hosts()

    def counts(self) -> dict[str, int]:
        """pending 不含推断跳过的（它们不会再被领取）。"""
        c = {s: 0 for s in Status.ALL}
        c.update(self.q.stats().tasks)
        skipped = sum(self.q.skip_counts().values())
        with self._lock:
            c[Status.RUNNING] = len(self._inflight)
        c[Status.PENDING] = max(0, c[Status.PENDING] - skipped - c[Status.RUNNING])
        return c

    def type_counts(self) -> dict[str, dict[str, int]]:
        return self.q.type_counts()

    def kind_counts(self) -> list[tuple[str, str, int]]:
        return self.q.kind_counts()

    def close(self) -> None:
        """回写剩余结果；没做完的块交还（放回 queued），别的机器马上能领。"""
        self._closed.set()
        self._flusher.join(timeout=5)
        with self._lock:
            self._flush()
            for cid, a in list(self._chunks.items()):
                a.keeper.__exit__(None, None, None)
                if not a.keeper.lost.is_set():
                    self.q.release(cid)
            self._chunks.clear()
            self._local.clear()
            self._task_chunk.clear()
        self.q.close()
