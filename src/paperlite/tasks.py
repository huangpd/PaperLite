#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/tasks.py
────────────────────
任务的公共定义：状态、链接类型、错误分类、Task，以及任务身份规则（URL 规范化、url_key、DOI 推导、文件路径）。
存储在 pgqueue.py（PostgreSQL）；本模块不碰数据库。

与 paper_downloader 的 PostgreSQL paper_pdf_resources 同名同义：
  - url_key = sha256(规范化 url)，唯一；重复导入不会重置已有任务
  - doi 来自导入列（没给则从链接回退推导，转小写）；决定文件名
  - url_type：pdf = 链接应返回 PDF（默认）；html = 要的就是网页本身，整页 HTML 存为 .html
  - status：pending → success | non_pdf | failed（html 任务没有 non_pdf：拿到网页即 success）；
    running 只是进程内的在途计数，不落库
  - error_kind 用 paper_downloader 站点监控的类别（extract_failed = 拿到了 PDF 但过小 / 超过大小上限）
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit


class Status:
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    NON_PDF = "non_pdf"

    ALL = (PENDING, RUNNING, SUCCESS, FAILED, NON_PDF)
    FINAL = (SUCCESS, FAILED, NON_PDF)


class UrlType:
    PDF = "pdf"          # 链接应返回 PDF
    HTML = "html"        # 要的是网页本身

    ALL = (PDF, HTML)


# 与 paper_downloader monitor/classify.py 同名类别
# rate_limited: HTTP 429 未在 Retry-After 等待后恢复（站点对该 URL 临时限流，非 Cloudflare 封禁）
ERROR_KINDS = ("not_found", "paywall", "landing_page", "blocked", "network", "server_error", "challenge_failed",
               "extract_failed", "rate_limited")

_DOI_IN_PATH_RE = re.compile(r"(10\.\d{4,9}/[^\s?#]+)", re.IGNORECASE)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STEM_LEN = 180


@dataclass(frozen=True)
class Task:
    id: int
    doi: str
    url: str
    retry_count: int
    challenge_count: int = 0
    url_type: str = UrlType.PDF


# ── 任务身份（与 paper_downloader downloader/identity.py、paths.py 规则一致）──────────────

def normalize_url(url: str) -> str:
    """协议和域名转小写、去掉 #片段；路径与参数区分大小写，原样保留。"""
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))


def url_key(url: str) -> bytes:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).digest()


def url_host(url: str) -> str:
    return (urlsplit(url.strip()).hostname or "").lower()


def derive_doi(url: str) -> str:
    """导入未提供 doi 时的回退：从 URL 路径里认 DOI/PII，认不出就用 url- 哈希。纯 URL 列表也能有稳定标识。"""
    m = _DOI_IN_PATH_RE.search(unquote(urlsplit(url.strip()).path))
    if m:
        return re.sub(r"(?:\.pdf|/)$", "", m.group(1), flags=re.IGNORECASE).lower()
    return f"url-{url_key(url).hex()[:16]}"


def pdf_path(base_dir: Path, doi: str, suffix: str = ".pdf") -> Path:
    """<base>/<h[0:2]>/<h[2:4]>/<doi>.pdf（网页任务为 .html），h = sha256(doi)；返回绝对路径。
    base 是输出目录（[paths].storage），不含主机层——doi 全局唯一，不同站点的文件也不会撞。"""
    digest = hashlib.sha256(doi.encode("utf-8")).hexdigest()
    readable = doi.strip().replace("/", "_")
    stem = _SAFE_NAME_RE.sub("_", readable).strip("._")
    if stem != readable or len(stem) > _MAX_STEM_LEN or not stem:
        stem = f"{stem[:_MAX_STEM_LEN]}_{digest[:8]}" if stem else f"paper_{digest[:8]}"
    return (Path(base_dir) / digest[:2] / digest[2:4] / f"{stem}{suffix}").resolve()
