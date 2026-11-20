#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/pgqueue.py
────────────────────
【研究原型】PostgreSQL 任务队列：按块领取，供多台机器共享一个批次。设计说明见 docs/PG_CHUNK_LEASE.md。

与单机 tasks.py（SQLite + flock，逐条按顺序领取）的区别：
  - 块 = 按 (host, doi) 排好序后连续的一段任务（默认 1000 条），导入后由 build_chunks 切好，任务记 chunk_id
  - 机器一次领一块（pl_chunks 上 FOR UPDATE SKIP LOCKED），带租约；续租失败说明块被别人回收了
  - 块内任务领取时不改状态（不写 running），结果攒批回写；回写只改仍是 pending 的行，重复上报无害
  - 该稍后重试的任务记 next_attempt_at，留在原块；块做完即 done。requeue_leftovers 把到点的剩余任务
    重新切成下一代块（generation + 1），相当于单机版"被挑战/失败的排到后面"
  - 租约过期（机器挂了）的块在下一次领取时被放回 queued，由别的机器接手

依赖可选：pip install 'paperlite[pg]'（psycopg 3）。本模块不被 CLI 导入，现有单机流程不受影响。
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .tasks import ERROR_KINDS, Status, Task, UrlType, derive_doi, url_host, url_key

try:  # 可选依赖：只有用到 PG 队列时才需要
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover
    psycopg = None
    sql = None

logger = logging.getLogger("paperlite")

_WAIT = object()                       # PgTaskSource 内部：本进程还有块在收尾，稍后再领
RETRY = "retry"                       # report 用：放回 pending，next_attempt_at 之后再领
SKIP = "skip"                         # report 用：块内推断跳过，保持 pending、写 skip_reason，不再领取
SKIP_REASONS = ("title_dup",)         # 巡检（同标题重复）；PG 模式不做卷期停止

DEFAULT_CHUNK_SIZE = 1000
DEFAULT_LEASE_S = 1800                # 30 分钟；正常一块 10～30 分钟，worker 每 lease/6 续一次

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS pl_tasks (
    id              bigserial PRIMARY KEY,
    batch           text        NOT NULL,
    url_key         bytea       NOT NULL UNIQUE,
    doi             text        NOT NULL,
    url             text        NOT NULL,
    url_type        text        NOT NULL DEFAULT 'pdf' CHECK (url_type IN ('pdf', 'html')),
    host            text        NOT NULL,
    chunk_id        bigint,
    status          text        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'success', 'failed', 'non_pdf')),
    retry_count     int         NOT NULL DEFAULT 0,
    challenge_count int         NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
    skip_reason     text        CHECK (skip_reason IN ('title_dup')),  -- 块内巡检跳过，不再领取
    http_status     int,
    error_kind      text        CHECK (error_kind IN ({", ".join(f"'{k}'" for k in ERROR_KINDS)})),
    error_message   text,
    file_path       text,
    file_size       bigint,
    elapsed_ms      int,
    exit_ip         text,
    final_url       text,
    title           text,
    worker          text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
) WITH (fillfactor = 80);
ALTER TABLE pl_tasks ADD COLUMN IF NOT EXISTS skip_reason text;
CREATE INDEX IF NOT EXISTS pl_tasks_chunk   ON pl_tasks (chunk_id, challenge_count, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS pl_tasks_unchunk ON pl_tasks (batch, id) WHERE status = 'pending' AND chunk_id IS NULL;
CREATE INDEX IF NOT EXISTS pl_tasks_status  ON pl_tasks (batch, status, error_kind);

CREATE TABLE IF NOT EXISTS pl_chunks (
    chunk_id    bigserial   PRIMARY KEY,
    batch       text        NOT NULL,
    generation  int         NOT NULL DEFAULT 0,
    order_key   bigint      NOT NULL,          -- 块内最小任务 id：同一代按导入（排序后）顺序领
    host        text        NOT NULL,
    size        int         NOT NULL,
    first_doi   text,
    last_doi    text,
    status      text        NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'leased', 'done')),
    worker      text,
    lease_until timestamptz,
    attempts    int         NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pl_chunks_queue ON pl_chunks (batch, generation, order_key) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS pl_chunks_lease ON pl_chunks (lease_until) WHERE status = 'leased';
