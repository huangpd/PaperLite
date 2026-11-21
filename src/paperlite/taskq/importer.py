"""
paperlite/taskq/importer.py
───────────────────────────
导入任务：读 txt（一行一个链接）或 CSV（DuckDB 按 DOI 排序，同一出版商 / 期刊的任务连号），
写进任务队列；明显下载不到的链接剔除并写报告。命令行的 import 和 run --urls 都调这里。
"""

import csv
import logging
from collections import Counter
from pathlib import Path
from typing import Iterator

from paperlite.errors import UserError
from paperlite.taskq.source import PgTaskSource
from paperlite.core.tasks import UrlType

logger = logging.getLogger("paperlite")


def read_url_file(path: str) -> Iterator[dict]:
    """txt：一行一个链接；CSV：表头含 url（或旧名 pdf_url），可选 doi / source / batch_no / url_type。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        first = f.readline()
        f.seek(0)
        if "url" in first or "pdf_url" in first:
            yield from read_csv_clustered(path)
        else:
            yield from ({"url": line.strip()} for line in f if line.strip().startswith("http"))


def read_csv_clustered(path: str) -> Iterator[dict]:
    """
    用 DuckDB 读 CSV，按 (DOI, url) 排序后逐行产出，字段与 csv.DictReader 相同（空单元格为 None）。
    不写任何出版商规则：DOI 是"前缀/后缀"结构，按字符串排序后同一前缀（出版商）、同一后缀开头（期刊代码）
    的任务自然连在一起（"." "/" 排在字母数字之前，j.ajog.* 与 j.ajogmf.* 不会交错）。任务按 id 领取，
    同一期刊的任务连号，浏览器少在站点间来回切。
    没有 doi 列时从 URL 里认 DOI；认不出 DOI 的行排在最后。url（或 pdf_url）为空的行跳过。
    """
    import duckdb
    safe = str(Path(path).resolve()).replace("'", "''")
    src = f"read_csv('{safe}', header=true, all_varchar=true, delim=',', quote='\"', escape='\"')"
    con = duckdb.connect()
    try:
        try:
            cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
        except duckdb.Error as e:
            raise UserError(f"读取 CSV 失败 {path}: {e}") from e
        quoted = {c: '"' + c.replace('"', '""') + '"' for c in cols}
        url_cols = [quoted[c] for c in ("url", "pdf_url") if c in quoted]
        if not url_cols:
            return
        url = "coalesce(" + ", ".join(f"nullif(trim({c}), '')" for c in url_cols) + ")"
        doi = ", ".join(f"nullif(trim({quoted[c]}), '')" for c in ("doi", "paper_uid") if c in quoted)
        doi_key = (f"lower(coalesce({doi + ', ' if doi else ''}"
             f"nullif(regexp_extract({url}, '(10\\.[0-9]{{4,9}}/[^\\s?#]+)', 1), '')))")
        sql = (f"SELECT {', '.join(quoted[c] for c in cols)} FROM (SELECT *, {doi_key} AS __pl_doi FROM {src}) "
               f"WHERE {url} IS NOT NULL ORDER BY __pl_doi NULLS LAST, {url}")
        try:
            cur = con.execute(sql)
        except duckdb.Error as e:
            raise UserError(f"读取 CSV 失败 {path}: {e}") from e
        while True:
            chunk = cur.fetchmany(50000)
            if not chunk:
                return
            for row in chunk:
                yield dict(zip(cols, row))
    finally:
        con.close()


def import_file(db: PgTaskSource, path: str, source: str = "", batch: str = "",
                url_type: str = UrlType.PDF) -> tuple[int, int]:
    """
    把 txt / CSV 导进任务队列，返回 (新增, 已存在)。url_type 是整批的默认类型；CSV 里的 url_type 列逐行覆盖。
    明显坏的链接不入库，明细写到输入文件旁边的 <文件名>.bad_url.csv。文件读不了、字段不合法抛 UserError。
    """
    rows = ({**r, "source": r.get("source") or source, "batch_no": r.get("batch_no") or batch,
             "url_type": (r.get("url_type") or "").strip() or url_type}
            for r in read_url_file(path))
    try:
        added, existed = db.import_urls(rows)
    except ValueError as e:
        raise UserError(str(e)) from e
    logger.info("导入 %s：新增 %d，已存在 %d（已存在的不重置）", path, added, existed)
    report_rejected(path, db.q.last_rejected)
    return added, existed


def report_rejected(path: str, rejected: list) -> None:
    """明显坏的链接没入库：按原因打一行日志，明细写到输入文件旁边的 <文件名>.bad_url.csv。"""
    if not rejected:
        return
    by = Counter(reason for _, reason in rejected)
    out = Path(path).with_name(Path(path).name + ".bad_url.csv")
    try:
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["doi", "url", "reason"])
            for r, reason in rejected:
                w.writerow([r.get("doi") or r.get("paper_uid") or "", r.get("url") or r.get("pdf_url") or "", reason])
        where = f"，明细见 {out}"
    except OSError as e:
        where = f"（明细写不了 {out}: {e}）"
    logger.warning("剔除明显下载不到的链接 %d 条，未入库：%s%s", len(rejected),
                   "，".join(f"{k} {n}" for k, n in by.most_common()), where)
