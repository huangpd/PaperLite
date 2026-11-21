#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/taskq/queue.py
────────────────────────
任务队列（PostgreSQL）：按块领取，一台或多台机器共享一个批次。设计说明见 docs/development/pg-chunk-lease.md。

  - 块 = 按 (host, doi) 排好序后连续的一段任务（默认 1000 条），导入后由 build_chunks 切好，任务记 chunk_id
  - 机器一次领一块（pl_chunks 上 FOR UPDATE SKIP LOCKED），带租约；续租失败说明块被别人回收了
  - 块内任务领取时不改状态（不写 running），结果攒批回写；回写只改仍是 pending 的行，重复上报无害
  - 该稍后重试的任务记 next_attempt_at，留在原块；块做完即 done。requeue_leftovers 把到点的剩余任务
    重新切成下一代块（generation + 1）：被挑战 / 要重试的任务排到后面
  - 租约过期（机器挂了）的块在下一次领取时被放回 queued，由别的机器接手
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from paperlite.core.tasks import ERROR_KINDS, Status, Task, UrlType, derive_doi, url_host, url_key, url_problem

import psycopg
from psycopg import sql

logger = logging.getLogger("paperlite")

RETRY = "retry"                       # report 用：放回 pending，next_attempt_at 之后再领
SKIP = "skip"                         # report 用：块内推断跳过，保持 pending、写 skip_reason，不再领取
SKIP_REASONS = ("title_dup",)         # 巡检（同标题重复）

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
    source          text,
    batch_no        text,
    chunk_id        bigint,
    status          text        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'success', 'failed', 'non_pdf')),
    retry_count     int         NOT NULL DEFAULT 0,
    challenge_count int         NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
    skip_reason     text        CHECK (skip_reason IN ('title_dup')),  -- 巡检跳过，不再领取
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
ALTER TABLE pl_tasks ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE pl_tasks ADD COLUMN IF NOT EXISTS batch_no text;
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

