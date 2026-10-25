#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/proxy.py
────────────────────
短效代理 IP：调提取接口拿一个新 IP，经代理访问 check_url 体检后交给生产者。
每个身份一个 IP、用完即弃，所以这里没有槽位、续租和缓存。

提取接口返回 JSON：{"code": "SUCCESS", "data": [{"server": "ip:port", "proxy_ip": "...",
"area": "...", "deadline": "YYYY-MM-DD HH:MM:SS"（北京时间）}]}（青果格式）。
"""

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import certifi
from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))
_API_RETRY_S = 5.0          # 接口失败后的最短重试间隔


class ProxyUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ProxySettings:
    api_url: str
    scheme: str = "http"
    username: str = ""
    password: str = ""
    lease_s: float = 600              # 单个 IP 最长使用时间，与接口 deadline 取较早者
    check_url: str = "https://dl.acm.org/robots.txt"
    check_timeout_s: float = 10
    check_max_tries: int = 3

    def url_for(self, server: str) -> str:
        """ip:port → 带鉴权的代理 URL（鉴权只在本地拼，不进 Redis）。"""
        if not self.username:
            return f"{self.scheme}://{server}"
        return f"{self.scheme}://{quote(self.username, safe='')}:{quote(self.password, safe='')}@{server}"


@dataclass(frozen=True)
class Lease:
    server: str           # ip:port
    exit_ip: str
    area: str
    expires_at: float     # time.time()

    def browser_proxy(self, s: ProxySettings) -> dict:
        proxy = {"server": f"{s.scheme}://{self.server}"}
        if s.username:
            proxy.update(username=s.username, password=s.password)
        return proxy


class IpSource:
    def __init__(self, settings: ProxySettings) -> None:
        if not settings.api_url:
            raise ValueError("需要代理提取接口 api_url：每个身份要一个独立出口 IP")
        self.s = settings
        self._lock = threading.Lock()        # 接口串行调用，避免并发触发限频
        self._last_error_at = 0.0

    def extract_checked(self) -> Lease:
        """提取一个体检通过的新 IP；接口失败或连续体检不过抛 ProxyUnavailable。"""
        for _ in range(max(1, self.s.check_max_tries)):
            lease = self._extract()
            if self._check(lease):
                return lease
            logger.info("新 IP %s 体检不通过，丢弃", lease.server)
        raise ProxyUnavailable(f"连续 {self.s.check_max_tries} 个 IP 体检不通过")

    def _extract(self) -> Lease:
        with self._lock:
            if time.time() - self._last_error_at < _API_RETRY_S:
                raise ProxyUnavailable("提取接口刚失败，稍后重试")
            try:
                with curl_requests.Session(trust_env=False, verify=certifi.where()) as c:
                    resp = c.get(self.s.api_url, timeout=15)      # 接口本身直连
                resp.raise_for_status()
                body = resp.json()
                item = (body.get("data") or [{}])[0]
                if body.get("code") != "SUCCESS" or not item.get("server"):
                    raise RuntimeError(f"接口返回异常: {str(body)[:200]}")
            except Exception as e:
                self._last_error_at = time.time()
                raise ProxyUnavailable(f"代理提取失败: {e}") from e
        expires = time.time() + self.s.lease_s
        deadline = parse_deadline(item.get("deadline", ""))
        if deadline is not None:
            expires = min(expires, deadline)
        return Lease(server=item["server"], exit_ip=item.get("proxy_ip", ""), area=item.get("area", ""),
                     expires_at=expires)

    def _check(self, lease: Lease) -> bool:
        """经代理请求 check_url，收到任意 HTTP 响应即合格（Cloudflare 的 403 也说明链路通）。"""
        if not self.s.check_url:
            return True
        try:
            with curl_requests.Session(trust_env=False, impersonate="chrome", verify=certifi.where()) as c:
                c.get(self.s.check_url, proxy=self.s.url_for(lease.server), allow_redirects=False,
                      timeout=self.s.check_timeout_s)
            return True
        except Exception as e:
            logger.debug("IP 体检失败 %s: %s", lease.server, e)
            return False


def parse_deadline(text: str) -> Optional[float]:
    text = re.sub(r"\s+", " ", text or "").strip()   # 接口偶有双空格
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CST).timestamp()
    except ValueError:
        return None
