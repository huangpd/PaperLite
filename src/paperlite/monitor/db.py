"""
面板端存储：监控消息落 SQLite（每类事件一张表，按 ts 建索引），查询时按时间窗（1/7/30 天）现算聚合。
超过 retention_days 的行定期删除。实时量（在线浏览器数等）只留每个终端的最新心跳。
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from paperlite.monitor import schema

WINDOWS = {"1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400}

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
CREATE INDEX IF NOT EXISTS ix_page_host_ts ON page(host, ts);

CREATE TABLE IF NOT EXISTS log (
  ts REAL NOT NULL, inst TEXT, node TEXT, batch TEXT, vendor TEXT,
  level TEXT, event TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS ix_log_ts ON log(ts);

CREATE TABLE IF NOT EXISTS instance (
  inst TEXT PRIMARY KEY, node TEXT, batch TEXT, vendor TEXT, last_seen REAL,
  mode TEXT, browsers_online INTEGER, workers INTEGER,
  pending INTEGER, running INTEGER, success INTEGER, failed INTEGER);
"""

_EVENT_COLUMNS = {
    schema.PROXY_CHECK: ("ok", "total_ms", "reason"),
    schema.BROWSER: ("ok", "kind", "host", "waf", "seconds", "error"),
    schema.PAGE: ("host", "passed", "ok", "status", "waf", "elapsed_ms", "error"),
    schema.LOG: ("level", "event", "message"),
}
_HB_COLUMNS = ("mode", "browsers_online", "workers", "pending", "running", "success", "failed")


