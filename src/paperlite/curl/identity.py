#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/curl/identity.py
──────────────────────────
curl 模式的身份池，存在 PostgreSQL（与任务队列同一个库、同一个 [pg] schema）：生产者和消费者只通过这里交互，
可分属不同进程 / 机器。

一个身份 = 出口 IP + 该 IP 过盾得到的 cookie + UA。cf_clearance 绑定 IP 与 UA，
三者必须一起用；过期时间取 cookie 过期与 IP 到期中较早者。

表都是 UNLOGGED：内容是临时的（身份最多活十几分钟），不写 WAL，频繁更新更快；PG 崩溃后清空，生产者重新生产即可。
  pl_identities   一行一个身份。holder 为空 = 空闲；holder 非空且 lease_until 未到 = 被下载线程占用；
                  租约过期（占用者崩溃）的身份领取时当作空闲。领取用 FOR UPDATE SKIP LOCKED，多线程多机不重复。
  pl_id_hosts     一行一个主机：生产者锁（producer + producer_until，同一主机同时只有一个生产者在干活）、
                  熔断计数 fresh_rejects、暂停到期 paused_until、最近成功链接 good_url。
  pl_id_workers   在线下载线程（最近心跳）；生产者据此决定生产多少。
  pl_id_pending   各下载进程上报的可领取任务数（数到线程数为止）；生产目标不超过各进程之和，没有消费者就不生产。

