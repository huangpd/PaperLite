import hashlib
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import duckdb

CSV_PATH = "10_1016_high_priority_doi_remove_err_title.csv"
DB_DIR = Path("db")
DB_PATH = DB_DIR / "10_1016_high_priority.db"
CHUNK_SIZE = 50000

_CST = timezone(timedelta(hours=8))

def now_text():
    return datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S")

def normalize_url(url: str) -> str:
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))

def url_key(url: str) -> bytes:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).digest()

def init_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA cache_size = -64000;")
    
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS paper_pdf_resources (
        id            INTEGER PRIMARY KEY,
        url_key       BLOB    NOT NULL UNIQUE,
        doi           TEXT    NOT NULL,
        url           TEXT    NOT NULL,
        url_type      TEXT    NOT NULL DEFAULT 'html',
        final_url     TEXT,
        title         TEXT,
        flag          TEXT,
        file_path     TEXT,
        source        TEXT,
        batch_no      TEXT,
        volume        TEXT,
        status        TEXT    NOT NULL DEFAULT 'pending',
        http_status   INTEGER,
        retry_count   INTEGER NOT NULL DEFAULT 0,
        challenge_count INTEGER NOT NULL DEFAULT 0,
        inferred      INTEGER NOT NULL DEFAULT 0,
        error_message TEXT,
        error_kind    TEXT,
        file_size     INTEGER,
        elapsed_ms    INTEGER,
        exit_ip       TEXT,
        claimed_by    TEXT,
        created_at    TEXT    NOT NULL,
        updated_at    TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_claim ON paper_pdf_resources (challenge_count, id) WHERE status = 'pending';
    CREATE INDEX IF NOT EXISTS idx_status ON paper_pdf_resources (status, error_kind);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('host', 'doi.org')")
    conn.commit()
    conn.close()

def import_csv():
    t0 = time.time()
    print(f"正在准备按族群模式排序导入 TaskDB: {DB_PATH.resolve()}")
    init_db(DB_PATH)
    
    print("⚡️ 启动 DuckDB 按照 (pattern_key, doi) 进行全量向量化排序...")
    con = duckdb.connect()
    sort_sql = f"""
    WITH raw AS (
        SELECT 
            doi,
            CASE
                WHEN lower(doi) LIKE '10.1016/%' THEN
                    regexp_extract(lower(doi), '^(10\\.1016/(?:j\\.[a-z0-9_-]+|s[0-9a-z_-]+|[a-z0-9_-]+))', 1)
                WHEN lower(doi) LIKE '10.1002/%' THEN
                    coalesce(nullif(regexp_extract(lower(doi), '^(10\\.1002/[a-z0-9_-]+)\\.', 1), ''), regexp_extract(lower(doi), '^(10\\.1002/[^/]+)', 1))
                ELSE
                    regexp_extract(lower(doi), '^(10\\.[0-9]+)', 1)
            END as pattern_key
        FROM read_csv('{CSV_PATH}', header=true, all_varchar=true, ignore_errors=true)
        WHERE doi IS NOT NULL AND doi != ''
    )
    SELECT doi, pattern_key FROM raw
    ORDER BY pattern_key ASC, doi ASC;
    """
    cursor = con.execute(sort_sql)
    
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA cache_size = -64000;")
    
    total = 0
    added = 0
    ts = now_text()
    
    insert_sql = """
    INSERT OR IGNORE INTO paper_pdf_resources 
    (url_key, doi, url, url_type, source, batch_no, volume, status, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
    """
    
    while True:
        chunk = cursor.fetchmany(CHUNK_SIZE)
        if not chunk:
            break
        
        batch = []
        for row in chunk:
            doi = row[0]
            url = f"https://doi.org/{doi}"
            k = url_key(url)
            batch.append((k, doi, url, "html", "10_1016_high_priority", None, None, ts, ts))
        
        with conn:
            cur = conn.executemany(insert_sql, batch)
            added += cur.rowcount
        
        total += len(batch)
        elapsed = time.time() - t0
        rate = total / elapsed if elapsed > 0 else 0
        print(f"已按族群顺序导入 {total:,} 条 | 新增 {added:,} 条 | 速度: {rate:,.0f} 条/秒")
    
    conn.close()
    con.close()
    print(f"🎉 导入全部完成！总数据量: {total:,} 条，总耗时: {time.time() - t0:.2f} 秒")

if __name__ == "__main__":
    import_csv()