class MonitorDB:
    def __init__(self, path: Path, retention_days: int = 30) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_s = retention_days * 86400
        self._lock = threading.Lock()
        self.conn = self._connect()
        with self._lock:
            self.conn.executescript(_DDL)
            self.conn.commit()
        self._pruned_at = 0.0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def close(self) -> None:
        self.conn.close()

    # ── 写入 ──
    def ingest(self, msgs: Iterable[dict[str, Any]], commit: bool = True) -> int:
        n = 0
        with self._lock:
            for m in msgs:
                self._ingest_one(m)
                n += 1
            if commit:
                self.conn.commit()
        return n

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def _ingest_one(self, m: dict[str, Any]) -> None:
        t, d = m["type"], m["data"]
        base = (float(m["ts"]), m.get("inst"), m.get("node"), m.get("batch"), m.get("vendor"))
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
        vals = [_sql(d.get(c)) for c in cols]
        self.conn.execute(
            f"INSERT INTO {t} (ts, inst, node, batch, vendor, {', '.join(cols)}) "
            f"VALUES ({', '.join('?' * (5 + len(cols)))})", (*base, *vals))

    def prune(self, now: Optional[float] = None, force: bool = False) -> int:
        """删掉超过保留期的事件（至多每小时做一次）。"""
        now = now or time.time()
        if not force and now - self._pruned_at < 3600:
            return 0
        self._pruned_at = now
        cutoff = now - self.retention_s
        n = 0
        with self._lock:
            for t in (*_EVENT_COLUMNS, ):
                n += self.conn.execute(f"DELETE FROM {t} WHERE ts < ?", (cutoff,)).rowcount
            n += self.conn.execute("DELETE FROM instance WHERE last_seen < ?", (cutoff,)).rowcount
            self.conn.commit()
        return n

    # ── 查询 ──
    def summary(self, window: str = "1d", now: Optional[float] = None, online_s: float = 60) -> dict[str, Any]:
        """大屏需要的全部数字。window ∈ 1d/7d/30d；online_s：心跳多久内算在线。"""
        if window not in WINDOWS:
            raise ValueError(f"window 只支持 {', '.join(WINDOWS)}")
        now = now or time.time()
        since = now - WINDOWS[window]
        with self._lock:
            q = self.conn.execute
            return {
                "window": window, "now": now, "since": since,
                "ip": self._ip(q, since),
                "browser": self._browser(q, since, now - online_s),
                "pages": self._pages(q, since),
                "domains": self._domains(q, since),
                "vendors": self._vendors(q, since),
                "waf": self._waf(q, since),
                "realtime": self._realtime(q, now, online_s),
                "timeline": self._timeline(q, since, now, window),
            }

    def _ip(self, q, since) -> dict:
        r = q("SELECT COUNT(*) n, COALESCE(SUM(ok),0) ok, AVG(CASE WHEN ok THEN total_ms END) avg_ms "
              "FROM proxy_check WHERE ts >= ?", (since,)).fetchone()
        reasons = q("SELECT reason, COUNT(*) n FROM proxy_check WHERE ts >= ? AND NOT ok "
                    "GROUP BY reason ORDER BY n DESC LIMIT 8", (since,)).fetchall()
        return {"checked": r["n"], "passed": r["ok"], "pass_rate": _rate(r["ok"], r["n"]),
                "avg_ms": _round(r["avg_ms"]),
                "fail_reasons": [{"reason": _reason_bucket(x["reason"]), "n": x["n"]} for x in reasons]}

    def _browser(self, q, since, online_since) -> dict:
        r = q("SELECT COUNT(*) n, COALESCE(SUM(ok),0) ok, "
              "COALESCE(SUM(kind = 'exception'),0) exc, AVG(CASE WHEN ok THEN seconds END) avg_s "
              "FROM browser WHERE ts >= ?", (since,)).fetchone()
        live = q("SELECT COALESCE(SUM(browsers_online),0) online, COALESCE(SUM(workers),0) workers, "
                 "COUNT(*) inst FROM instance WHERE last_seen >= ?", (online_since,)).fetchone()
        return {"online": live["online"], "target": live["workers"], "instances": live["inst"],
                "starts": r["n"], "start_ok": r["ok"], "start_rate": _rate(r["ok"], r["n"]),
                "exceptions": r["exc"], "avg_pass_s": _round(r["avg_s"], 1)}

    def _pages(self, q, since) -> dict:
        """成功率/过盾率按全部尝试算；耗时（avg/P95）只算真加载出来的页。"""
        r = q("SELECT COUNT(*) n, COALESCE(SUM(ok),0) ok, COALESCE(SUM(passed),0) passed, "
              "AVG(CASE WHEN passed THEN elapsed_ms END) avg_ms FROM page WHERE ts >= ?", (since,)).fetchone()
        return {"attempts": r["n"], "success": r["ok"], "failed": r["n"] - r["ok"],
                "success_rate": _rate(r["ok"], r["n"]), "pass_rate": _rate(r["passed"], r["n"]),
                "avg_ms": _round(r["avg_ms"]),
                "p95_ms": _percentile(q, "page", "elapsed_ms", "ts >= ? AND passed", (since,), 0.95)}

    def _domains(self, q, since, limit: int = 50) -> list[dict]:
        rows = q("SELECT host, COUNT(*) n, SUM(ok) ok, SUM(passed) passed, "
                 "AVG(CASE WHEN passed THEN elapsed_ms END) avg_ms, "
                 "GROUP_CONCAT(DISTINCT waf) wafs FROM page WHERE ts >= ? GROUP BY host "
                 "ORDER BY n DESC LIMIT ?", (since, limit)).fetchall()
        out = []
        for x in rows:
            out.append({"host": x["host"], "attempts": x["n"], "success": x["ok"],
                        "success_rate": _rate(x["ok"], x["n"]), "pass_rate": _rate(x["passed"], x["n"]),
                        "avg_ms": _round(x["avg_ms"]),
                        "p95_ms": _percentile(q, "page", "elapsed_ms", "ts >= ? AND host = ? AND passed",
                                              (since, x["host"]), 0.95),
                        "waf": _main_waf(x["wafs"])})
        return out

    def _vendors(self, q, since) -> list[dict]:
        rows = q("SELECT vendor, host, COUNT(*) n, SUM(ok) ok, SUM(passed) passed, "
                 "AVG(CASE WHEN passed THEN elapsed_ms END) avg_ms "
                 "FROM page WHERE ts >= ? GROUP BY vendor, host ORDER BY vendor, n DESC", (since,)).fetchall()
        ips = {x["vendor"]: x for x in q(
            "SELECT vendor, COUNT(*) n, SUM(ok) ok FROM proxy_check WHERE ts >= ? GROUP BY vendor",
            (since,)).fetchall()}
        vendors: dict[str, dict] = {}
        for x in rows:
            v = vendors.setdefault(x["vendor"], {"vendor": x["vendor"], "attempts": 0, "success": 0,
                                                  "domains": []})
            v["attempts"] += x["n"]
            v["success"] += x["ok"]
            v["domains"].append({"host": x["host"], "attempts": x["n"], "success_rate": _rate(x["ok"], x["n"]),
                                 "pass_rate": _rate(x["passed"], x["n"]), "avg_ms": _round(x["avg_ms"])})
        for name, ip in ips.items():
            vendors.setdefault(name, {"vendor": name, "attempts": 0, "success": 0, "domains": []})
        for v in vendors.values():
            v["success_rate"] = _rate(v["success"], v["attempts"])
            ip = ips.get(v["vendor"])
            v["ip_checked"] = ip["n"] if ip else 0
            v["ip_pass_rate"] = _rate(ip["ok"], ip["n"]) if ip else None
        return sorted(vendors.values(), key=lambda v: -v["attempts"])

    def _waf(self, q, since) -> list[dict]:
        """过盾分析：页面采集 + 浏览器热身落地页，凡识别出 WAF 的都算一次过盾尝试；passed=页面真加载出来。"""
        rows = q("SELECT waf, COUNT(*) n, SUM(passed) passed, AVG(CASE WHEN passed THEN ms END) avg_ms "
                 "FROM (SELECT waf, passed, elapsed_ms ms FROM page WHERE ts >= ? "
                 "      UNION ALL SELECT waf, ok, seconds * 1000 FROM browser WHERE ts >= ? AND kind != 'exception') "
                 "WHERE waf IN (%s) GROUP BY waf" % ",".join("?" * len(schema.WAFS)),
                 (since, since, *schema.WAFS)).fetchall()
        by = {x["waf"]: x for x in rows}
        out = []
        for w in schema.WAFS:
            x = by.get(w)
            n = x["n"] if x else 0
            p95 = _percentile(q, "page", "elapsed_ms", "ts >= ? AND waf = ? AND passed", (since, w), 0.95)
            out.append({"waf": w, "attempts": n, "passed": x["passed"] if x else 0,
                        "pass_rate": _rate(x["passed"], n) if x else None,
                        "avg_ms": _round(x["avg_ms"]) if x else None, "p95_ms": p95})
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
            "   || ' ' || round(seconds, 1) || 's' || CASE WHEN error != '' THEN ' ' || error ELSE '' END FROM browser WHERE ts >= ?"
            " UNION ALL SELECT ts, inst, 'log', lower(level), message FROM log WHERE ts >= ?"
            ") ORDER BY ts DESC LIMIT 60", (now - 3600,) * 3).fetchall()
        return {"pending": sum(i["pending"] or 0 for i in live), "running": sum(i["running"] or 0 for i in live),
                "online_instances": len(live), "instances": instances,
                "domain_freq": [{"host": x["host"], "per_min": round(x["n"] / 5, 1)} for x in freq],
                "events": [dict(x) for x in events]}

    def _timeline(self, q, since, now, window) -> list[dict]:
        bucket = {"1d": 3600, "7d": 6 * 3600, "30d": 86400}[window]
        start = since - since % bucket
        rows = q("SELECT CAST((ts - ?) / ? AS INTEGER) b, COUNT(*) n, SUM(ok) ok, SUM(passed) passed "
                 "FROM page WHERE ts >= ? GROUP BY b", (start, bucket, since)).fetchall()
        by = {x["b"]: x for x in rows}
        out = []
        for i in range(int((now - start) // bucket) + 1):
            x = by.get(i)
            out.append({"t": start + i * bucket, "attempts": x["n"] if x else 0,
                        "success": x["ok"] if x else 0, "passed": x["passed"] if x else 0})
        return out


def _sql(v):
    return int(v) if isinstance(v, bool) else v


def _rate(num, den) -> Optional[float]:
    return round((num or 0) / den, 4) if den else None


def _round(v, nd: int = 0):
    if v is None:
        return None
    return round(v, nd) if nd else int(round(v))


def _percentile(q, table: str, col: str, where: str, args: tuple, p: float) -> Optional[int]:
    n = q(f"SELECT COUNT({col}) FROM {table} WHERE {where} AND {col} IS NOT NULL", args).fetchone()[0]
    if not n:
        return None
    off = min(n - 1, max(0, int(round(p * n)) - 1))
    v = q(f"SELECT {col} FROM {table} WHERE {where} AND {col} IS NOT NULL ORDER BY {col} LIMIT 1 OFFSET ?",
          (*args, off)).fetchone()[0]
    return int(v)


def _main_waf(wafs: Optional[str]) -> str:
    found = [w for w in (wafs or "").split(",") if w in schema.WAFS]
    return found[0] if found else schema.WAF_NONE


def _reason_bucket(reason: Optional[str]) -> str:
    """slow_3456ms / exit_ip_mismatch(1.2.3.4) → slow / exit_ip_mismatch，便于归类。"""
    r = reason or "unknown"
    if r.startswith("slow_"):
        return "slow"
    if r.startswith("status_"):
        return r
    return r.split("(", 1)[0].split(":", 1)[0][:60]