"""


@dataclass(frozen=True)
class Chunk:
    chunk_id: int
    generation: int
    host: str
    attempts: int
    tasks: tuple[Task, ...]


@dataclass
class Result:
    """一条任务的结果。status 为 success / non_pdf / failed，RETRY（放回，retry_delay_s 后再领），
    或 SKIP（块内推断跳过：保持 pending、写 skip_reason）。"""
    task_id: int
    status: str
    http_status: Optional[int] = None
    error_kind: Optional[str] = None
    error_message: Optional[str] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    elapsed_ms: Optional[int] = None
    exit_ip: Optional[str] = None
    final_url: Optional[str] = None
    title: Optional[str] = None
    retry_delay_s: float = 0.0
    count_retry: bool = True          # False：不是任务本身的问题（身份失效等），不计 retry_count
    challenged: bool = False          # True：带有效身份仍被挑战，challenge_count + 1（排到块内后面）
    skip_reason: Optional[str] = None  # SKIP 时必填：SKIP_REASONS 之一


@dataclass
class QueueStats:
    tasks: dict[str, int] = field(default_factory=dict)
    chunks: dict[str, int] = field(default_factory=dict)


class PgTaskQueue:
    """
    一个进程一个实例；线程间共享一条连接（加锁）。按块交互，每块只有领取、续租、几次批量回写，
    连接上的语句频率很低，不需要连接池。
    """

    def __init__(self, dsn: str, batch: str, *, schema: Optional[str] = None, worker: str = "") -> None:
        if psycopg is None:
            raise RuntimeError("PG 队列需要 psycopg：pip install 'paperlite[pg]'")
        self.batch = batch
        self.worker = worker
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._lock:
            if schema:
                self._conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
                self._conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            # 多台机器同时启动会并发建表：用 advisory lock 串行化 DDL
            with self._conn.transaction():
                self._conn.execute("SELECT pg_advisory_xact_lock(hashtext('paperlite.pgqueue.schema'))")
                self._conn.execute(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 导入与切块 ──────────────────────────────────────────

    def import_urls(self, rows: Iterable[dict]) -> tuple[int, int]:
        """
        与 TaskDB.import_urls 同样的输入。先 COPY 进临时表，再按 (host, doi) 排序插入：
        id 顺序即领取顺序，导入前不需要再排序。返回 (新增, 已存在)。
        """
        seen = 0
        with self._lock, self._conn.transaction():
            c = self._conn
            c.execute("CREATE TEMP TABLE pl_import (url_key bytea, doi text, url text, url_type text, host text) "
                      "ON COMMIT DROP")
            with c.cursor().copy("COPY pl_import FROM STDIN") as cp:
                for r in rows:
                    url = (r.get("url") or r.get("pdf_url") or "").strip()
                    h = url_host(url)
                    if not h:
                        raise ValueError(f"不是有效链接: {url}")
                    doi = (r.get("doi") or r.get("paper_uid") or "").strip().lower() or derive_doi(url)
                    kind = (r.get("url_type") or UrlType.PDF).strip().lower()
                    if kind not in UrlType.ALL:
                        raise ValueError(f"url_type 只能是 {' / '.join(UrlType.ALL)}，不是 {kind!r}: {url}")
                    cp.write_row((url_key(url), doi, url, kind, h))
                    seen += 1
            # 同一 url 多行只留 doi 最小的一行；按 (host, doi) 顺序插入，id 顺序即领取顺序
            added = c.execute(
                "INSERT INTO pl_tasks (batch, url_key, doi, url, url_type, host) "
                "SELECT %s, url_key, doi, url, url_type, host FROM ("
                "  SELECT DISTINCT ON (url_key) * FROM pl_import ORDER BY url_key, doi"
                ") d ORDER BY host, doi "
                "ON CONFLICT (url_key) DO NOTHING", (self.batch,)).rowcount
        return added, seen - added

    def build_chunks(self, size: int = DEFAULT_CHUNK_SIZE, generation: int = 0) -> int:
        """
        把本批次还没分块的 pending 任务按 id 顺序每 size 条切一块；换主机必切（一块只打一个站）。
        返回新建的块数。可重复调用：增量导入后再切一次即可。
        """
        # 用临时表 + ANALYZE 而不是一条 CTE：CTE 没有统计信息，规划器对百万行选嵌套循环（100 万行要 2 分多钟）
        p = {"size": size, "batch": self.batch, "gen": generation}
        with self._lock, self._conn.transaction():
            c = self._conn
            c.execute("CREATE TEMP TABLE pl_cut ON COMMIT DROP AS "
                      "SELECT id, host, doi, (row_number() OVER (PARTITION BY host ORDER BY id) - 1) / %(size)s AS k "
                      "FROM pl_tasks WHERE batch = %(batch)s AND status = 'pending' AND chunk_id IS NULL", p)
            c.execute("CREATE TEMP TABLE pl_cutmap (host text, k bigint, chunk_id bigint) ON COMMIT DROP")
            c.execute(
                "WITH g AS (SELECT host, k, count(*) AS n, min(id) AS lo, min(doi) AS fd, max(doi) AS ld "
                "           FROM pl_cut GROUP BY host, k), "
                "ins AS (INSERT INTO pl_chunks (batch, generation, order_key, host, size, first_doi, last_doi) "
                "        SELECT %(batch)s, %(gen)s, lo, host, n, fd, ld FROM g ORDER BY lo "
                "        RETURNING chunk_id, host, order_key) "
                "INSERT INTO pl_cutmap SELECT g.host, g.k, ins.chunk_id FROM g "
                "JOIN ins ON ins.host = g.host AND ins.order_key = g.lo", p)
            c.execute("ANALYZE pl_cut")
            c.execute("ANALYZE pl_cutmap")
            c.execute("UPDATE pl_tasks t SET chunk_id = m.chunk_id FROM pl_cut x JOIN pl_cutmap m USING (host, k) "
                      "WHERE t.id = x.id")
            return c.execute("SELECT count(*) FROM pl_cutmap").fetchone()[0]

    def requeue_leftovers(self, size: int = DEFAULT_CHUNK_SIZE) -> int:
        """
        已做完（done）的块里还剩的 pending 任务，到了 next_attempt_at 的，重新切成下一代块。
        返回新建的块数。由任一机器或调度进程定期调用；用 advisory lock 保证同一批次同时只有一个在切。
        """
        with self._lock, self._conn.transaction():
            c = self._conn
            got = c.execute("SELECT pg_try_advisory_xact_lock(hashtext('paperlite.pgqueue.requeue:' || %s))",
                            (self.batch,)).fetchone()[0]
            if not got:
                return 0
            gen = c.execute("SELECT coalesce(max(generation), -1) + 1 FROM pl_chunks WHERE batch = %s",
                            (self.batch,)).fetchone()[0]
            moved = c.execute(
                "UPDATE pl_tasks t SET chunk_id = NULL FROM pl_chunks k "
                "WHERE t.chunk_id = k.chunk_id AND k.status = 'done' AND k.batch = %s AND t.status = 'pending' "
                "AND t.skip_reason IS NULL AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now())",
                (self.batch,)).rowcount
        if not moved:
            return 0
        return self.build_chunks(size, generation=gen)

    # ── 领取、续租、完成 ────────────────────────────────────

    def reap_expired(self) -> int:
        """租约过期的块放回 queued（机器挂了 / 卡死）。返回回收的块数。"""
        with self._lock:
            return self._conn.execute(
                "UPDATE pl_chunks SET status = 'queued', worker = NULL, lease_until = NULL, updated_at = now() "
                "WHERE batch = %s AND status = 'leased' AND lease_until < now()", (self.batch,)).rowcount

    def claim_chunk(self, lease_s: float = DEFAULT_LEASE_S) -> Optional[Chunk]:
        """
        领一块：先回收过期租约，再按 (generation, order_key) 取第一块 queued（SKIP LOCKED，多机不抢同一块）。
        块里已没有可领任务（全做完/全被标记/全在等重试）就直接记 done，继续领下一块。没有块返回 None。
        """
        self.reap_expired()
        while True:
            with self._lock:
                c = self._conn
                row = c.execute(
                    "UPDATE pl_chunks SET status = 'leased', worker = %(w)s, attempts = attempts + 1, "
                    "lease_until = now() + make_interval(secs => %(lease)s), updated_at = now() "
                    "WHERE chunk_id = ("
                    "  SELECT chunk_id FROM pl_chunks WHERE batch = %(batch)s AND status = 'queued' "
                    "  ORDER BY generation, order_key LIMIT 1 FOR UPDATE SKIP LOCKED"
                    ") RETURNING chunk_id, generation, host, attempts",
                    {"w": self.worker, "lease": lease_s, "batch": self.batch}).fetchone()
                if row is None:
                    return None
                chunk_id, gen, host, attempts = row
                tasks = tuple(Task(r[0], r[1], r[2], r[3], r[4], r[5]) for r in c.execute(
                    "SELECT id, doi, url, retry_count, challenge_count, url_type FROM pl_tasks "
                    "WHERE chunk_id = %s AND status = 'pending' AND skip_reason IS NULL "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
                    "ORDER BY challenge_count, id", (chunk_id,)))
                if tasks:
                    return Chunk(chunk_id, gen, host, attempts, tasks)
                c.execute("UPDATE pl_chunks SET status = 'done', lease_until = NULL, updated_at = now() "
                          "WHERE chunk_id = %s", (chunk_id,))

    def renew(self, chunk_id: int, lease_s: float = DEFAULT_LEASE_S) -> bool:
        """续租。返回 False = 租约已丢（过期后被别的机器领走），应停止处理这块（已下的结果照常上报）。"""
        with self._lock:
            return self._conn.execute(
                "UPDATE pl_chunks SET lease_until = now() + make_interval(secs => %s), updated_at = now() "
                "WHERE chunk_id = %s AND worker = %s AND status = 'leased'",
                (lease_s, chunk_id, self.worker)).rowcount == 1

    def release(self, chunk_id: int) -> bool:
        """主动交还块（进程退出时）：放回 queued，别的机器不用等租约过期。"""
        with self._lock:
            return self._conn.execute(
                "UPDATE pl_chunks SET status = 'queued', worker = NULL, lease_until = NULL, updated_at = now() "
                "WHERE chunk_id = %s AND worker = %s AND status = 'leased'",
                (chunk_id, self.worker)).rowcount == 1

    def has_work(self) -> bool:
        """还有可领的：queued 块，或 done 块里到点待重试的剩余任务（requeue_leftovers 能切出新块）。"""
        with self._lock:
            return self._conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pl_chunks WHERE batch = %(b)s AND status = 'queued') "
                "OR EXISTS (SELECT 1 FROM pl_chunks k JOIN pl_tasks t ON t.chunk_id = k.chunk_id "
                "  WHERE k.batch = %(b)s AND k.status = 'done' AND t.status = 'pending' AND t.skip_reason IS NULL "
                "  AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now()))", {"b": self.batch}).fetchone()[0]

    def complete(self, chunk_id: int) -> bool:
        """块处理完：记 done。返回 False = 租约已不属于本机（结果已写入，不影响）。"""
        with self._lock:
            return self._conn.execute(
                "UPDATE pl_chunks SET status = 'done', lease_until = NULL, updated_at = now() "
                "WHERE chunk_id = %s AND worker = %s AND status = 'leased'",
                (chunk_id, self.worker)).rowcount == 1

    # ── 结果批量回写 ────────────────────────────────────────

    def report(self, results: Sequence[Result]) -> int:
        """
        批量写结果。只改仍为 pending 的行：块被回收后两台机器都做了同一条，先写的生效，后写的忽略。
        返回实际更新的行数。
        """
        finals = [r for r in results if r.status not in (RETRY, SKIP)]
        retries = [r for r in results if r.status == RETRY]
        skips = [r for r in results if r.status == SKIP]
        for r in finals:
            assert r.status in Status.FINAL, r.status
        for r in skips:
            assert r.skip_reason in SKIP_REASONS, r.skip_reason
        n = 0
        with self._lock, self._conn.transaction():
            with self._conn.cursor() as cur:
                if finals:
                    cur.executemany(
                        "UPDATE pl_tasks SET status = %s, http_status = %s, error_kind = %s, error_message = %s, "
                        "file_path = %s, file_size = %s, elapsed_ms = %s, exit_ip = %s, final_url = %s, title = %s, "
                        "challenge_count = challenge_count + %s, worker = %s, updated_at = now() "
                        "WHERE id = %s AND status = 'pending'",
                        [(r.status, r.http_status, r.error_kind, r.error_message, r.file_path, r.file_size,
                          r.elapsed_ms, r.exit_ip, r.final_url, r.title, int(r.challenged), self.worker, r.task_id)
                         for r in finals], returning=False)
                    n += cur.rowcount
                if retries:
                    cur.executemany(
                        "UPDATE pl_tasks SET retry_count = retry_count + %s, challenge_count = challenge_count + %s, "
                        "next_attempt_at = now() + make_interval(secs => %s), "
                        "error_kind = coalesce(%s, error_kind), error_message = coalesce(%s, error_message), "
                        "http_status = coalesce(%s, http_status), exit_ip = coalesce(%s, exit_ip), "
                        "worker = %s, updated_at = now() WHERE id = %s AND status = 'pending'",
                        [(int(r.count_retry), int(r.challenged), r.retry_delay_s, r.error_kind, r.error_message,
                          r.http_status, r.exit_ip, self.worker, r.task_id) for r in retries], returning=False)
                    n += cur.rowcount
                if skips:
                    cur.executemany(
                        "UPDATE pl_tasks SET skip_reason = %s, error_kind = %s, error_message = %s, worker = %s, "
                        "updated_at = now() WHERE id = %s AND status = 'pending'",
                        [(r.skip_reason, r.error_kind, r.error_message, self.worker, r.task_id) for r in skips],
                        returning=False)
                    n += cur.rowcount
        return n

    def skip_counts(self) -> dict[str, int]:
        """按 skip_reason 统计被块内推断跳过、仍为 pending 的任务数。"""
        with self._lock:
            return dict(self._conn.execute(
                "SELECT skip_reason, count(*) FROM pl_tasks WHERE batch = %s AND status = 'pending' "
                "AND skip_reason IS NOT NULL GROUP BY skip_reason", (self.batch,)).fetchall())

    def clear_skips(self, reason: Optional[str] = None, size: int = DEFAULT_CHUNK_SIZE) -> int:
        """
        撤销推断跳过（reason 为空 = 全部）：清掉 skip_reason，重新切成下一代块，之后照常领取下载。
        返回放回的任务数。
        """
        assert reason is None or reason in SKIP_REASONS, reason
        with self._lock, self._conn.transaction():
            c = self._conn
            gen = c.execute("SELECT coalesce(max(generation), -1) + 1 FROM pl_chunks WHERE batch = %s",
                            (self.batch,)).fetchone()[0]
            n = c.execute(
                "UPDATE pl_tasks SET skip_reason = NULL, error_kind = NULL, error_message = NULL, chunk_id = NULL, "
                "updated_at = now() WHERE batch = %s AND status = 'pending' AND skip_reason IS NOT NULL "
                "AND (%s::text IS NULL OR skip_reason = %s)", (self.batch, reason, reason)).rowcount
        if n:
            self.build_chunks(size, generation=gen)
        return n

    # ── 查询 ────────────────────────────────────────────────

    def stats(self) -> QueueStats:
        with self._lock:
            c = self._conn
            tasks = dict(c.execute("SELECT status, count(*) FROM pl_tasks WHERE batch = %s GROUP BY status",
                                   (self.batch,)).fetchall())
            chunks = dict(c.execute("SELECT status, count(*) FROM pl_chunks WHERE batch = %s GROUP BY status",
                                    (self.batch,)).fetchall())
        return QueueStats(tasks, chunks)

    def hosts(self) -> list[str]:
        """按领取顺序（块顺序）列出本批次的主机。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT host FROM pl_chunks WHERE batch = %s GROUP BY host ORDER BY min(chunk_id)",
                (self.batch,)).fetchall()
            if not rows:                    # 还没切块
                rows = self._conn.execute("SELECT DISTINCT host FROM pl_tasks WHERE batch = %s ORDER BY host",
                                          (self.batch,)).fetchall()
        return [r[0] for r in rows]

    def kind_counts(self) -> list[tuple[str, str, int, int]]:
        with self._lock:
            return [(s, k, n, 0) for s, k, n in self._conn.execute(
                "SELECT status, error_kind, count(*) FROM pl_tasks WHERE batch = %s AND status <> 'pending' "
                "GROUP BY status, error_kind ORDER BY status, count(*) DESC", (self.batch,))]

    def type_counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        with self._lock:
            for t, s, n in self._conn.execute(
                    "SELECT url_type, status, count(*) FROM pl_tasks WHERE batch = %s GROUP BY url_type, status",
                    (self.batch,)):
                out.setdefault(t, {k: 0 for k in Status.ALL})[s] = n
        return out


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


