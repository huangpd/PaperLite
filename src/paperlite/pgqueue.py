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

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .tasks import ERROR_KINDS, Status, Task, UrlType, derive_doi, derive_volume, url_host, url_key

try:  # 可选依赖：只有用到 PG 队列时才需要
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover
    psycopg = None
    sql = None

RETRY = "retry"                       # report 用：放回 pending，next_attempt_at 之后再领

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
    volume          text,
    chunk_id        bigint,
    status          text        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'success', 'failed', 'non_pdf')),
    flag            text,
    retry_count     int         NOT NULL DEFAULT 0,
    challenge_count int         NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
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
    """一条任务的结果。status 为 success / non_pdf / failed，或 RETRY（放回，retry_delay_s 后再领）。"""
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
            c.execute("CREATE TEMP TABLE pl_import (url_key bytea, doi text, url text, url_type text, host text, "
                      "volume text) ON COMMIT DROP")
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
                    cp.write_row((url_key(url), doi, url, kind, h, derive_volume(doi)))
                    seen += 1
            # 同一 url 多行只留 doi 最小的一行；按 (host, doi) 顺序插入，id 顺序即领取顺序
            added = c.execute(
                "INSERT INTO pl_tasks (batch, url_key, doi, url, url_type, host, volume) "
                "SELECT %s, url_key, doi, url, url_type, host, volume FROM ("
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
                "AND t.flag IS NULL AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now())",
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
                    "WHERE chunk_id = %s AND status = 'pending' AND flag IS NULL "
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
        finals = [r for r in results if r.status != RETRY]
        retries = [r for r in results if r.status == RETRY]
        for r in finals:
            assert r.status in Status.FINAL, r.status
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
