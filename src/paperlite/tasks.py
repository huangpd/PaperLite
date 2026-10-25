#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/tasks.py
────────────────────
SQLite 任务库：一个站点一个库（<out>/tasks.db），存任务与结果，可断点续跑、按状态查询与重跑。

表结构参照 paper_downloader 的 PostgreSQL 的 paper_pdf_resources（同名同义的列取值一致，便于以后迁移）：
  - url_key = sha256(规范化 pdf_url)，32 字节 BLOB，唯一；重复导入不会重置已有任务
  - paper_uid 从链接提取 DOI（转小写），没有时用 url-<url_key 前 16 位>；决定文件名
  - status：pending → running → success | non_pdf | failed
  - challenge_count：带有效 cookie 仍被站点挑战的次数（站点单独盯上了这条 URL）；领取时排在后面，
    满 max_url_challenges 判 failed / challenge_failed
  - volume：DOI 后缀点号前的卷期号（10.1145/1012326.1012328 → 1012326），新式纯数字 DOI 为空；
    同一卷期还没下到过 PDF 时最多同时放 volume_stop_after 篇出去，这几篇都是 404 / 订阅 / 摘要页
    就停掉该卷期其余任务：记 non_pdf、inferred = 1，不再请求（见 volume_stops 表）
  - error_kind 用 paper_downloader 站点监控的类别：not_found / paywall / landing_page / blocked / network / server_error
  - created_at / updated_at 为北京时间文本 'YYYY-MM-DD HH:MM:SS'
paper_downloader 的 host 生成列、fetched_via 不需要：一个库只有一个主机、只走 cookie 路线；多了 exit_ip。

同一个库同时只允许一个下载进程（flock），启动时把上次残留的 running 放回 pending。
"""

import fcntl
import hashlib
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote, urlsplit, urlunsplit


class Status:
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    NON_PDF = "non_pdf"

    ALL = (PENDING, RUNNING, SUCCESS, FAILED, NON_PDF)
    FINAL = (SUCCESS, FAILED, NON_PDF)


# 与 paper_downloader monitor/classify.py 同名类别
ERROR_KINDS = ("not_found", "paywall", "landing_page", "blocked", "network", "server_error", "challenge_failed")
# 计入卷期停止的"没有 PDF"类别（实测 ACM 的订阅页和摘要页都没有 PDF 入口，只差一条横幅）
NO_PDF_KINDS = ("not_found", "paywall", "landing_page")

_CST = timezone(timedelta(hours=8))
_DOI_IN_PATH_RE = re.compile(r"(10\.\d{4,9}/[^\s?#]+)", re.IGNORECASE)
_VOLUME_RE = re.compile(r"^10\.\d{4,9}/(\d+)\.\d+$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STEM_LEN = 180


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS paper_pdf_resources (
    id            INTEGER PRIMARY KEY,
    url_key       BLOB    NOT NULL UNIQUE,
    paper_uid     TEXT    NOT NULL,
    pdf_url       TEXT    NOT NULL,
    file_path     TEXT,
    source        TEXT,
    batch_no      TEXT,
    volume        TEXT,
    status        TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN {_in(Status.ALL)}),
    http_status   INTEGER,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    challenge_count INTEGER NOT NULL DEFAULT 0,
    inferred      INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    error_kind    TEXT    CHECK (error_kind IN {_in(ERROR_KINDS)}),
    file_size     INTEGER,
    elapsed_ms    INTEGER,
    exit_ip       TEXT,
    claimed_by    TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim ON paper_pdf_resources (challenge_count, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_status ON paper_pdf_resources (status, error_kind);
CREATE INDEX IF NOT EXISTS idx_volume ON paper_pdf_resources (volume, status) WHERE volume IS NOT NULL;
CREATE TABLE IF NOT EXISTS volume_stops (
    volume      TEXT PRIMARY KEY,
    error_kind  TEXT NOT NULL,
    samples     TEXT NOT NULL,        -- 判停依据的几篇 paper_uid
    skipped     INTEGER NOT NULL,     -- 停掉时未请求的任务数
    stopped_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


# ── 任务身份（与 paper_downloader downloader/identity.py、paths.py 规则一致）──────────────

def normalize_url(url: str) -> str:
    """协议和域名转小写、去掉 #片段；路径与参数区分大小写，原样保留。"""
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))


