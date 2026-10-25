#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/store.py
────────────────────
Redis 里的身份池：生产者和消费者只通过这里交互，可分属不同进程/机器。

一个身份 = 出口 IP + 该 IP 过盾得到的 cookie + UA。cf_clearance 绑定 IP 与 UA，
三者必须一起用；过期时间取 cookie 过期与 IP 到期中较早者。

键（prefix 默认 paperlite，host 如 dl.acm.org）：
  {prefix}:{host}:id:{id}     hash  身份内容，EXPIREAT = 身份过期时间
  {prefix}:{host}:ready       zset  空闲身份，score = 过期时间
  {prefix}:{host}:inuse       zset  被下载线程占用的身份，score = 过期时间
  {prefix}:{host}:lease:{id}  str   占用者，带 TTL；心跳续期，进程崩溃后到期由 reap() 归还
  {prefix}:{host}:workers     zset  在线下载线程，score = 最近心跳；生产者据此决定生产多少
  {prefix}:{host}:producer    str   当前生产者，带 TTL；同一主机同时只有一个生产者在干活，其余待命
  {prefix}:{host}:fresh_rejects  int  连续"一篇没下成就被拒"的新身份数；任一身份下载成功即清零
  {prefix}:{host}:paused      str   熔断暂停生产，带 TTL

