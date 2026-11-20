#!/usr/bin/env python3
"""
压测 PG 队列的调度开销（不下载，只测领取 + 回写）：按块领取 vs 逐条 SKIP LOCKED 领取。

    uv run --extra pg python tools/bench_pg_chunks.py --dsn postgresql://postgres:pl@127.0.0.1:5439/paperlite \
        --tasks 1000000 --workers 100 --chunk 1000

每个 worker 一条连接（模拟 100 台机器各一个进程）。按块模式：领块 → 每 500 条批量回写 → complete。
逐条模式：每条 UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1) 领取，再单条回写。
"""

import argparse
import threading
import time
import uuid

import psycopg

from paperlite import pgqueue


def rows(n):
    for i in range(n):
        yield {"doi": f"10.1/x{i:08d}", "url": f"https://h{i % 5}.example/{i}"}


def bench_chunks(dsn, schema, n, workers, chunk, flush):
    q = pgqueue.PgTaskQueue(dsn, "bench", schema=schema, worker="setup")
    t = time.time()
    q.import_urls(rows(n))
    t_import = time.time() - t
    t = time.time()
    nchunks = q.build_chunks(chunk)
    t_chunk = time.time() - t
    done = [0]
    lock = threading.Lock()

    def run(i):
        w = pgqueue.PgTaskQueue(dsn, "bench", schema=schema, worker=f"w{i}")
        while (ch := w.claim_chunk()) is not None:
            buf = []
            for task in ch.tasks:
                buf.append(pgqueue.Result(task.id, "success", file_size=1))
                if len(buf) >= flush:
                    w.report(buf)
                    buf = []
            if buf:
                w.report(buf)
            w.complete(ch.chunk_id)
            with lock:
                done[0] += len(ch.tasks)
        w.close()

    t = time.time()
    ts = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    el = time.time() - t
    st = q.stats()
    q.close()
    return dict(import_s=t_import, chunk_s=t_chunk, chunks=nchunks, run_s=el, tasks=done[0],
                per_s=done[0] / el, stats=st)


def bench_rows(dsn, schema, n, workers):
    q = pgqueue.PgTaskQueue(dsn, "bench", schema=schema, worker="setup")
    q.import_urls(rows(n))
    q.close()
    done = [0]
    lock = threading.Lock()

    def run(i):
        c = psycopg.connect(dsn, autocommit=True)
        c.execute(f'SET search_path TO "{schema}"')
        k = 0
        while True:
            r = c.execute(
                "UPDATE pl_tasks SET worker = %s, next_attempt_at = now() + interval '30 min' WHERE id = ("
                " SELECT id FROM pl_tasks WHERE batch = 'bench' AND status = 'pending' "
                " AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
                " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id", (f"w{i}",)).fetchone()
            if r is None:
                break
            c.execute("UPDATE pl_tasks SET status = 'success', file_size = 1 WHERE id = %s", r)
            k += 1
        with lock:
            done[0] += k
        c.close()

    t = time.time()
    ts = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    el = time.time() - t
    return dict(run_s=el, tasks=done[0], per_s=done[0] / el)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--tasks", type=int, default=200_000)
    ap.add_argument("--row-tasks", type=int, default=20_000, help="逐条模式的任务数（慢，单独设小一点）")
    ap.add_argument("--workers", type=int, default=100)
    ap.add_argument("--chunk", type=int, default=1000)
    ap.add_argument("--flush", type=int, default=500)
    a = ap.parse_args()
    for name, fn in (("chunks", lambda s: bench_chunks(a.dsn, s, a.tasks, a.workers, a.chunk, a.flush)),
                     ("rows", lambda s: bench_rows(a.dsn, s, a.row_tasks, a.workers))):
        schema = f"bench_{uuid.uuid4().hex[:8]}"
        try:
            print(name, fn(schema), flush=True)
        finally:
            with psycopg.connect(a.dsn, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


if __name__ == "__main__":
    main()