def url_key(url: str) -> bytes:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).digest()


def url_host(url: str) -> str:
    return (urlsplit(url.strip()).hostname or "").lower()


def derive_paper_uid(url: str) -> str:
    m = _DOI_IN_PATH_RE.search(unquote(urlsplit(url.strip()).path))
    if m:
        return re.sub(r"(?:\.pdf|/)$", "", m.group(1), flags=re.IGNORECASE).lower()
    return f"url-{url_key(url).hex()[:16]}"


def derive_volume(paper_uid: str) -> Optional[str]:
    """DOI 后缀点号前的卷期号：10.1145/1012326.1012328 → 1012326；新式纯数字 DOI 返回 None。"""
    m = _VOLUME_RE.match(paper_uid)
    return m.group(1) if m else None


def pdf_path(out_dir: Path, paper_uid: str) -> Path:
    """<out>/<h[0:2]>/<h[2:4]>/<名字>.pdf，h = sha256(paper_uid)；百万级文件也不会挤在一个目录里。"""
    digest = hashlib.sha256(paper_uid.encode("utf-8")).hexdigest()
    readable = paper_uid.strip().replace("/", "_")
    stem = _SAFE_NAME_RE.sub("_", readable).strip("._")
    if stem != readable or len(stem) > _MAX_STEM_LEN or not stem:
        stem = f"{stem[:_MAX_STEM_LEN]}_{digest[:8]}" if stem else f"paper_{digest[:8]}"
    return Path(out_dir) / digest[:2] / digest[2:4] / f"{stem}.pdf"


def now_text() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class Task:
    id: int
    paper_uid: str
    pdf_url: str
    retry_count: int
    challenge_count: int = 0