Redis 里不存代理密码，只存 ip:port；消费者用本地配置补鉴权。
"""

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

import redis

WORKER_STALE_S = 60   # 下载线程超过此时长无心跳即视为离线

# 原子地取出一个剩余寿命 ≥ min_left 的空闲身份（优先最早过期的，先用掉快过期的），转入 inuse 并加占用锁。
_CLAIM_LUA = """
local ready, inuse = KEYS[1], KEYS[2]
local now, min_exp, ttl, holder, lease_prefix = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5]
redis.call('ZREMRANGEBYSCORE', ready, '-inf', now)
local ids = redis.call('ZRANGEBYSCORE', ready, min_exp, '+inf', 'WITHSCORES', 'LIMIT', 0, 1)
if #ids == 0 then return false end
redis.call('ZREM', ready, ids[1])
redis.call('ZADD', inuse, ids[2], ids[1])
redis.call('SET', lease_prefix .. ids[1], holder, 'EX', ttl)
return ids[1]
"""


# 持有者是自己就续期，没人持有就抢；返回 1 表示持有
_HOLD_LUA = """
local cur = redis.call('GET', KEYS[1])
if cur == ARGV[1] or not cur then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
end
return 0
"""

_UNHOLD_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


@dataclass
class Identity:
    host: str
    exit_ip: str
    proxy_server: str                 # ip:port，不含鉴权
    user_agent: str
    cookies: list[dict]
    expires_at: float
    created_at: float = field(default_factory=time.time)
    fingerprint: str = ""
    pass_s: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def left(self, now: Optional[float] = None) -> float:
        return self.expires_at - (time.time() if now is None else now)

    def to_redis(self) -> dict[str, str]:
        d = asdict(self)
        d["cookies"] = json.dumps(self.cookies, ensure_ascii=False)
        return {k: str(v) for k, v in d.items()}

    @classmethod
    def from_redis(cls, d: dict[str, str]) -> "Identity":
        return cls(
            host=d["host"], exit_ip=d["exit_ip"], proxy_server=d["proxy_server"],
            user_agent=d["user_agent"], cookies=json.loads(d["cookies"]),
            expires_at=float(d["expires_at"]), created_at=float(d["created_at"]),
            fingerprint=d.get("fingerprint", ""), pass_s=float(d.get("pass_s", 0)), id=d["id"],
        )


@dataclass(frozen=True)
class PoolStats:
    ready: int
    inuse: int
    healthy: int          # 剩余寿命 > refill_before_s 的 ready + inuse
    workers: int


class IdentityStore:
    def __init__(self, url: str, prefix: str = "paperlite") -> None:
        self.r = redis.Redis.from_url(url, decode_responses=True)
        self.prefix = prefix
        self._claim = self.r.register_script(_CLAIM_LUA)
        self._hold = self.r.register_script(_HOLD_LUA)
        self._unhold = self.r.register_script(_UNHOLD_LUA)

    def _k(self, host: str, *parts: str) -> str:
        return ":".join((self.prefix, host) + parts)

    def ping(self) -> None:
        self.r.ping()

    # ── 生产者 ────────────────────────────────────────────────

    def add(self, ident: Identity) -> None:
        key = self._k(ident.host, "id", ident.id)
        with self.r.pipeline() as p:
            p.hset(key, mapping=ident.to_redis())
            p.expireat(key, int(ident.expires_at) + 1)
            p.zadd(self._k(ident.host, "ready"), {ident.id: ident.expires_at})
            p.execute()

    def hold_producer(self, host: str, holder: str, ttl_s: float) -> bool:
        """抢占/续期该主机的生产者锁；进程崩溃后 ttl_s 内由待命的生产者接手。"""
        return bool(self._hold(keys=[self._k(host, "producer")], args=[holder, int(ttl_s)]))

    def release_producer(self, host: str, holder: str) -> None:
        self._unhold(keys=[self._k(host, "producer")], args=[holder])

    def producer_holder(self, host: str) -> Optional[str]:
        return self.r.get(self._k(host, "producer"))

    def stats(self, host: str, refill_before_s: float = 0) -> PoolStats:
        now = time.time()
        healthy_from = now + refill_before_s
        with self.r.pipeline() as p:
            p.zcount(self._k(host, "ready"), now, "+inf")
            p.zcount(self._k(host, "inuse"), now, "+inf")
            p.zcount(self._k(host, "ready"), healthy_from, "+inf")
            p.zcount(self._k(host, "inuse"), healthy_from, "+inf")
            p.zcount(self._k(host, "workers"), now - WORKER_STALE_S, "+inf")
            ready, inuse, h_ready, h_inuse, workers = p.execute()
        return PoolStats(ready, inuse, h_ready + h_inuse, workers)

    def list(self, host: str) -> list[Identity]:
        ids = self.r.zrange(self._k(host, "ready"), 0, -1) + self.r.zrange(self._k(host, "inuse"), 0, -1)
        out = []
        for ident_id in ids:
            raw = self.r.hgetall(self._k(host, "id", ident_id))
            if raw:
                out.append(Identity.from_redis(raw))
        return sorted(out, key=lambda i: i.expires_at)

    def reap(self, host: str) -> int:
        """清理过期身份和离线线程；占用锁已过期（占用者崩溃）的身份归还 ready。返回归还数。"""
        now = time.time()
        ready, inuse = self._k(host, "ready"), self._k(host, "inuse")
        self.r.zremrangebyscore(ready, "-inf", now)
        self.r.zremrangebyscore(self._k(host, "workers"), "-inf", now - WORKER_STALE_S)
        for ident_id in self.r.zrangebyscore(inuse, "-inf", now):
            self._drop(host, ident_id)
        returned = 0
        for ident_id, exp in self.r.zrange(inuse, 0, -1, withscores=True):
            if not self.r.exists(self._k(host, "lease", ident_id)):
                with self.r.pipeline() as p:
                    p.zrem(inuse, ident_id)
                    p.zadd(ready, {ident_id: exp})
                    p.execute()
                returned += 1
        return returned

    # ── 消费者 ────────────────────────────────────────────────

    def claim(self, host: str, holder: str, min_left_s: float, lease_ttl_s: float) -> Optional[Identity]:
        now = time.time()
        while True:
            ident_id = self._claim(
                keys=[self._k(host, "ready"), self._k(host, "inuse")],
                args=[now, now + min_left_s, int(lease_ttl_s), holder, self._k(host, "lease", "")],
            )
            if not ident_id:
                return None
            raw = self.r.hgetall(self._k(host, "id", ident_id))
            if raw:
                return Identity.from_redis(raw)
            self._drop(host, ident_id)   # hash 已过期而 zset 残留，丢掉再取

    def heartbeat(self, ident: Identity, lease_ttl_s: float) -> None:
        self.r.expire(self._k(ident.host, "lease", ident.id), int(lease_ttl_s))

    def release(self, ident: Identity) -> None:
        """身份仍可用，还回 ready 给别的线程。"""
        with self.r.pipeline() as p:
            p.zrem(self._k(ident.host, "inuse"), ident.id)
            p.delete(self._k(ident.host, "lease", ident.id))
            if ident.left() > 0:
                p.zadd(self._k(ident.host, "ready"), {ident.id: ident.expires_at})
            p.execute()

    # ── 熔断：新身份接连一用就被拒时暂停生产，别把代理配额烧光 ──────────────

    def note_success(self, host: str) -> None:
        self.r.set(self._k(host, "fresh_rejects"), 0)

    def note_fresh_reject(self, host: str) -> int:
        return int(self.r.incr(self._k(host, "fresh_rejects")))

    def fresh_rejects(self, host: str) -> int:
        return int(self.r.get(self._k(host, "fresh_rejects")) or 0)

    def trip(self, host: str, pause_s: float, threshold: int) -> None:
        """暂停生产 pause_s 秒；计数退到 threshold-1，恢复后只要再有一个新身份被拒就再次暂停。"""
        with self.r.pipeline() as p:
            p.set(self._k(host, "paused"), "1", ex=int(pause_s))
            p.set(self._k(host, "fresh_rejects"), max(0, threshold - 1))
            p.execute()

    def paused_for(self, host: str) -> int:
        """熔断剩余秒数，未暂停为 0。"""
        return max(0, int(self.r.ttl(self._k(host, "paused"))))

    def discard(self, ident: Identity) -> None:
        """cookie 被拒、IP 掉线或寿命将尽：彻底删除。"""
        self._drop(ident.host, ident.id)

    def worker_alive(self, host: str, worker: str) -> None:
        self.r.zadd(self._k(host, "workers"), {worker: time.time()})

    def worker_gone(self, host: str, worker: str) -> None:
        self.r.zrem(self._k(host, "workers"), worker)

    def _drop(self, host: str, ident_id: str) -> None:
        with self.r.pipeline() as p:
            p.zrem(self._k(host, "ready"), ident_id)
            p.zrem(self._k(host, "inuse"), ident_id)
            p.delete(self._k(host, "id", ident_id), self._k(host, "lease", ident_id))
            p.execute()
