"""
面板端存储（SQLite）。两层：

  原始事件表  page / browser / proxy_check / log      只留 raw_retention_hours（默认 48h），给实时事件流、
                                                       近 5 分钟域名频率、明细排查用
  小时汇总表  page_h / browser_h / proxy_h / proxy_reason_h / fleet_h   留 retention_days（默认 30 天），
                                                       1/7/30 天大屏全部从这里算

入库时每批消息先在内存按 (小时, 维度) 聚合，再与库里的汇总行合并，所以查询代价与事件量无关
（30 天 × 几十个域名也只是几万行）。P95 由每小时的对数分桶直方图合并得出，误差约 ±6%。
时间窗按整小时对齐：「最近 1 天」= 含当前小时在内的最近 24 个小时桶。
"""

import bisect
import json
import math
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from paperlite.monitor import schema

WINDOWS = {"1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
TREND_BUCKET = {"1d": 3600, "7d": 6 * 3600, "30d": 86400}
HOUR = 3600

# 耗时直方图分桶：50ms 起每档 ×1.12，到 10 分钟，约 84 档；取桶的几何中点作分位值（误差约 ±6%）
_EDGES = [50.0 * 1.12 ** i for i in range(int(math.log(600_000 / 50) / math.log(1.12)) + 2)]

_DDL = """
CREATE TABLE IF NOT EXISTS proxy_check (
  ts REAL NOT NULL, inst TEXT, node TEXT, batch TEXT, vendor TEXT,
  ok INTEGER, total_ms REAL, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_proxy_ts ON proxy_check(ts);
CREATE TABLE IF NOT EXISTS browser (
  ts REAL NOT NULL, inst TEXT, node TEXT, batch TEXT, vendor TEXT,
  ok INTEGER, kind TEXT, host TEXT, waf TEXT, seconds REAL, error TEXT);
CREATE INDEX IF NOT EXISTS ix_browser_ts ON browser(ts);
CREATE TABLE IF NOT EXISTS page (
  ts REAL NOT NULL, inst TEXT, node TEXT, batch TEXT, vendor TEXT,
  host TEXT, passed INTEGER, ok INTEGER, status INTEGER, waf TEXT, elapsed_ms INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS ix_page_ts ON page(ts);
CREATE TABLE IF NOT EXISTS log (
  ts REAL NOT NULL, inst TEXT, node TEXT, batch TEXT, vendor TEXT,
  level TEXT, event TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS ix_log_ts ON log(ts);

CREATE TABLE IF NOT EXISTS instance (
  inst TEXT PRIMARY KEY, node TEXT, batch TEXT, vendor TEXT, last_seen REAL,
  mode TEXT, browsers_online INTEGER, workers INTEGER,
  pending INTEGER, running INTEGER, success INTEGER, failed INTEGER);

CREATE TABLE IF NOT EXISTS page_h (
  hour INTEGER, vendor TEXT, host TEXT, waf TEXT,
  n INTEGER, passed INTEGER, ok INTEGER, ms_sum REAL, hist TEXT,      -- 耗时只计 passed
  PRIMARY KEY (hour, vendor, host, waf));
CREATE TABLE IF NOT EXISTS browser_h (
  hour INTEGER, vendor TEXT, host TEXT, waf TEXT, kind TEXT,
  n INTEGER, ok INTEGER, s_sum REAL,                                  -- s_sum 只计 ok
  PRIMARY KEY (hour, vendor, host, waf, kind));
CREATE TABLE IF NOT EXISTS proxy_h (
  hour INTEGER, vendor TEXT, n INTEGER, ok INTEGER, ms_sum REAL,      -- ms_sum 只计 ok
  PRIMARY KEY (hour, vendor));
CREATE TABLE IF NOT EXISTS proxy_reason_h (
  hour INTEGER, vendor TEXT, reason TEXT, n INTEGER,
  PRIMARY KEY (hour, vendor, reason));
CREATE TABLE IF NOT EXISTS fleet_h (
  hour INTEGER, inst TEXT, n INTEGER, online_sum REAL,                -- 该终端该小时心跳的在线浏览器数之和
  PRIMARY KEY (hour, inst));
"""

_EVENT_COLUMNS = {
    schema.PROXY_CHECK: ("ok", "total_ms", "reason"),
    schema.BROWSER: ("ok", "kind", "host", "waf", "seconds", "error"),
    schema.PAGE: ("host", "passed", "ok", "status", "waf", "elapsed_ms", "error"),
    schema.LOG: ("level", "event", "message"),
}
_HB_COLUMNS = ("mode", "browsers_online", "workers", "pending", "running", "success", "failed")


class MonitorDB:
    def __init__(self, path: Path, retention_days: int = 30, raw_retention_hours: int = 48) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_s = retention_days * 86400
        self.raw_retention_s = raw_retention_hours * HOUR
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self.conn.executescript(_DDL)
            self.conn.commit()
        self._pruned_at = 0.0
        self._cache: dict[str, tuple[float, dict]] = {}

    def close(self) -> None:
        self.conn.close()

    # ── 写入 ────────────────────────────────────────────────────────────
    def ingest(self, msgs: Iterable[dict[str, Any]], commit: bool = True) -> int:
        """写原始事件 + 合并进小时汇总。可多次调用后再 commit()（消费端按批提交）。"""
        acc = _Acc()
        n = 0
        with self._lock:
            for m in msgs:
                self._raw(m)
                acc.add(m)
                n += 1
            acc.flush(self.conn)
            if commit:
                self.conn.commit()
        if n:
            self._cache.clear()
        return n

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def _raw(self, m: dict[str, Any]) -> None:
        t, d = m["type"], m["data"]
        if t == schema.HEARTBEAT:
            self.conn.execute(
                "INSERT INTO instance (inst, node, batch, vendor, last_seen, " + ", ".join(_HB_COLUMNS) + ") "
                "VALUES (?, ?, ?, ?, ?, " + ", ".join("?" * len(_HB_COLUMNS)) + ") "
                "ON CONFLICT(inst) DO UPDATE SET node=excluded.node, batch=excluded.batch, "
                "vendor=excluded.vendor, last_seen=MAX(instance.last_seen, excluded.last_seen), "
                + ", ".join(f"{c}=CASE WHEN excluded.last_seen >= instance.last_seen THEN excluded.{c} "
                            f"ELSE instance.{c} END" for c in _HB_COLUMNS),
                (m.get("inst"), m.get("node"), m.get("batch"), m.get("vendor"), float(m["ts"]),
                 *(d.get(c) for c in _HB_COLUMNS)))
            return
        cols = _EVENT_COLUMNS[t]
        self.conn.execute(
            f"INSERT INTO {t} (ts, inst, node, batch, vendor, {', '.join(cols)}) "
            f"VALUES ({', '.join('?' * (5 + len(cols)))})",
            (float(m["ts"]), m.get("inst"), m.get("node"), m.get("batch"), m.get("vendor"),
             *(_sql(d.get(c)) for c in cols)))

    def prune(self, now: Optional[float] = None, force: bool = False) -> int:
        """原始事件留 raw_retention，汇总与终端留 retention（至多每小时做一次）。"""
        now = now or time.time()
        if not force and now - self._pruned_at < HOUR:
            return 0
        self._pruned_at = now
        raw_cut, cut = now - self.raw_retention_s, now - self.retention_s
        hour_cut = int(cut // HOUR)
        n = 0
        with self._lock:
            for t in _EVENT_COLUMNS:
                n += self.conn.execute(f"DELETE FROM {t} WHERE ts < ?", (raw_cut,)).rowcount
            for t in ("page_h", "browser_h", "proxy_h", "proxy_reason_h", "fleet_h"):
                n += self.conn.execute(f"DELETE FROM {t} WHERE hour < ?", (hour_cut,)).rowcount
            n += self.conn.execute("DELETE FROM instance WHERE last_seen < ?", (cut,)).rowcount
            self.conn.commit()
        self._cache.clear()
        return n

    # ── 查询 ────────────────────────────────────────────────────────────
    def summary(self, window: str = "1d", now: Optional[float] = None, online_s: float = 60,
                cache_s: float = 5.0) -> dict[str, Any]:
        """大屏需要的全部数字。多个浏览器同时刷新时 cache_s 内复用同一份结果。"""
        if window not in WINDOWS:
            raise ValueError(f"window 只支持 {', '.join(WINDOWS)}")
        real_now = time.time()
        if now is None and window in self._cache and real_now - self._cache[window][0] < cache_s:
            return self._cache[window][1]
        now = now or real_now
        h1 = int(now // HOUR) + 1                       # 不含
        h0 = h1 - WINDOWS[window] // HOUR               # 含
        p0 = h0 - (h1 - h0)                             # 上一个同长时间窗，用于环比
        with self._lock:
            q = self.conn.execute
            rows = self._page_rows(q, h0, h1)          # page_h 只读一遍、直方图只解一次，下面几块共用
            out = {
                "window": window, "now": now, "since": h0 * HOUR,
                "ip": self._ip(q, h0, h1),
                "browser": self._browser(q, h0, h1, now - online_s),
                "pages": self._pages(rows),
                "domains": self._domains(q, rows, now),
                "vendors": self._vendors(q, h0, h1),
                "waf": self._waf(q, rows, h0, h1),
                "realtime": self._realtime(q, now, online_s),
                "trend": self._trend(q, h0, h1, TREND_BUCKET[window] // HOUR),
            }
            out["prev"] = {"ip": self._ip(q, p0, h0, reasons=False), "pages": self._pages(self._page_rows(q, p0, h0))}
            out["outcomes"] = {"success": out["pages"]["success"],
                               "http_failed": out["pages"]["passed"] - out["pages"]["success"],
                               "not_passed": out["pages"]["attempts"] - out["pages"]["passed"]}
        if now == real_now:
            self._cache[window] = (real_now, out)
        return out

    def _ip(self, q, h0, h1, reasons: bool = True) -> dict:
        r = q("SELECT COALESCE(SUM(n),0) n, COALESCE(SUM(ok),0) ok, SUM(ms_sum) ms FROM proxy_h "
              "WHERE hour >= ? AND hour < ?", (h0, h1)).fetchone()
        out = {"checked": r["n"], "passed": r["ok"], "failed": r["n"] - r["ok"],
               "pass_rate": _rate(r["ok"], r["n"]), "avg_ms": _div(r["ms"], r["ok"])}
        if reasons:
            out["fail_reasons"] = [dict(x) for x in q(
                "SELECT reason, SUM(n) n FROM proxy_reason_h WHERE hour >= ? AND hour < ? "
                "GROUP BY reason ORDER BY n DESC LIMIT 8", (h0, h1)).fetchall()]
        return out

    def _browser(self, q, h0, h1, online_since) -> dict:
        r = q("SELECT COALESCE(SUM(n),0) n, COALESCE(SUM(ok),0) ok, "
              "COALESCE(SUM(CASE WHEN kind = 'exception' THEN n END),0) exc, SUM(s_sum) s "
              "FROM browser_h WHERE hour >= ? AND hour < ?", (h0, h1)).fetchone()
        live = q("SELECT COALESCE(SUM(browsers_online),0) online, COALESCE(SUM(workers),0) workers, "
                 "COUNT(*) inst FROM instance WHERE last_seen >= ?", (online_since,)).fetchone()
        return {"online": live["online"], "target": live["workers"], "instances": live["inst"],
                "starts": r["n"], "start_ok": r["ok"], "start_rate": _rate(r["ok"], r["n"]),
                "exceptions": r["exc"], "avg_pass_s": _div(r["s"], r["ok"], 1)}

    @staticmethod
    def _page_rows(q, h0, h1) -> list[dict]:
        return [{**dict(x), "hist": _merge([x["hist"]])} for x in q(
            "SELECT host, waf, n, passed, ok, ms_sum, hist FROM page_h WHERE hour >= ? AND hour < ?", (h0, h1))]

    def _pages(self, rows: list[dict]) -> dict:
        n = sum(x["n"] for x in rows)
        ok = sum(x["ok"] for x in rows)
        passed = sum(x["passed"] for x in rows)
        hist = _merge(x["hist"] for x in rows)
        return {"attempts": n, "success": ok, "failed": n - ok, "passed": passed,
                "success_rate": _rate(ok, n), "pass_rate": _rate(passed, n),
                "avg_ms": _div(sum(x["ms_sum"] or 0 for x in rows), passed),
                "p95_ms": _pct(hist, 0.95)}

    def _domains(self, q, rows: list[dict], now, limit: int = 50) -> list[dict]:
        by: dict[str, dict] = {}
        for x in rows:
            d = by.setdefault(x["host"], {"host": x["host"], "attempts": 0, "success": 0, "passed": 0,
                                          "ms": 0.0, "hists": [], "wafs": defaultdict(int)})
            d["attempts"] += x["n"]
            d["success"] += x["ok"]
            d["passed"] += x["passed"]
            d["ms"] += x["ms_sum"] or 0
            d["hists"].append(x["hist"])
            d["wafs"][x["waf"]] += x["n"]
        freq = {x["host"]: x["n"] for x in q(
            "SELECT host, COUNT(*) n FROM page WHERE ts >= ? GROUP BY host", (now - 300,)).fetchall()}
        out = []
        for d in sorted(by.values(), key=lambda d: -d["attempts"])[:limit]:
            known = {w: c for w, c in d["wafs"].items() if w in schema.WAFS}
            out.append({"host": d["host"], "attempts": d["attempts"], "success": d["success"],
                        "success_rate": _rate(d["success"], d["attempts"]),
                        "pass_rate": _rate(d["passed"], d["attempts"]),
                        "avg_ms": _div(d["ms"], d["passed"]), "p95_ms": _pct(_merge(d["hists"]), 0.95),
                        "waf": max(known, key=known.get) if known else schema.WAF_NONE,
                        "per_min": round(freq.get(d["host"], 0) / 5, 1)})
        return out

    def _vendors(self, q, h0, h1) -> list[dict]:
        rows = q("SELECT vendor, host, SUM(n) n, SUM(ok) ok, SUM(passed) passed, SUM(ms_sum) ms "
                 "FROM page_h WHERE hour >= ? AND hour < ? GROUP BY vendor, host ORDER BY vendor, n DESC",
                 (h0, h1)).fetchall()
        ips = {x["vendor"]: x for x in q("SELECT vendor, SUM(n) n, SUM(ok) ok FROM proxy_h "
                                         "WHERE hour >= ? AND hour < ? GROUP BY vendor", (h0, h1)).fetchall()}
        vendors: dict[str, dict] = {}
        for x in rows:
            v = vendors.setdefault(x["vendor"], {"vendor": x["vendor"], "attempts": 0, "success": 0, "domains": []})
            v["attempts"] += x["n"]
            v["success"] += x["ok"]
            v["domains"].append({"host": x["host"], "attempts": x["n"], "success": x["ok"],
                                 "success_rate": _rate(x["ok"], x["n"]), "pass_rate": _rate(x["passed"], x["n"]),
                                 "avg_ms": _div(x["ms"], x["passed"])})
        for name in ips:
            vendors.setdefault(name, {"vendor": name, "attempts": 0, "success": 0, "domains": []})
        for v in vendors.values():
            v["success_rate"] = _rate(v["success"], v["attempts"])
            ip = ips.get(v["vendor"])
            v["ip_checked"] = ip["n"] if ip else 0
            v["ip_pass_rate"] = _rate(ip["ok"], ip["n"]) if ip else None
        return sorted(vendors.values(), key=lambda v: -v["attempts"])

    def _waf(self, q, rows: list[dict], h0, h1) -> list[dict]:
        """过盾：页面采集 + 浏览器热身落地页（不含启动异常），凡识别出 WAF 的都算一次；passed = 页面真加载出来。"""
        pages: dict[str, dict] = defaultdict(lambda: {"n": 0, "passed": 0, "ms": 0.0})
        hists = defaultdict(list)
        for x in rows:
            p = pages[x["waf"]]
            p["n"] += x["n"]; p["passed"] += x["passed"]; p["ms"] += x["ms_sum"] or 0
            hists[x["waf"]].append(x["hist"])
        land = {x["waf"]: x for x in q(
            "SELECT waf, SUM(n) n, SUM(ok) ok, SUM(s_sum) s FROM browser_h WHERE hour >= ? AND hour < ? "
            "AND kind != 'exception' GROUP BY waf", (h0, h1)).fetchall()}
        out = []
        for w in schema.WAFS:
            p, b = pages.get(w), land.get(w)     # pages 是 defaultdict，.get 不会凭空建键
            n = (p["n"] if p else 0) + (b["n"] if b else 0)
            passed = (p["passed"] if p else 0) + (b["ok"] if b else 0)
            ms = (p["ms"] or 0 if p else 0) + ((b["s"] or 0) * 1000 if b else 0)
            out.append({"waf": w, "attempts": n, "passed": passed, "pass_rate": _rate(passed, n),
                        "avg_ms": _div(ms, passed), "p95_ms": _pct(_merge(hists.get(w, [])), 0.95)})
        return out

    def _trend(self, q, h0, h1, step: int) -> list[dict]:
        """按 step 小时一档：成功率、过盾率、平均耗时、在线浏览器数（各终端该档平均值之和）、IP 质检通过率。"""
        nb = (h1 - h0 + step - 1) // step
        b = [{"t": (h0 + i * step) * HOUR, "attempts": 0, "success": 0, "passed": 0, "ms": 0.0,
              "ip_n": 0, "ip_ok": 0, "online": defaultdict(lambda: [0, 0.0])} for i in range(nb)]
        idx = lambda h: (h - h0) // step
        for x in q("SELECT hour, SUM(n) n, SUM(ok) ok, SUM(passed) passed, SUM(ms_sum) ms FROM page_h "
                   "WHERE hour >= ? AND hour < ? GROUP BY hour", (h0, h1)).fetchall():
            d = b[idx(x["hour"])]
            d["attempts"] += x["n"]; d["success"] += x["ok"]; d["passed"] += x["passed"]; d["ms"] += x["ms"] or 0
        for x in q("SELECT hour, SUM(n) n, SUM(ok) ok FROM proxy_h WHERE hour >= ? AND hour < ? GROUP BY hour",
                   (h0, h1)).fetchall():
            d = b[idx(x["hour"])]
            d["ip_n"] += x["n"]; d["ip_ok"] += x["ok"]
        for x in q("SELECT hour, inst, n, online_sum FROM fleet_h WHERE hour >= ? AND hour < ?", (h0, h1)).fetchall():
            o = b[idx(x["hour"])]["online"][x["inst"]]
            o[0] += x["n"]; o[1] += x["online_sum"]
        out = []
        for d in b:
            online = round(sum(s / n for n, s in d["online"].values() if n), 1) if d["online"] else None
            out.append({"t": d["t"], "attempts": d["attempts"], "success": d["success"], "passed": d["passed"],
                        "success_rate": _rate(d["success"], d["attempts"]),
                        "pass_rate": _rate(d["passed"], d["attempts"]),
                        "avg_ms": _div(d["ms"], d["passed"]), "online": online,
                        "ip_checked": d["ip_n"], "ip_pass_rate": _rate(d["ip_ok"], d["ip_n"])})
        return out

    def _realtime(self, q, now, online_s) -> dict:
        inst = q("SELECT * FROM instance ORDER BY last_seen DESC LIMIT 200").fetchall()
        instances = [{**dict(x), "online": x["last_seen"] >= now - online_s} for x in inst]
        live = [i for i in instances if i["online"]]
        freq = q("SELECT host, COUNT(*) n FROM page WHERE ts >= ? GROUP BY host ORDER BY n DESC LIMIT 20",
                 (now - 300,)).fetchall()
        events = q(
            "SELECT * FROM ("
            " SELECT ts, inst, 'page' type, (CASE WHEN ok THEN 'info' ELSE 'warn' END) level,"
            "   host || ' ' || COALESCE(status, '-') || ' ' || waf || ' ' || elapsed_ms || 'ms'"
            "   || CASE WHEN error != '' THEN ' ' || error ELSE '' END msg FROM page WHERE ts >= ?"
            " UNION ALL SELECT ts, inst, 'browser', (CASE WHEN ok THEN 'info' ELSE 'warn' END),"
            "   '浏览器' || CASE WHEN ok THEN '就绪 ' ELSE '失败(' || kind || ') ' END || host || ' ' || waf"
            "   || ' ' || round(seconds, 1) || 's' || CASE WHEN error != '' THEN ' ' || error ELSE '' END"
            "   FROM browser WHERE ts >= ?"
            " UNION ALL SELECT ts, inst, 'log', lower(level), message FROM log WHERE ts >= ?"
            ") ORDER BY ts DESC LIMIT 60", (now - HOUR,) * 3).fetchall()
        return {"pending": sum(i["pending"] or 0 for i in live), "running": sum(i["running"] or 0 for i in live),
                "online_instances": len(live), "instances": instances,
                "domain_freq": [{"host": x["host"], "per_min": round(x["n"] / 5, 1)} for x in freq],
                "events": [dict(x) for x in events]}


class _Acc:
    """一批消息在内存里按 (小时, 维度) 聚合，最后一次性与库里的汇总行合并。"""

    def __init__(self) -> None:
        self.page = defaultdict(lambda: [0, 0, 0, 0.0, defaultdict(int)])     # n passed ok ms hist
        self.browser = defaultdict(lambda: [0, 0, 0.0])                       # n ok s
        self.proxy = defaultdict(lambda: [0, 0, 0.0])                         # n ok ms
        self.reason = defaultdict(int)
        self.fleet = defaultdict(lambda: [0, 0.0])

    def add(self, m: dict[str, Any]) -> None:
        t, d, h, v = m["type"], m["data"], int(float(m["ts"]) // HOUR), m.get("vendor") or "unknown"
        if t == schema.PAGE:
            a = self.page[(h, v, d.get("host") or "", d.get("waf") or schema.WAF_NONE)]
            a[0] += 1
            if d.get("passed"):
                ms = float(d.get("elapsed_ms") or 0)
                a[1] += 1; a[3] += ms; a[4][_bucket(ms)] += 1
            if d.get("ok"):
                a[2] += 1
        elif t == schema.BROWSER:
            a = self.browser[(h, v, d.get("host") or "", d.get("waf") or schema.WAF_NONE, d.get("kind") or "")]
            a[0] += 1
            if d.get("ok"):
                a[1] += 1; a[2] += float(d.get("seconds") or 0)
        elif t == schema.PROXY_CHECK:
            a = self.proxy[(h, v)]
            a[0] += 1
            if d.get("ok"):
                a[1] += 1; a[2] += float(d.get("total_ms") or 0)
            else:
                self.reason[(h, v, reason_bucket(d.get("reason")))] += 1
        elif t == schema.HEARTBEAT:
            a = self.fleet[(h, m.get("inst") or "")]
            a[0] += 1; a[1] += float(d.get("browsers_online") or 0)

    def flush(self, conn: sqlite3.Connection) -> None:
        for k, (n, passed, ok, ms, hist) in self.page.items():
            old = conn.execute("SELECT hist FROM page_h WHERE hour=? AND vendor=? AND host=? AND waf=?", k).fetchone()
            merged = _merge([old[0] if old else None, hist])
            conn.execute(
                "INSERT INTO page_h VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(hour, vendor, host, waf) DO UPDATE SET "
                "n=n+excluded.n, passed=passed+excluded.passed, ok=ok+excluded.ok, ms_sum=ms_sum+excluded.ms_sum, "
                "hist=excluded.hist", (*k, n, passed, ok, ms, _dump(merged)))
        conn.executemany(
            "INSERT INTO browser_h VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(hour, vendor, host, waf, kind) DO UPDATE SET "
            "n=n+excluded.n, ok=ok+excluded.ok, s_sum=s_sum+excluded.s_sum",
            [(*k, *a) for k, a in self.browser.items()])
        conn.executemany(
            "INSERT INTO proxy_h VALUES (?,?,?,?,?) ON CONFLICT(hour, vendor) DO UPDATE SET "
            "n=n+excluded.n, ok=ok+excluded.ok, ms_sum=ms_sum+excluded.ms_sum",
            [(*k, *a) for k, a in self.proxy.items()])
        conn.executemany(
            "INSERT INTO proxy_reason_h VALUES (?,?,?,?) ON CONFLICT(hour, vendor, reason) DO UPDATE SET n=n+excluded.n",
            [(*k, n) for k, n in self.reason.items()])
        conn.executemany(
            "INSERT INTO fleet_h VALUES (?,?,?,?) ON CONFLICT(hour, inst) DO UPDATE SET "
            "n=n+excluded.n, online_sum=online_sum+excluded.online_sum",
            [(*k, *a) for k, a in self.fleet.items()])


def _bucket(ms: float) -> int:
    return min(bisect.bisect_right(_EDGES, ms), len(_EDGES))


def _merge(hists) -> dict[int, int]:
    out: dict[int, int] = defaultdict(int)
    for h in hists:
        if not h:
            continue
        for k, c in (json.loads(h).items() if isinstance(h, str) else h.items()):
            out[int(k)] += c
    return out


def _dump(hist: dict[int, int]) -> str:
    return json.dumps({str(k): c for k, c in sorted(hist.items())}, separators=(",", ":"))


def _pct(hist: dict[int, int], p: float) -> Optional[int]:
    total = sum(hist.values())
    if not total:
        return None
    need, cum = p * total, 0
    for k in sorted(hist):
        cum += hist[k]
        if cum >= need:
            lo = _EDGES[k - 1] if k > 0 else 0.0
            hi = _EDGES[k] if k < len(_EDGES) else _EDGES[-1] * 1.12
            return int(round(math.sqrt(max(lo, 1.0) * hi)))       # 桶的几何中点
    return None


def _sql(v):
    return int(v) if isinstance(v, bool) else v


def _rate(num, den) -> Optional[float]:
    return round((num or 0) / den, 4) if den else None


def _div(total, n, nd: int = 0):
    if not n or total is None:
        return None
    v = total / n
    return round(v, nd) if nd else int(round(v))


def reason_bucket(reason: Optional[str]) -> str:
    """slow_3456ms / exit_ip_mismatch(1.2.3.4) / ConnectionError: … → slow / exit_ip_mismatch / ConnectionError。"""
    r = reason or "unknown"
    if r.startswith("slow_"):
        return "slow"
    if r.startswith("status_"):
        return r
    return r.split("(", 1)[0].split(":", 1)[0][:60]