时间都用 epoch 秒（与 Identity.expires_at 一致），由调用方的时钟给出；各机器要做时钟同步（NTP）。
库里不存代理密码，只存 ip:port；消费者用本地配置补鉴权。
"""

import json
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from http.cookiejar import CookieJar
from typing import Optional

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from paperlite.curl.cookies import jar_from_records, jar_to_records

WORKER_STALE_S = 60   # 下载线程超过此时长无心跳即视为离线
PENDING_STALE_S = 60  # 下载进程超过此时长未上报可领取任务数即视为离线，其上报作废
_GOOD_URL_TTL_S = 86400            # 最近成功链接保留一天：批次停了很久再跑时它可能已不代表现状

_SCHEMA = """
CREATE UNLOGGED TABLE IF NOT EXISTS pl_identities (
    id          text PRIMARY KEY,
    host        text NOT NULL,
    data        jsonb NOT NULL,                   -- Identity.to_dict()：ip、UA、cookie jar……
    expires_at  double precision NOT NULL,
    holder      text,                             -- 空 = 空闲
    lease_until double precision                  -- 占用租约到期（epoch 秒）；过期视为空闲
) WITH (fillfactor = 70);
CREATE INDEX IF NOT EXISTS pl_identities_host ON pl_identities (host, expires_at);
CREATE UNLOGGED TABLE IF NOT EXISTS pl_id_hosts (
    host           text PRIMARY KEY,
    producer       text,
    producer_until double precision,
    fresh_rejects  int NOT NULL DEFAULT 0,
    paused_until   double precision,
    good_url       text,
    good_until     double precision
);
CREATE UNLOGGED TABLE IF NOT EXISTS pl_id_workers (
    host text NOT NULL, worker text NOT NULL, last_seen double precision NOT NULL,
    PRIMARY KEY (host, worker)
);
CREATE UNLOGGED TABLE IF NOT EXISTS pl_id_pending (
    host text NOT NULL, consumer text NOT NULL, n int NOT NULL, ts double precision NOT NULL,
    PRIMARY KEY (host, consumer)
);
"""


@dataclass
class Identity:
    host: str
    exit_ip: str
    proxy_server: str                 # ip:port，不含鉴权
    user_agent: str
    cookies: CookieJar
    expires_at: float
    created_at: float = field(default_factory=time.time)
    fingerprint: str = ""
    pass_s: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    _holder: str = field(default="", init=False, repr=False, compare=False)

    def left(self, now: Optional[float] = None) -> float:
        return self.expires_at - (time.time() if now is None else now)

    def to_dict(self) -> dict[str, str]:
        data = {item.name: str(getattr(self, item.name)) for item in fields(self)
                if item.name not in ("cookies", "_holder")}
        data.update(cookies=self.cookies_json(), cookies_format="cookiejar-v1")
        return data

    def cookies_json(self) -> str:
        return json.dumps(jar_to_records(self.cookies), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "Identity":
        if d.get("cookies_format") not in (None, "cookiejar-v1"):
            raise ValueError("不支持的 cookie 格式")
        return cls(
            host=d["host"], exit_ip=d["exit_ip"], proxy_server=d["proxy_server"],
            user_agent=d["user_agent"], cookies=jar_from_records(json.loads(d["cookies"])),
            expires_at=float(d["expires_at"]), created_at=float(d["created_at"]),
            fingerprint=d.get("fingerprint", ""), pass_s=float(d.get("pass_s", 0)), id=d["id"],
        )


@dataclass(frozen=True)
class PoolStats:
    ready: int
    inuse: int
    healthy: int          # 剩余寿命 > refill_before_s 的 ready + inuse
    workers: int
    pending: Optional[int] = None   # 在线下载进程上报的可领取任务数之和；没有任何上报为 None


class IdentityStore:
    """一个进程一个实例；生产者线程和各下载线程共用一条连接（加锁），每次操作都是一两条短语句。"""

    def __init__(self, dsn: str, schema: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._lock:
            if schema:
                self._conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
                self._conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            with self._conn.transaction():
                # 锁名沿用旧模块名（pgqueue / store）：新旧版本同时连一个库时仍互斥，不要改
                self._conn.execute("SELECT pg_advisory_xact_lock(hashtext('paperlite.store.schema'))")
                self._conn.execute(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _x(self, query: str, params=()):
        with self._lock:
            return self._conn.execute(query, params)

    def _one(self, query: str, params=()):
        with self._lock:
            return self._conn.execute(query, params).fetchone()

    def ping(self) -> None:
        self._x("SELECT 1")

    def _host_row(self, host: str) -> None:
        """调用方持锁：保证 pl_id_hosts 有这一行。"""
        self._conn.execute("INSERT INTO pl_id_hosts (host) VALUES (%s) ON CONFLICT DO NOTHING", (host,))

    # ── 生产者 ────────────────────────────────────────────────

    def add(self, ident: Identity) -> None:
        self._x("INSERT INTO pl_identities (id, host, data, expires_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET data = excluded.data, expires_at = excluded.expires_at, "
                "holder = NULL, lease_until = NULL",
                (ident.id, ident.host, Jsonb(ident.to_dict()), ident.expires_at))

    def hold_producer(self, host: str, holder: str, ttl_s: float) -> bool:
        """抢占 / 续期该主机的生产者锁；进程崩溃后 ttl_s 内由待命的生产者接手。"""
        now = time.time()
        with self._lock:
            self._host_row(host)
            return self._conn.execute(
                "UPDATE pl_id_hosts SET producer = %s, producer_until = %s WHERE host = %s "
                "AND (producer IS NULL OR producer = %s OR producer_until < %s)",
                (holder, now + ttl_s, host, holder, now)).rowcount == 1

    def release_producer(self, host: str, holder: str) -> None:
        self._x("UPDATE pl_id_hosts SET producer = NULL, producer_until = NULL WHERE host = %s AND producer = %s",
                (host, holder))

    def producer_holder(self, host: str) -> Optional[str]:
        row = self._one("SELECT producer FROM pl_id_hosts WHERE host = %s AND producer_until >= %s",
                        (host, time.time()))
        return row[0] if row else None

    def stats(self, host: str, refill_before_s: float = 0) -> PoolStats:
        now = time.time()
        ready, inuse, healthy = self._one(
            "SELECT count(*) FILTER (WHERE holder IS NULL OR lease_until < %(now)s), "
            "       count(*) FILTER (WHERE holder IS NOT NULL AND lease_until >= %(now)s), "
            "       count(*) FILTER (WHERE expires_at >= %(h)s) "
            "FROM pl_identities WHERE host = %(host)s AND expires_at >= %(now)s",
            {"now": now, "h": now + refill_before_s, "host": host})
        workers = self._one("SELECT count(*) FROM pl_id_workers WHERE host = %s AND last_seen >= %s",
                            (host, now - WORKER_STALE_S))[0]
        return PoolStats(ready, inuse, healthy, workers, self.pending(host))

    def list(self, host: str) -> list[Identity]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM pl_identities WHERE host = %s AND expires_at >= %s "
                                      "ORDER BY expires_at", (host, time.time())).fetchall()
        return [Identity.from_dict(r[0]) for r in rows]

    def reap(self, host: str) -> int:
        """清理过期身份、离线线程和过期上报；占用租约已过期（占用者崩溃）的身份归还空闲。返回归还数。"""
        now = time.time()
        with self._lock, self._conn.transaction():
            c = self._conn
            c.execute("DELETE FROM pl_identities WHERE host = %s AND expires_at < %s", (host, now))
            c.execute("DELETE FROM pl_id_workers WHERE host = %s AND last_seen < %s", (host, now - WORKER_STALE_S))
            c.execute("DELETE FROM pl_id_pending WHERE host = %s AND ts < %s", (host, now - PENDING_STALE_S))
            return c.execute("UPDATE pl_identities SET holder = NULL, lease_until = NULL "
                             "WHERE host = %s AND holder IS NOT NULL AND lease_until < %s", (host, now)).rowcount

    # ── 消费者 ────────────────────────────────────────────────

    def claim(self, host: str, holder: str, min_left_s: float, lease_ttl_s: float) -> Optional[Identity]:
        """取一个剩余寿命 ≥ min_left_s 的空闲身份（优先最早过期的，先用掉快过期的），加占用租约。"""
        now = time.time()
        with self._lock, self._conn.transaction():
            c = self._conn
            c.execute("DELETE FROM pl_identities WHERE host = %s AND expires_at < %s "
                      "AND (holder IS NULL OR lease_until < %s)", (host, now, now))
            row = c.execute(
                "UPDATE pl_identities SET holder = %(holder)s, lease_until = %(lease)s WHERE id = ("
                "  SELECT id FROM pl_identities WHERE host = %(host)s AND expires_at >= %(min_exp)s "
                "  AND (holder IS NULL OR lease_until < %(now)s) "
                "  ORDER BY expires_at LIMIT 1 FOR UPDATE SKIP LOCKED"
                ") RETURNING data",
                {"holder": holder, "lease": now + lease_ttl_s, "host": host, "min_exp": now + min_left_s,
                 "now": now}).fetchone()
        if row is None:
            return None
        ident = Identity.from_dict(row[0])
        ident._holder = holder
        return ident

    def heartbeat(self, ident: Identity, lease_ttl_s: float) -> None:
        """续占用租约并保存最新 Cookie Jar；租约已不归自己（被回收后别人领走）时什么都不改。"""
        self._x("UPDATE pl_identities SET data = jsonb_set(data, '{cookies}', to_jsonb(%s::text)), lease_until = %s "
                "WHERE id = %s AND holder = %s",
                (ident.cookies_json(), time.time() + lease_ttl_s, ident.id, ident._holder))

    def release(self, ident: Identity) -> None:
        """保存最新 Cookie Jar，再将仍可用的身份还回空闲；已过期的直接删掉。租约不归自己时什么都不改。"""
        now = time.time()
        with self._lock, self._conn.transaction():
            c = self._conn
            if ident.expires_at <= now:
                c.execute("DELETE FROM pl_identities WHERE id = %s AND holder = %s", (ident.id, ident._holder))
                return
            c.execute("UPDATE pl_identities SET data = jsonb_set(data, '{cookies}', to_jsonb(%s::text)), "
                      "holder = NULL, lease_until = NULL WHERE id = %s AND holder = %s",
                      (ident.cookies_json(), ident.id, ident._holder))

    # ── 熔断：新身份接连一用就被拒时暂停生产，别把代理配额烧光 ──────────────

    def note_success(self, host: str) -> None:
        with self._lock:
            self._host_row(host)
            self._conn.execute("UPDATE pl_id_hosts SET fresh_rejects = 0 WHERE host = %s", (host,))

    def note_good_url(self, host: str, url: str) -> None:
        """记下该主机最近一次下载成功的链接（各下载进程共享）：生产者验收新身份、消费者复验身份时拿它当参照。"""
        with self._lock:
            self._host_row(host)
            self._conn.execute("UPDATE pl_id_hosts SET good_url = %s, good_until = %s WHERE host = %s",
                               (url, time.time() + _GOOD_URL_TTL_S, host))

    def good_url(self, host: str) -> Optional[str]:
        row = self._one("SELECT good_url FROM pl_id_hosts WHERE host = %s AND good_until >= %s", (host, time.time()))
        return row[0] if row else None

    def note_fresh_reject(self, host: str) -> int:
        with self._lock:
            self._host_row(host)
            return self._conn.execute("UPDATE pl_id_hosts SET fresh_rejects = fresh_rejects + 1 WHERE host = %s "
                                      "RETURNING fresh_rejects", (host,)).fetchone()[0]

    def fresh_rejects(self, host: str) -> int:
        row = self._one("SELECT fresh_rejects FROM pl_id_hosts WHERE host = %s", (host,))
        return row[0] if row else 0

    def trip(self, host: str, pause_s: float, threshold: int) -> None:
        """暂停生产 pause_s 秒；计数退到 threshold-1，恢复后只要再有一个新身份被拒就再次暂停。"""
        with self._lock:
            self._host_row(host)
            self._conn.execute("UPDATE pl_id_hosts SET paused_until = %s, fresh_rejects = %s WHERE host = %s",
                               (time.time() + pause_s, max(0, threshold - 1), host))

    def paused_for(self, host: str) -> int:
        """熔断剩余秒数，未暂停为 0。"""
        row = self._one("SELECT paused_until FROM pl_id_hosts WHERE host = %s", (host,))
        return max(0, int(row[0] - time.time())) if row and row[0] else 0

    def discard(self, ident: Identity) -> None:
        """cookie 被拒、IP 掉线或寿命将尽：彻底删除。"""
        self._x("DELETE FROM pl_identities WHERE id = %s", (ident.id,))

    def worker_alive(self, host: str, worker: str) -> None:
        self._x("INSERT INTO pl_id_workers (host, worker, last_seen) VALUES (%s, %s, %s) "
                "ON CONFLICT (host, worker) DO UPDATE SET last_seen = excluded.last_seen", (host, worker, time.time()))

    def worker_gone(self, host: str, worker: str) -> None:
        self._x("DELETE FROM pl_id_workers WHERE host = %s AND worker = %s", (host, worker))

    # ── 可领取任务数：消费者驱动生产，没活就不生产 ──────────────

    def report_pending(self, host: str, consumer: str, n: int) -> None:
        """下载进程上报自己还有多少可领取任务（调用方数到线程数为止即可）。"""
        self._x("INSERT INTO pl_id_pending (host, consumer, n, ts) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (host, consumer) DO UPDATE SET n = excluded.n, ts = excluded.ts",
                (host, consumer, max(0, int(n)), time.time()))

    def clear_pending(self, host: str, consumer: str) -> None:
        self._x("DELETE FROM pl_id_pending WHERE host = %s AND consumer = %s", (host, consumer))

    def pending(self, host: str) -> Optional[int]:
        """在线下载进程上报之和；超过 PENDING_STALE_S 未更新的上报视为离线并清掉。没有任何有效上报返回 None。"""
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM pl_id_pending WHERE host = %s AND ts < %s", (host, now - PENDING_STALE_S))
            n, total = self._conn.execute("SELECT count(*), sum(n) FROM pl_id_pending WHERE host = %s",
                                          (host,)).fetchone()
        return int(total) if n else None
