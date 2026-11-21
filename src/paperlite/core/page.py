#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/core/page.py
──────────────────────
页面判定，两种模式共用（只看响应头和正文，不做网络请求）：

  is_challenge / is_cf_denied   响应是不是 Cloudflare 挑战页 / 封禁页（curl 模式判身份）
  classify_waf                  站点前面是哪家 WAF（仅供监控统计）
  classify_html                 PDF 链接返回 HTML 时判断原因，写进 error_kind（取值与 paper_downloader 站点监控一致）：
                                  not_found     站点 404 页（DOI 不存在）
                                  blocked       站点封了该 IP（ACM："IP blocked"）
                                  paywall       页面提示需要订阅（ACM："Features requiring a subscription"）
                                  landing_page  普通摘要页，没有全文入口
"""

import re

_TITLE_RE = re.compile(r"<title>([^<]*)", re.IGNORECASE)

# ACM 所有页面导航栏都带 "Basic Edition"（包括 404 页），不能据此判订阅；只认正文横幅。
# 实测 paywall 与 landing_page 两类 ACM 页面上都没有 PDF / 全文入口，横幅只说明页面有灰掉的订阅功能。
_PAYWALL_MARKERS = ("Features requiring a subscription",)
_BLOCKED_MARKERS = ("IP blocked",)


def page_title(html: str) -> str:
    m = _TITLE_RE.search(html)
    return " ".join(m.group(1).split()) if m else ""


def classify_html(html: str) -> str:
    title = page_title(html)
    if "Error: 404" in title or title.startswith("404"):
        return "not_found"
    if any(m in title for m in _BLOCKED_MARKERS):
        return "blocked"
    if any(m in html for m in _PAYWALL_MARKERS):
        return "paywall"
    return "landing_page"


def is_challenge(headers, body: bytes) -> bool:
    # 挑战页引用 /cdn-cgi/challenge-platform/h/<x>/orchestrate/…；正常页面里的 …/scripts/jsd、…/scripts/precursor 探针不算
    return bool(headers.get("cf-mitigated")) or b"/challenge-platform/h/" in body[:20000]


def is_cf_denied(code: int, headers, body: bytes) -> bool:
    if code not in (403, 429):
        return False
    if "cloudflare" not in headers.get("server", "").lower() and not headers.get("cf-ray"):
        return False
    return bool(re.search(
        rb"<title[^>]*>\s*(?:access denied|attention required)", body[:20000], re.IGNORECASE))


def classify_waf(headers: dict) -> str:
    """按主文档响应头识别站点前面的 WAF：cloudflare / aws_waf / datadome / none（仅供监控统计，不参与过盾判断）。"""
    h = {str(k).lower(): str(v or "").lower() for k, v in (headers or {}).items()}
    cookie = h.get("set-cookie", "")
    if "cf-mitigated" in h or "cf-ray" in h or "cloudflare" in h.get("server", ""):
        return "cloudflare"
    if "x-amzn-waf-action" in h or "aws-waf-token" in cookie or "awselb" in h.get("server", ""):
        return "aws_waf"
    if "x-datadome" in h or "x-datadome-cid" in h or "datadome=" in cookie or "datadome" in h.get("server", ""):
        return "datadome"
    return "none"