class TaskDB:
    """线程安全：所有语句走同一个连接并加锁（写入量每秒几条，不需要连接池）。"""

    def __init__(self, path: Path, volume_stop_after: int = 0) -> None:
        self.path = Path(path)
        self.volume_stop_after = volume_stop_after     # 0 = 不按卷期停止
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._lock_file = None
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            old = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'paper_pdf_resources'").fetchone()
            if old and not all(col in old[0] for col in ("challenge_count", "volume", "inferred")):
                raise RuntimeError(f"{self.path} 是旧版结构（缺新列），删除后重新 import")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
        if self._lock_file is not None:
            self._lock_file.close()

    # ── 进程锁 ────────────────────────────────────────────────

    def lock_for_download(self) -> int:
        """独占本库的下载权；拿不到抛 RuntimeError。返回放回 pending 的残留 running 数。"""
        f = open(self.path.with_name(self.path.name + ".lock"), "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise RuntimeError(f"已有下载进程在使用 {self.path}")
        self._lock_file = f
        with self._lock:
            cur = self._conn.execute(
                "UPDATE paper_pdf_resources SET status = 'pending', claimed_by = NULL, updated_at = ? "
                "WHERE status = 'running'", (now_text(),))
            return cur.rowcount

    # ── 主机 ──────────────────────────────────────────────────

    def host(self) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'host'").fetchone()
        return row[0] if row else None

    # ── 导入 ──────────────────────────────────────────────────

    def import_urls(self, rows: Iterable[dict]) -> tuple[int, int]:
        """
        rows: {"pdf_url", 可选 "paper_uid" / "source" / "batch_no"}。返回 (新增, 已存在)。
        一个库只能有一个主机：第一次导入时记下，之后其他主机的链接直接报错。
        """
        host = self.host()
        batch, seen = [], 0
        ts = now_text()
        for r in rows:
            url = r["pdf_url"].strip()
            h = url_host(url)
            if not h:
                raise ValueError(f"不是有效链接: {url}")
            host = host or h
            if h != host:
                raise ValueError(f"本库主机是 {host}，不能导入 {h} 的链接（一个站点一个库）: {url}")
            uid = (r.get("paper_uid") or "").strip().lower() or derive_paper_uid(url)
            batch.append((url_key(url), uid, url, r.get("source") or None, r.get("batch_no") or None,
                          derive_volume(uid), ts, ts))
            seen += 1
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('host', ?)", (host,))
                before = self._conn.total_changes
                self._conn.executemany(
                    "INSERT INTO paper_pdf_resources (url_key, paper_uid, pdf_url, source, batch_no, volume, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (url_key) DO NOTHING", batch)
                added = self._conn.total_changes - before
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return added, seen - added

    # ── 领取与结果 ────────────────────────────────────────────

    _CLAIM_BATCH = 64            # 每次按领取顺序取这么多候选，逐个判卷期能否放行
    _CLAIM_SCAN_MAX = 4096       # 最多看这么多候选；都被卷期限流挡住就返回 None，稍后再领

    def claim(self, worker: str) -> Optional[Task]:
        """
        按 (challenge_count, id) 顺序领一条。卷期还没下到过 PDF 时，在途 + 已判"没有 PDF"的不超过
        volume_stop_after 篇才放行，其余线程去领别的卷期。
        候选在 Python 里逐个判：写成一条 SQL 时 SQLite 会把 70 万条 pending 全排一遍再逐条跑子查询，每次领取要几秒。
        """
        n = self.volume_stop_after
        with self._lock:
            c = self._conn
            allowed: dict[str, bool] = {}
            after_cc, after_id = -1, -1
            scanned = 0
            while scanned < self._CLAIM_SCAN_MAX:
                rows = c.execute(
                    "SELECT id, volume, challenge_count FROM paper_pdf_resources INDEXED BY idx_claim "
                    "WHERE status = 'pending' AND (challenge_count, id) > (?, ?) ORDER BY challenge_count, id LIMIT ?",
                    (after_cc, after_id, self._CLAIM_BATCH)).fetchall()
                if not rows:
                    return None
                for task_id, volume, cc in rows:
                    if n and volume is not None:
                        if volume not in allowed:
                            allowed[volume] = self._volume_open(volume, n)
                        if not allowed[volume]:
                            continue
                    row = c.execute(
                        "UPDATE paper_pdf_resources SET status = 'running', claimed_by = ?, updated_at = ? "
                        "WHERE id = ? AND status = 'pending' "
                        "RETURNING id, paper_uid, pdf_url, retry_count, challenge_count",
                        (worker, now_text(), task_id)).fetchone()
                    if row:
                        return Task(row["id"], row["paper_uid"], row["pdf_url"], row["retry_count"],
                                    row["challenge_count"])
                after_id, _, after_cc = rows[-1]
                scanned += len(rows)
            return None

    def _volume_open(self, volume: str, n: int) -> bool:
        """调用方已持锁。卷期已下到过 PDF，或在途 + 已判没有 PDF 的少于 n 篇。"""
        c = self._conn
        if c.execute("SELECT EXISTS (SELECT 1 FROM paper_pdf_resources WHERE volume = ? AND status = 'success')",
                     (volume,)).fetchone()[0]:
            return True
        busy = c.execute(
            "SELECT count(*) FROM paper_pdf_resources WHERE volume = ? AND (status = 'running' OR (status = 'non_pdf' "
            f"AND inferred = 0 AND error_kind IN {_in(NO_PDF_KINDS)}))", (volume,)).fetchone()[0]
        return busy < n

    def finish(self, task: Task, status: str, *, http_status: Optional[int] = None, file_path: str = None,
               file_size: Optional[int] = None, elapsed_ms: Optional[int] = None, exit_ip: str = None,
               error_kind: Optional[str] = None, error_message: Optional[str] = None) -> Optional[tuple[str, int]]:
        """
        写最终结果（success / non_pdf / failed）。成功时清空错误列。
        判 non_pdf 后若该卷期就此满足停止条件，停掉其余任务并返回 (卷期号, 停掉的任务数)，否则 None。
        """
        assert status in Status.FINAL, status
        with self._lock:
            self._conn.execute(
                "UPDATE paper_pdf_resources SET status = ?, http_status = ?, file_path = ?, file_size = ?, "
                "elapsed_ms = ?, exit_ip = ?, error_kind = ?, error_message = ?, updated_at = ? WHERE id = ?",
                (status, http_status, file_path, file_size, elapsed_ms, exit_ip, error_kind, error_message,
                 now_text(), task.id))
            if status == Status.NON_PDF and error_kind in NO_PDF_KINDS and self.volume_stop_after:
                return self._maybe_stop_volume(task.id)
        return None

    def _maybe_stop_volume(self, task_id: int) -> Optional[tuple[str, int]]:
        """调用方已持锁。卷期还没下到过 PDF、已有 N 篇"没有 PDF" → 其余 pending 记 non_pdf / inferred。"""
        c = self._conn
        volume = c.execute("SELECT volume FROM paper_pdf_resources WHERE id = ?", (task_id,)).fetchone()[0]
        if volume is None or c.execute("SELECT EXISTS (SELECT 1 FROM paper_pdf_resources WHERE volume = ? "
                                       "AND status = 'success')", (volume,)).fetchone()[0]:
            return None
        samples = c.execute(
            "SELECT paper_uid, error_kind FROM paper_pdf_resources WHERE volume = ? AND status = 'non_pdf' "
            f"AND inferred = 0 AND error_kind IN {_in(NO_PDF_KINDS)} ORDER BY id", (volume,)).fetchall()
        if len(samples) < self.volume_stop_after:
            return None
        kinds = [k for _, k in samples]
        kind = max(set(kinds), key=kinds.count)
        ts = now_text()
        c.execute("BEGIN IMMEDIATE")
        try:
            skipped = c.execute(
                "UPDATE paper_pdf_resources SET status = 'non_pdf', inferred = 1, error_kind = ?, error_message = ?, "
                "updated_at = ? WHERE volume = ? AND status = 'pending'",
                (kind, f"按卷期推断：卷期 {volume} 已有 {len(samples)} 篇没有 PDF（{', '.join(sorted(set(kinds)))}），未请求",
                 ts, volume)).rowcount
            c.execute("INSERT OR REPLACE INTO volume_stops (volume, error_kind, samples, skipped, stopped_at) "
                      "VALUES (?, ?, ?, ?, ?)", (volume, kind, " ".join(u for u, _ in samples), skipped, ts))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return volume, skipped

    def retry_later(self, task: Task, *, count: bool, error_kind: Optional[str] = None,
                    error_message: Optional[str] = None, exit_ip: Optional[str] = None) -> None:
        """放回 pending；count=False 用于不是任务本身的问题（cookie 被拒、身份失效），不计重试。"""
        with self._lock:
            self._conn.execute(
                "UPDATE paper_pdf_resources SET status = 'pending', claimed_by = NULL, "
                "retry_count = retry_count + ?, error_kind = COALESCE(?, error_kind), "
                "error_message = COALESCE(?, error_message), exit_ip = COALESCE(?, exit_ip), updated_at = ? "
                "WHERE id = ?", (1 if count else 0, error_kind, error_message, exit_ip, now_text(), task.id))

    def challenged(self, task: Task, max_challenges: int, exit_ip: str, message: str) -> bool:
        """带有效 cookie 仍被挑战：计一次，排到后面；满 max_challenges 判 failed。返回是否已判失败。"""
        failed = task.challenge_count + 1 >= max_challenges
        with self._lock:
            self._conn.execute(
                "UPDATE paper_pdf_resources SET status = ?, challenge_count = challenge_count + 1, claimed_by = NULL, "
                "error_kind = 'challenge_failed', error_message = ?, http_status = 403, exit_ip = ?, updated_at = ? "
                "WHERE id = ?", (Status.FAILED if failed else Status.PENDING, message, exit_ip, now_text(), task.id))
        return failed

    # ── 查询与运维 ────────────────────────────────────────────

    def has_status(self, status: str) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT EXISTS (SELECT 1 FROM paper_pdf_resources WHERE status = ?)", (status,)).fetchone()[0] == 1

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, count(*) FROM paper_pdf_resources GROUP BY status").fetchall()
        return {s: 0 for s in Status.ALL} | {r[0]: r[1] for r in rows}

    def kind_counts(self) -> list[tuple[str, str, int, int]]:
        """[(status, error_kind, 条数, 其中按卷期推断的条数)]"""
        with self._lock:
            return [tuple(r) for r in self._conn.execute(
                "SELECT status, error_kind, count(*), sum(inferred) FROM paper_pdf_resources "
                "WHERE status IN ('failed', 'non_pdf') GROUP BY 1, 2 ORDER BY 1, 3 DESC")]

    def reset(self, status: str, error_kind: Optional[str] = None) -> int:
        """把指定状态（可再按 error_kind 筛）的任务重置为 pending、清零重试，用于重跑。"""
        if status not in Status.FINAL:
            raise ValueError(f"只能重置 {', '.join(Status.FINAL)}")
        sql = ("UPDATE paper_pdf_resources SET status = 'pending', retry_count = 0, challenge_count = 0, "
               "inferred = 0, claimed_by = NULL, updated_at = ? WHERE status = ?")
        args: list = [now_text(), status]
        if error_kind:
            sql += " AND error_kind = ?"
            args.append(error_kind)
        with self._lock:
            n = self._conn.execute(sql, args).rowcount
            self._conn.execute("DELETE FROM volume_stops WHERE volume NOT IN (SELECT volume FROM paper_pdf_resources "
                               "WHERE inferred = 1 AND volume IS NOT NULL)")
            return n

    def reset_stopped_volumes(self) -> tuple[int, int]:
        """
        重新核实被停掉的卷期：这些卷期里所有 non_pdf（判停的样本 + 推断的）都放回 pending。
        只重置推断的那部分没用：样本仍在，会立刻满足停止条件、推断的任务永远领不到。返回 (卷期数, 任务数)。
        """
        with self._lock:
            vols = [r[0] for r in self._conn.execute("SELECT volume FROM volume_stops")]
            n = 0
            for v in vols:
                n += self._conn.execute(
                    "UPDATE paper_pdf_resources SET status = 'pending', inferred = 0, retry_count = 0, "
                    "claimed_by = NULL, updated_at = ? WHERE volume = ? AND status = 'non_pdf'", (now_text(), v)).rowcount
            self._conn.execute("DELETE FROM volume_stops")
            return len(vols), n

    def volume_summary(self) -> tuple[int, int]:
        """(被停掉的卷期数, 按卷期推断、未请求的任务数)"""
        with self._lock:
            v = self._conn.execute("SELECT count(*) FROM volume_stops").fetchone()[0]
            n = self._conn.execute("SELECT count(*) FROM paper_pdf_resources WHERE inferred = 1").fetchone()[0]
        return v, n

    def rows(self, status: Optional[str] = None):
        sql = ("SELECT id, paper_uid, volume, pdf_url, status, error_kind, inferred, http_status, file_size, elapsed_ms, "
               "exit_ip, retry_count, challenge_count, file_path, error_message, updated_at FROM paper_pdf_resources")
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        with self._lock:
            return self._conn.execute(sql + " ORDER BY id", args).fetchall()
