"""测试用的 PostgreSQL 辅助：每个用例一个独立 schema，用完删掉。

    docker run -d --name paperlite-pg -e POSTGRES_PASSWORD=pl -e POSTGRES_DB=paperlite -p 5439:5432 postgres:16-alpine
    PAPERLITE_TEST_PG=postgresql://postgres:pl@127.0.0.1:5439/paperlite uv run pytest

没设 PAPERLITE_TEST_PG 时，依赖 PG 的用例跳过。
"""

import os
import unittest
import uuid

DSN = os.environ.get("PAPERLITE_TEST_PG")
needs_pg = unittest.skipUnless(DSN, "需要 PostgreSQL：设 PAPERLITE_TEST_PG")


def new_schema(test: unittest.TestCase) -> str:
    """建一个随机 schema 名，用例结束时 DROP ... CASCADE。"""
    schema = "t_" + uuid.uuid4().hex[:10]

    def drop():
        import psycopg
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    test.addCleanup(drop)
    return schema


def make_source(test: unittest.TestCase, batch: str = "t", schema: str | None = None, worker: str = "w",
                **kw):
    """一个独立 schema 上的 PgTaskSource。默认每条结果立即回写（flush_n=1），断言时不用等缓冲。"""
    from paperlite.taskq import PgTaskQueue, PgTaskSource
    schema = schema or new_schema(test)
    q = PgTaskQueue(DSN, batch, schema=schema, worker=worker)
    kw.setdefault("flush_n", 1)
    src = PgTaskSource(q, **kw)
    test.addCleanup(src.close)              # 先 close（cleanup 后进先出），再 drop schema
    return src


def rows(src) -> list[dict]:
    """当前批次全部任务（导出列），按 id。"""
    from paperlite.taskq import PgTaskQueue
    return [dict(zip(PgTaskQueue.EXPORT_COLUMNS, r)) for r in src.q.rows()]


def row(src, url: str) -> dict:
    return next(r for r in rows(src) if r["url"] == url)


def execute(src, sql: str, params=()):
    """直接对任务表执行 SQL（模拟其他机器写入、准备测试数据）。"""
    with src.q._lock:
        return src.q._conn.execute(sql, params)


def pg_config(schema: str, batch: str = "t", extra: str = "") -> str:
    """config.toml 里的 [pg] 段。"""
    return f'[pg]\ndsn = "{DSN}"\nbatch = "{batch}"\nschema = "{schema}"\n{extra}'


def make_store(test: unittest.TestCase, schema: str | None = None):
    """一个独立 schema 上的身份池（curl 模式）。"""
    from paperlite.curl.identity import IdentityStore
    store = IdentityStore(DSN, schema=schema or new_schema(test))
    test.addCleanup(store.close)
    return store


def identity_row(store, ident_id: str):
    """身份池里一行的 (data, holder, lease_until, expires_at)；没有返回 None。"""
    return store._one("SELECT data, holder, lease_until, expires_at FROM pl_identities WHERE id = %s", (ident_id,))