# ── TaskDB 的多机替身 ─────────────────────────────────────────

def _prefix_match(t: Task, doi_prefix: str, url_prefix: str) -> bool:
    return (not doi_prefix or t.doi.startswith(doi_prefix)) and (not url_prefix or t.url.startswith(url_prefix))


@dataclass
class _Active:
    keeper: LeaseKeeper
    left: int                         # 本块还没出结果的任务数（本地排队 + 在途）


class PgTaskSource:
    """
    给下载 worker（consumer.Downloader / browser_pool.BrowserPool）用的任务来源，接口与 TaskDB 中
    worker 用到的那部分一致：claim / finish / retry_later / challenged / count_claimable / has_status /
    host / hosts / counts。内部：按块领取 → 本地队列 → 结果攒批回写。

    - 本地队列空了就领下一块（上一块还有在途任务也照领，线程不空等）；没块了先 requeue_leftovers 再试
    - 一块的任务全部出结果 → 回写缓冲 → complete；租约丢了 → 丢弃本地还没领的任务，在途的结果照常上报
    - 结果每 flush_n 条或 flush_s 秒回写一次；关闭时回写剩余结果、交还没做完的块
    - 巡检只在本机领到的块里做，不查、不改全库（见 docs/PG_CHUNK_LEASE.md）：浏览器模式的 WindowInspector
      调 count_pending_matching / flag_suspects，只看、只跳本地队列，记 skip_reason = title_dup；
      跳过的任务保持 pending，不再领取，clear_skips 可放回
    - 不做卷期停止（依赖 ACM 式 DOI 结构，不通用）：volume_stop_after 被忽略，finish 恒返回 None
    """

    def __init__(self, queue: PgTaskQueue, *, chunk_size: int = DEFAULT_CHUNK_SIZE,
                 lease_s: float = DEFAULT_LEASE_S, flush_n: int = 500, flush_s: float = 5.0,
                 retry_delay_s: float = 0.0, remote_check_s: float = 5.0) -> None:
        self.q = queue
        self.path = f"pg:{queue.batch}"            # 日志里代替 TaskDB.path
        self.chunk_size, self.lease_s = chunk_size, lease_s
        self.flush_n, self.flush_s = flush_n, flush_s
        self.retry_delay_s = retry_delay_s
        self.remote_check_s = remote_check_s
        self.volume_stop_after = 0                 # Downloader 会赋值；PG 模式不做卷期停止，忽略
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

    # ── 导入（CLI import --pg）───────────────────────────────

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
               final_url: Optional[str] = None, title: Optional[str] = None) -> Optional[tuple[str, int]]:
        assert status in Status.FINAL, status
        self._done(task, Result(task.id, status, http_status=http_status, error_kind=error_kind,
                                error_message=error_message, file_path=file_path, file_size=file_size,
                                elapsed_ms=elapsed_ms, exit_ip=exit_ip, final_url=final_url, title=title))
        return None

    # ── 巡检钩子：browser_pool 的 WindowInspector 调用，只作用于本地队列 ──────

    def count_pending_matching(self, doi_prefix: str, url_prefix: str) -> int:
        with self._lock:
            return sum(1 for t in self._local if _prefix_match(t, doi_prefix, url_prefix))

    def flag_suspects(self, doi_prefix: str, url_prefix: str, title: str, support: int, precision: float) -> int:
        msg = (f"巡检（本机块内）：标题 {title[:80]!r} 重复，规则 doi~{doi_prefix!r} url~{url_prefix!r}"
               f"（precision {precision:.3f}，命中 {support}），未请求")
        with self._lock:
            return self._skip_local(lambda t: _prefix_match(t, doi_prefix, url_prefix), "title_dup", None, msg)

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

    def lock_for_download(self) -> int:
        """多机共享，没有独占锁；在途任务由租约保证。"""
        return 0

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

    def kind_counts(self) -> list[tuple[str, str, int, int]]:
        return self.q.kind_counts()

    def volume_summary(self) -> tuple[int, int]:
        return 0, 0

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
