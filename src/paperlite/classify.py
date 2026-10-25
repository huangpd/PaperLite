#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/classify.py
───────────────────────
PDF 链接返回 HTML 时判断原因，写进 error_kind（取值与 paper_downloader 站点监控一致）：

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
