#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/rules/page.py
───────────────────────
页面判定，两种模式共用（只看响应头和正文，不做网络请求）：

  is_challenge / is_cf_denied   响应是不是 Cloudflare 挑战页 / 封禁页（curl 模式判身份）
  classify_waf                  站点前面是哪家 WAF（仅供监控统计）
  visible_text_len / site_key   页面可见文字长度、主机的站点键（浏览器模式等内容加载完时用）
  page_problem                  浏览器拿到的网页能不能记成功：站点人机验证页（blocked）、没渲染出正文的空壳（incomplete）
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


_HIDDEN_RE = re.compile(r"(?is)<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_TWO_LEVEL = {"ac", "co", "com", "edu", "gov", "go", "ne", "net", "or", "org"}

# 站点自己的人机验证 / 限流页（不是 Cloudflare 挑战：响应头不带 cf-mitigated，状态码常是 200）。
# 只在正文很短的页面上认、只用较具体的说法（不单认 captcha），免得正常页面被误判
_BOT_TITLES = ("validate user", "just a moment", "attention required", "access denied", "are you a robot",
               "robot check", "security check", "bot verification", "verify you are human", "request rejected")
_BOT_TEXTS = ("unusual traffic", "not a robot", "verify you are human", "verify you are a human",
              "complete the security check", "automated requests")
_BOT_MAX_TEXT = 3000


def visible_text(html: str) -> str:
    """去掉脚本、样式和标签后的可见文字（含 <title>），空白压成单个空格。"""
    return " ".join(_TAG_RE.sub(" ", _HIDDEN_RE.sub(" ", html or "")).split())


def visible_text_len(html: str) -> int:
    return len(visible_text(html))


def site_key(host: str) -> str:
    """主机所属站点（粗略的可注册域名）：ieeexplore.ieee.org → ieee.org，www.jstage.jst.go.jp → jst.go.jp。
    用来区分"站点自己的数据请求"和第三方统计/广告请求；不求精确，判错只影响多等或少等几秒。"""
    labels = (host or "").lower().rstrip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _TWO_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def page_problem(html: str, min_text: int) -> str:
    """
    浏览器打开、HTTP 状态正常（< 400）的网页能不能记成功：
      blocked     站点人机验证 / 限流页（OUP "Validate User"…unusual traffic），换 IP 重来
      incomplete  可见文字少于 min_text：正文没渲染出来（JS 页面还在加载、文档只解析了 <head>）
      ""          正常
    min_text <= 0 时不判 incomplete。
    """
    text = visible_text(html)
    if len(text) <= _BOT_MAX_TEXT:
        title, low = page_title(html).lower(), text.lower()
        if any(t in title for t in _BOT_TITLES) or any(t in low for t in _BOT_TEXTS):
            return "blocked"
    if min_text > 0 and len(text) < min_text:
        return "incomplete"
    return ""


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
