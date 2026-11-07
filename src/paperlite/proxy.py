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
from io import BytesIO
from typing import Optional
from urllib.parse import quote

import certifi
from curl_cffi import requests as curl_requests
from curl_cffi import Curl
from curl_cffi.const import CurlInfo, CurlOpt

from paperlite.logutil import log_event

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))
_API_RETRY_S = 5.0          # 接口失败后的最短重试间隔
_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _extract_ip(text: str) -> str:
    """从体检响应里抠出出口 IP：cloudflare trace 的 ``ip=1.2.3.4`` 或 httpbin 的 ``"origin": "1.2.3.4"``。"""
    m = re.search(r"ip=" + _IPV4_RE.pattern, text) or _IPV4_RE.search(text or "")
    return m.group(1) if m else ""


@dataclass
class ProbeResult:
    """一次体检探针的结果与分段计时（毫秒）。"""
    ok: bool                       # 是否拿到 HTTP 响应（连得上、TLS 通、收到状态码）
    status: int = 0
    tcp_ms: float = 0.0            # TCP 连到代理耗时
    tls_ms: float = 0.0            # TLS 握手耗时（到目标，经代理隧道）
    ttfb_ms: float = 0.0           # 首字节时间
    total_ms: float = 0.0          # 总耗时
    egress: str = ""               # 响应回显的出口 IP
    error: str = ""


class ProxyUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ProxySettings:
    api_url: str
    scheme: str = "http"
    username: str = ""
    password: str = ""
    lease_s: float = 600              # 单个 IP 最长使用时间，与接口 deadline 取较早者
    check_url: str = "https://www.cloudflare.com/cdn-cgi/trace"   # 首选判定目标（回显出口 IP）
    check_url2: str = "https://httpbin.org/ip"                    # 备用判定目标（回显 origin IP），去单点抖动；两者任一通过即合格
    connect_timeout_s: float = 3                                  # 先 TCP 连代理端口，连不上快速弃（不耗满 check_timeout_s）
    check_timeout_s: float = 10                                   # 单次体检 HTTP 请求超时
    check_max_latency_ms: float = 3000                            # 体检往返延迟超过此值即弃（慢代理大概率过盾也慢）；0 关闭
    verify_exit_ip: bool = True                                   # 核对回显出口 IP 是否等于接口给的 proxy_ip（证明真转发）
    check_max_tries: int = 3

    def browser_proxy_for(self, server: str) -> dict:
        """ip:port → Playwright 代理配置（浏览器兜底下载用，与下载线程同一出口）。"""
        proxy = {"server": f"{self.scheme}://{server}"}
        if self.username:
            proxy.update(username=self.username, password=self.password)
        return proxy

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

    def left(self, now: Optional[float] = None) -> float:
        return self.expires_at - (time.time() if now is None else now)

    def browser_proxy(self, s: ProxySettings) -> dict:
        return s.browser_proxy_for(self.server)


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
        """分层体检：经代理探一个判定目标，要求 ① 连得上+TLS 通+2xx → ② 回显出口 IP == proxy_ip →
        ③ 往返延迟不超阈值。两个判定目标任一通过即合格（去单点抖动）。每个 IP 记一条 proxy_check
        结构化日志，含 TCP / TLS握手 / TTFB / 总耗时四段，便于聚合评估代理质量。"""
        targets = [u for u in (self.s.check_url, self.s.check_url2) if u]
        if not targets:
            return True
        last = None
        for url in targets:
            pr = self._probe(url, lease)
            last = pr
            reason = ""
            if not pr.ok:
                reason = pr.error or "no_response"
            elif not 200 <= pr.status < 300:
                reason = f"status_{pr.status}"
            elif self.s.verify_exit_ip and lease.exit_ip and pr.egress and pr.egress != lease.exit_ip:
                reason = f"exit_ip_mismatch({pr.egress})"
            elif self.s.check_max_latency_ms and pr.total_ms > self.s.check_max_latency_ms:
                reason = f"slow_{pr.total_ms:.0f}ms"
            if not reason:
                self._log_probe(lease, url, pr, ok=True)
                return True
            self._log_probe(lease, url, pr, ok=False, reason=reason)
        return False

    def _log_probe(self, lease: Lease, url: str, pr: "ProbeResult", ok: bool, reason: str = "") -> None:
        log_event(logger, logging.INFO if ok else logging.DEBUG, "proxy_check",
                  (f"IP {lease.server} 体检通过 {pr.total_ms:.0f}ms" if ok
                   else f"IP {lease.server} 体检不过：{reason}"),
                  exit_ip=lease.exit_ip, target=url, passed=ok, reason=reason, status=pr.status,
                  tcp_ms=round(pr.tcp_ms, 1), tls_ms=round(pr.tls_ms, 1),
                  ttfb_ms=round(pr.ttfb_ms, 1), total_ms=round(pr.total_ms, 1))

    def _probe(self, url: str, lease: Lease) -> ProbeResult:
        """经代理 GET 一个判定目标（curl 低层接口），返回状态 + 分段计时 + 回显出口 IP。"""
        c = Curl()
        buf = BytesIO()
        try:
            c.setopt(CurlOpt.URL, url.encode())
            c.setopt(CurlOpt.PROXY, self.s.url_for(lease.server).encode())
            c.setopt(CurlOpt.WRITEDATA, buf)
            c.setopt(CurlOpt.FOLLOWLOCATION, 0)
            c.setopt(CurlOpt.CONNECTTIMEOUT_MS, int(self.s.connect_timeout_s * 1000))
            c.setopt(CurlOpt.TIMEOUT_MS, int(self.s.check_timeout_s * 1000))
            c.impersonate("chrome")
            c.perform()
            connect = c.getinfo(CurlInfo.CONNECT_TIME) or 0.0
            appconnect = c.getinfo(CurlInfo.APPCONNECT_TIME) or 0.0
            return ProbeResult(
                ok=True, status=int(c.getinfo(CurlInfo.RESPONSE_CODE) or 0),
                tcp_ms=connect * 1000,
                tls_ms=(appconnect - connect) * 1000 if appconnect > connect else 0.0,
                ttfb_ms=(c.getinfo(CurlInfo.STARTTRANSFER_TIME) or 0.0) * 1000,
                total_ms=(c.getinfo(CurlInfo.TOTAL_TIME) or 0.0) * 1000,
                egress=_extract_ip(buf.getvalue().decode("utf-8", "replace")))
        except Exception as e:
            return ProbeResult(ok=False, error=f"{type(e).__name__}: {str(e).splitlines()[0][:120]}")
        finally:
            c.close()


def parse_deadline(text: str) -> Optional[float]:
    text = re.sub(r"\s+", " ", text or "").strip()   # 接口偶有双空格
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CST).timestamp()
    except ValueError:
        return None
