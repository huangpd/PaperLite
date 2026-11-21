"""
paperlite/curl/http.py
──────────────────────
curl 模式的 HTTP 会话：按身份建 curl_cffi 会话（TLS 指纹跟浏览器版本走）、解析 Retry-After。
生产者（验收身份）和下载线程共用。
"""

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import certifi
from curl_cffi import requests as curl_requests

from paperlite.curl.identity import Identity

_MAX_RATE_LIMIT_WAIT_S = 60.0


def impersonate_for(user_agent: str) -> str:
    """按浏览器 Chrome 主版本选 curl_cffi 指纹（不高于浏览器版本的最新一个）；cf_clearance 与 UA/TLS 指纹绑定。"""
    from curl_cffi.requests import BrowserType

    m = re.search(r"Chrome/(\d+)", user_agent)
    if m:
        major = int(m.group(1))
        candidates = sorted(
            (int(v.group(1)), b.value) for b in BrowserType
            if (v := re.fullmatch(r"chrome(\d+)a?", b.value)) and int(v.group(1)) <= major
        )
        if candidates:
            return candidates[-1][1]
    return "chrome"


def session_for_identity(ident: Identity):
    session = curl_requests.Session(trust_env=False, verify=certifi.where(),
                                    impersonate=impersonate_for(ident.user_agent), cookies=ident.cookies)
    session.headers["user-agent"] = ident.user_agent
    return session


def rate_limit_wait(headers, identity_left: float, min_left: float) -> Optional[float]:
    """返回可安全等待的 Retry-After 秒数；缺失或超出身份寿命时不等待。"""
    value = headers.get("retry-after", "").strip()
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not 0 <= delay <= _MAX_RATE_LIMIT_WAIT_S or identity_left - delay < min_left:
        return None
    return delay