CREATE TABLE IF NOT EXISTS pl_skip_rules (
    batch       text        NOT NULL,
    doi_prefix  text        NOT NULL,          -- 空串 = 不限 doi
    url_prefix  text        NOT NULL,          -- 空串 = 不限 url
    title       text        NOT NULL,          -- 触发的坏标题（样本）
    support     int         NOT NULL,          -- 已采数据里命中规则的条数
    precision   real        NOT NULL,          -- 命中条目里坏标题的占比
    flagged     int         NOT NULL,          -- 据此跳过的 pending 数（多次触发累加）
    scope       text        NOT NULL,          -- batch = inspect 全批次；chunk = 下载时本机块内
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (batch, doi_prefix, url_prefix)
);
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
        self.batch = batch
        self.worker = worker
        self.last_rejected: list[tuple[dict, str]] = []    # 最近一次 import_urls 剔除的 (原始行, 原因)
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._lock:
            if schema:
                self._conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
                self._conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            # 多台机器同时启动会并发建表：用 advisory lock 串行化 DDL
            with self._conn.transaction():
                # 锁名沿用旧模块名（pgqueue / store）：新旧版本同时连一个库时仍互斥，不要改
                self._conn.execute("SELECT pg_advisory_xact_lock(hashtext('paperlite.pgqueue.schema'))")
                self._conn.execute(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 导入与切块 ──────────────────────────────────────────

    def import_urls(self, rows: Iterable[dict]) -> tuple[int, int]:
        """
        rows：dict，含 url（或 pdf_url），可选 doi / source / batch_no / url_type。先 COPY 进临时表，
        再按 (host, doi) 排序插入：id 顺序即领取顺序，导入前不需要排序。返回 (新增, 已存在)。
        明显下载不到的链接（内网 IP、主机名不合法等，见 tasks.url_problem）不入库，记在 last_rejected 里。
        """
        seen = 0
        self.last_rejected = []
        with self._lock, self._conn.transaction():
            c = self._conn
            c.execute("CREATE TEMP TABLE pl_import (url_key bytea, doi text, url text, url_type text, host text, "
                      "source text, batch_no text) ON COMMIT DROP")
            with c.cursor().copy("COPY pl_import FROM STDIN") as cp:
                for r in rows:
                    url = (r.get("url") or r.get("pdf_url") or "").strip()
                    problem = url_problem(url)
                    if problem:
                        self.last_rejected.append((r, problem))
                        continue
                    h = url_host(url)
                    doi = (r.get("doi") or r.get("paper_uid") or "").strip().lower() or derive_doi(url)
                    kind = (r.get("url_type") or UrlType.PDF).strip().lower()
                    if kind not in UrlType.ALL:
                        raise ValueError(f"url_type 只能是 {' / '.join(UrlType.ALL)}，不是 {kind!r}: {url}")
                    cp.write_row((url_key(url), doi, url, kind, h,
                                  (r.get("source") or "").strip() or None, (r.get("batch_no") or "").strip() or None))
                    seen += 1
            # 同一 url 多行只留 doi 最小的一行；按 (host, doi) 顺序插入，id 顺序即领取顺序
            added = c.execute(
                "INSERT INTO pl_tasks (batch, url_key, doi, url, url_type, host, source, batch_no) "
                "SELECT %s, url_key, doi, url, url_type, host, source, batch_no FROM ("
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
            # 多台机器同时 import / requeue：串行切块，否则会把同一批任务切进两套块
            c.execute("SELECT pg_advisory_xact_lock(hashtext('paperlite.pgqueue.chunk:' || %s))", (self.batch,))
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
        """还有可领的：queued 块里有可领任务，或 done 块里到点待重试的剩余任务（requeue_leftovers 能切出新块）。
        只看块是否 queued 不够：块里任务可能全被巡检跳过，领到也是空块。"""
        with self._lock:
            return self._conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pl_chunks k JOIN pl_tasks t ON t.chunk_id = k.chunk_id "
                "  WHERE k.batch = %(b)s AND k.status IN ('queued', 'done') AND t.status = 'pending' "
                "  AND t.skip_reason IS NULL AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now()))",
                {"b": self.batch}).fetchone()[0]

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
            if reason in (None, "title_dup"):
                c.execute("DELETE FROM pl_skip_rules WHERE batch = %s", (self.batch,))
        if n:
            self.build_chunks(size, generation=gen)
        return n

    # ── 巡检（全批次，paperlite inspect 用；下载时的块内巡检见 PgTaskSource）──────

    @staticmethod
    def _prefix_where(doi_prefix: str, url_prefix: str) -> tuple[str, list]:
        """前缀匹配用 left()（避免 LIKE 的 % _ 通配问题）；空前缀 = 不限该维度。"""
        where, args = "", []
        if doi_prefix:
            where += " AND left(doi, %s) = %s"; args += [len(doi_prefix), doi_prefix]
        if url_prefix:
            where += " AND left(url, %s) = %s"; args += [len(url_prefix), url_prefix]
        return where, args

    def sample_pending_url(self, host: str) -> Optional[str]:
        """该主机一条还没下载、也没被挑战过的链接（从待领 / 领取中的块里取，走块索引，不扫全表）。
        curl 模式的生产者在批次刚开始、还没有成功链接时，用它验收新身份。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT t.url FROM pl_chunks k JOIN pl_tasks t ON t.chunk_id = k.chunk_id "
                "WHERE k.batch = %s AND k.host = %s AND k.status IN ('queued', 'leased') "
                "AND t.status = 'pending' AND t.skip_reason IS NULL AND t.challenge_count = 0 LIMIT 1",
                (self.batch, host)).fetchone()
        return row[0] if row else None

    def crawled_titles(self, cap: int = 100_000) -> list[tuple[str, str, str]]:
        """已采成功任务的 (doi, url, title)，供巡检归纳；超量按 id 取前 cap 条。"""
        with self._lock:
            return [tuple(r) for r in self._conn.execute(
                "SELECT doi, url, title FROM pl_tasks WHERE batch = %s AND status = 'success' "
                "AND title IS NOT NULL ORDER BY id LIMIT %s", (self.batch, cap))]

    def count_pending_matching(self, doi_prefix: str, url_prefix: str) -> int:
        """全批次未采（pending 且未跳过）任务里 doi/url 命中前缀的条数。"""
        where, args = self._prefix_where(doi_prefix, url_prefix)
        with self._lock:
            return self._conn.execute(
                "SELECT count(*) FROM pl_tasks WHERE batch = %s AND status = 'pending' AND skip_reason IS NULL"
                + where, [self.batch, *args]).fetchone()[0]

    def flag_suspects(self, doi_prefix: str, url_prefix: str, title: str, support: int, precision: float) -> int:
        """全批次：命中规则的 pending 记 skip_reason = title_dup（不再领取），规则记入 pl_skip_rules。
        已在别的机器本地队列里的任务这次照常下载（它们不会再回库查标记）。返回本次跳过数。"""
        where, args = self._prefix_where(doi_prefix, url_prefix)
        msg = f"巡检：标题 {title[:80]!r} 重复，规则 doi~{doi_prefix!r} url~{url_prefix!r}（precision {precision:.3f}），未请求"
        with self._lock, self._conn.transaction():
            n = self._conn.execute(
                "UPDATE pl_tasks SET skip_reason = 'title_dup', error_message = %s, updated_at = now() "
                "WHERE batch = %s AND status = 'pending' AND skip_reason IS NULL" + where,
                [msg, self.batch, *args]).rowcount
            self._record_rule(doi_prefix, url_prefix, title, support, precision, n, "batch")
        return n

    def record_rule(self, doi_prefix: str, url_prefix: str, title: str, support: int, precision: float,
                    flagged: int, scope: str) -> None:
        with self._lock:
            self._record_rule(doi_prefix, url_prefix, title, support, precision, flagged, scope)

    def _record_rule(self, doi_prefix, url_prefix, title, support, precision, flagged, scope) -> None:
        """调用方持锁。同一规则多次触发（多台机器各自块内触发）累加 flagged。"""
        self._conn.execute(
            "INSERT INTO pl_skip_rules (batch, doi_prefix, url_prefix, title, support, precision, flagged, scope) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (batch, doi_prefix, url_prefix) DO UPDATE SET "
            "flagged = pl_skip_rules.flagged + excluded.flagged, support = greatest(pl_skip_rules.support, excluded.support)",
            (self.batch, doi_prefix, url_prefix, title, support, round(precision, 4), flagged, scope))

    def skip_rules(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT doi_prefix, url_prefix, title, support, precision, flagged, scope, created_at "
                "FROM pl_skip_rules WHERE batch = %s ORDER BY created_at", (self.batch,))
            names = [d.name for d in cur.description]
            return [dict(zip(names, r)) for r in cur]

    # ── 重试与导出 ──────────────────────────────────────────

    def reset(self, status: str, error_kind: Optional[str] = None) -> int:
        """把指定状态（可再按 error_kind 筛）的任务放回 pending、清零重试计数；之后由 requeue_leftovers
        切进下一代块（任一下载机领不到块时会自动切）。返回放回的任务数。"""
        if status not in Status.FINAL:
            raise ValueError(f"只能重置 {', '.join(Status.FINAL)}")
        with self._lock:
            return self._conn.execute(
                "UPDATE pl_tasks SET status = 'pending', retry_count = 0, challenge_count = 0, next_attempt_at = NULL, "
                "updated_at = now() WHERE batch = %s AND status = %s AND (%s::text IS NULL OR error_kind = %s)",
                (self.batch, status, error_kind, error_kind)).rowcount

    EXPORT_COLUMNS = ("id", "doi", "url", "url_type", "host", "source", "batch_no", "final_url", "title", "status",
                      "error_kind", "skip_reason", "http_status", "file_size", "elapsed_ms", "exit_ip", "retry_count",
                      "challenge_count", "file_path", "error_message", "worker", "chunk_id", "updated_at")

    def rows(self, status: Optional[str] = None, fetch: int = 10_000):
        """逐行产出导出用的任务（按 id）；用服务端游标，几百万行也不占满内存。"""
        cols = ", ".join(self.EXPORT_COLUMNS)
        with self._lock, self._conn.transaction():
            with self._conn.cursor(name="pl_export") as cur:
                cur.itersize = fetch
                cur.execute(f"SELECT {cols} FROM pl_tasks WHERE batch = %s AND (%s::text IS NULL OR status = %s) "
                            "ORDER BY id", (self.batch, status, status))
                yield from cur

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
        """本批次的主机，任务多的在前。第一个用作 curl 模式的主机和日志 / 监控里的主机标签：不按排序取第一个——
        数据里混进一条 https://.asm.org/… 这样的坏链接时，排序第一的就是它。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT host FROM pl_chunks WHERE batch = %s GROUP BY host ORDER BY sum(size) DESC, host",
                (self.batch,)).fetchall()
            if not rows:                    # 还没切块
                rows = self._conn.execute("SELECT host FROM pl_tasks WHERE batch = %s GROUP BY host "
                                          "ORDER BY count(*) DESC, host", (self.batch,)).fetchall()
        return [r[0] for r in rows]

    def kind_counts(self) -> list[tuple[str, str, int]]:
        with self._lock:
            return [(s, k, n) for s, k, n in self._conn.execute(
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
