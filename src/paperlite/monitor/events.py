"""
监控事件的中转表：上报端按批 INSERT，面板端在一个事务里取走（DELETE … RETURNING）、汇总入库。

  events   UNLOGGED：内容只在两端之间停留几秒，不写 WAL；PG 崩溃最多丢这几秒的指标。
           面板长期不开时由面板的 prune 按 backlog_max 截掉最旧的。

上报端只需要对这张表有 INSERT 权限（可给终端单独建一个只能写不能读的 PG 账号）；建表在面板端做，
上报端有权限时也会顺手建（终端比面板先启动时不至于写失败）。
"""

from typing import Any, Iterable

from psycopg import sql
from psycopg.types.json import Jsonb

DDL = """
CREATE UNLOGGED TABLE IF NOT EXISTS events (
    id  bigserial PRIMARY KEY,
    msg jsonb NOT NULL                -- schema.envelope()
);
"""


def use_schema(conn, schema: str, create: bool) -> None:
    """切到监控 schema；create=True 时顺带建 schema 和 events 表（没有建表权限时忽略，只要能写就行）。"""
    if create:
        try:
            with conn.transaction():
                conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
                conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('paperlite.monitor.events'))")
                conn.execute(DDL)
            return
        except Exception:
            pass                       # 只有写权限的终端账号：表由面板建，这里只切 schema
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))


def insert(conn, msgs: Iterable[dict[str, Any]]) -> int:
    """一批事件一次写入（psycopg 的 executemany 走 pipeline，一次往返）。返回条数。"""
    rows = [(Jsonb(m),) for m in msgs]
    if rows:
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO events (msg) VALUES (%s)", rows)
    return len(rows)
