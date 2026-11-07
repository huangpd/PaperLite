#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/browser.py
──────────────────────
用一个独立 CloakBrowser 进程经指定代理打开落地页，等 Cloudflare 挑战通过后导出 cookie。
遇到交互式 Turnstile（复选框）时自动点击，见 paperlite/turnstile.py。

每次过盾单独 launch：CloakBrowser 的指纹种子（--fingerprint）是启动参数，同一进程内所有 context 共用，
独立进程才能做到一 IP 一指纹。
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from paperlite.proxy import Lease, ProxySettings
from paperlite.turnstile import TurnstileClicker

logger = logging.getLogger(__name__)

CLEARANCE = "cf_clearance"



class DocTracker:
    """
    跟踪主框架最近一次文档响应，按响应头 cf-mitigated 判断当前是不是 Cloudflare 挑战页。
    不在页面里执行 JS：实测挑战期间每 0.5s 在页面主环境调 document.querySelector 会让挑战明显难过
    （ScienceDirect 3 次里 2 次超时，不调时 3 次都约 23s 通过）；也不看 window._cf_chl_opt / challenge-platform 脚本，
    正常页面上 Cloudflare 同样会注入 /cdn-cgi/challenge-platform/scripts/{jsd,precursor} 探针并设 _cf_chl_opt。
    """

    def __init__(self, page) -> None:
        self.page = page
        self.url: str = ""
        self.status: Optional[int] = None
        self.challenged: Optional[bool] = None     # 还没收到文档响应时为 None
        page.on("response", self._on_response)

    def _on_response(self, resp) -> None:
        try:
            if resp.request.is_navigation_request() and resp.frame == self.page.main_frame:
                self.url, self.status = resp.url, resp.status
                self.challenged = bool(resp.headers.get("cf-mitigated"))
        except Exception:
            pass

    def reset(self) -> None:
        """下一次导航前清状态，避免用上一页的 challenged 判断新页（challenged=None 视作"跳转中"）。"""
        self.url, self.status, self.challenged = "", None, None


def browser_env() -> dict[str, str]:
    # macOS 上 DYLD_LIBRARY_PATH=/opt/homebrew/lib 会让 Chromium 启动即 SIGBUS，浏览器进程一律剔除 DYLD_*
    return {k: v for k, v in os.environ.items() if not k.startswith("DYLD_")}


@dataclass
class PassResult:
    ok: bool
    seconds: float
    user_agent: str = ""
    cookies: list[dict] = field(default_factory=list)
    fingerprint: str = ""
    challenged: bool = False
    error: str = ""

    def clearance_expires(self) -> Optional[float]:
        c = next((c for c in self.cookies if c["name"] == CLEARANCE), None)
        return c["expires"] if c and c.get("expires", -1) > 0 else None


def geoip_db_ready() -> tuple[bool, str, str]:
    """geoip 用的 GeoLite2-City.mmdb 是否已就绪；返回 (是否存在, DB 路径, 下载地址)。
    路径从 cloakbrowser 取（变了也跟着变），取不到就退回默认 ~/.cloakbrowser/geoip/。"""
    try:
        from cloakbrowser.geoip import _get_geoip_dir, GEOIP_DB_FILENAME, GEOIP_DB_URL
        path = _get_geoip_dir() / GEOIP_DB_FILENAME
        url = GEOIP_DB_URL
    except Exception:
        from pathlib import Path as _P
        path = _P.home() / ".cloakbrowser" / "geoip" / "GeoLite2-City.mmdb"
        url = "https://github.com/P3TERX/GeoLite.mmdb/raw/download/GeoLite2-City.mmdb"
    return path.exists(), str(path), url


def ensure_geoip_db(connect_timeout: float = 15.0, read_timeout: float = 45.0) -> tuple[bool, str, str]:
    """确保 GeoIP 库就位：已存在直接返回；缺失则**一次性、短超时**下载（connect/read 卡住就快速失败，
    不像 cloakbrowser 那样每次启动都重试 5 分钟）。返回 (是否就绪, DB 路径, 下载地址)。"""
    from pathlib import Path as _P
    ok, path, url = geoip_db_ready()
    if ok:
        return True, path, url
    import httpx
    p = _P(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    try:
        logger.info("首次使用 geoip：下载 GeoIP 库(~70MB) %s …", url)
        with httpx.stream("GET", url, follow_redirects=True,
                          timeout=httpx.Timeout(read_timeout, connect=connect_timeout)) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(65_536):
                    f.write(chunk)
        tmp.replace(p)
        logger.info("GeoIP 库就绪: %s", p)
        return True, path, url
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        logger.warning("GeoIP 库下载失败: %s", str(e).splitlines()[0][:120])
        return False, path, url


async def pass_challenge(
    landing: str,
    lease: Lease,
    proxy: ProxySettings,
    timeout_s: float,
    headed: bool = False,
    debug_dir: str = "",
    geoip: bool = False,
    webrtc_spoof: bool = True,
) -> PassResult:
    """
    打开 landing 直到挑战脚本消失且拿到 cf_clearance；超时返回 ok=False。
    没有挑战（该 IP 直接放行）时 ok=True 但 cookies 里无 cf_clearance，由调用方决定是否采用。
    """
    from cloakbrowser import get_default_stealth_args, launch_async

    t0 = time.monotonic()
    args = [a for a in get_default_stealth_args() if a.split("=", 1)[0] != "--no-sandbox"]
    fingerprint = next((a.split("=", 1)[1] for a in args if a.startswith("--fingerprint=")), "")
    result = PassResult(ok=False, seconds=0, fingerprint=fingerprint)
    browser = None
    page = None
    try:
        browser = await launch_async(
            headless=not headed, proxy=lease.browser_proxy(proxy), stealth_args=False, geoip=geoip,
            args=args + ["--disable-dev-shm-usage", "--window-size=1440,900"]
            + (["--fingerprint-webrtc-ip=auto"] if webrtc_spoof else []), env=browser_env(),
        )
        context = await browser.new_context()
        page = await context.new_page()
        doc = DocTracker(page)
        deadline = t0 + timeout_s
        clicker = TurnstileClicker()
        await page.goto(landing, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
        while True:
            challenged = doc.challenged is not False      # None：跳转中，还没收到新文档
            result.challenged |= challenged
            if not challenged:
                cookies = await context.cookies([landing])
                if any(c["name"] == CLEARANCE for c in cookies) or not result.challenged:
                    result.cookies = cookies
                    break
            if time.monotonic() > deadline:
                raise TimeoutError(f"{timeout_s:.0f}s 内未通过挑战（{await _title(page)!r}，点击 {clicker.clicks} 次）")
            if challenged:
                await clicker.tick(page)     # 交互式 Turnstile（复选框）要点；非交互的不会出现可点控件
            await asyncio.sleep(0.5)
        result.user_agent = await _user_agent(page, deadline)
        result.ok = True
    except Exception as e:
        first = str(e).splitlines()[0][:160] if str(e) else ""
        result.error = f"{type(e).__name__}: {first}"
        if debug_dir and page is not None:
            await _screenshot(page, Path(debug_dir) / f"{lease.exit_ip or lease.server}_{int(time.time())}.png")
    finally:
        result.seconds = round(time.monotonic() - t0, 1)
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.debug("关闭浏览器异常", exc_info=True)
    return result


async def _user_agent(page, deadline: float) -> str:
    """挑战通过时（按响应头判断）新页面可能还在加载，执行上下文会被销毁，等加载完再读。"""
    while True:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
            return await page.evaluate("navigator.userAgent")
        except Exception:
            if time.monotonic() > deadline + 15:
                raise
            await asyncio.sleep(0.5)


async def _title(page) -> str:
    try:
        return (await page.title()).strip()[:200]      # 存库用，别截太短（原来 60 会把文章标题砍半）
    except Exception:
        return ""


async def _screenshot(page, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path))
    except Exception:
        pass


# ── 网页任务的浏览器兜底 ─────────────────────────────────────────────

# 页面里还有短延迟（≤10s）的 meta refresh = 中转页，还会继续跳（只在非挑战页上执行）
PENDING_REFRESH_JS = """() => [...document.querySelectorAll('meta[http-equiv]')].some(m =>
    m.httpEquiv.toLowerCase() === 'refresh' && parseFloat(m.content) <= 10)"""


@dataclass
class RenderResult:
    ok: bool
    seconds: float
    url: str = ""                 # 最终地址
    status: Optional[int] = None  # 最终主文档的 HTTP 状态
    title: str = ""
    html: str = ""
    clicks: int = 0
    error: str = ""


class BrowserSession:
    """
    一个常驻 CloakBrowser 进程 + 一个出口 IP（由传入的 proxy 决定）。
    过盾一次后在同一个 context 里连续 render() 多篇：同站第二篇起多半不再弹挑战，省掉反复启动浏览器。
    用于浏览器下载模式（browser_pool），也被一次性的 render_page() 复用。
    """

    def __init__(self, proxy: Optional[dict], headed: bool = False, geoip: bool = False,
                 webrtc_spoof: bool = True) -> None:
        self.proxy = proxy
        self.headed = headed
        self.geoip = geoip
        self.webrtc_spoof = webrtc_spoof
        self.browser = None
        self.page = None
        self.doc: Optional[DocTracker] = None
        self.clicker = None

    async def start(self) -> None:
        from cloakbrowser import get_default_stealth_args, launch_async
        from paperlite.turnstile import TurnstileClicker

        args = [a for a in get_default_stealth_args() if a.split("=", 1)[0] != "--no-sandbox"]
        self.browser = await launch_async(
            headless=not self.headed, proxy=self.proxy, stealth_args=False, geoip=self.geoip,
            args=args + ["--disable-dev-shm-usage", "--window-size=1440,900"]
            + (["--fingerprint-webrtc-ip=auto"] if self.webrtc_spoof else []), env=browser_env(),
        )
        self.page = await (await self.browser.new_context()).new_page()
        self.doc = DocTracker(self.page)
        self.clicker = TurnstileClicker()

    async def render(self, url: str, timeout_s: float, settle_s: float = 2.0) -> RenderResult:
        """
        在常驻浏览器里打开 url，让它自己完成跳转（HTTP 3xx、meta refresh、JS 跳转）和 Cloudflare 挑战
        （交互式 Turnstile 自动点），等页面不再是挑战页、无待执行 meta refresh、地址 settle_s 秒不变后返回。
        clicks 记本次新增的点击数。失败（超时/异常）时 ok=False，浏览器仍可复用于下一篇。
        """
        page, doc, clicker = self.page, self.doc, self.clicker
        t0 = time.monotonic()
        result = RenderResult(ok=False, seconds=0)
        clicks0 = clicker.clicks
        try:
            doc.reset()                       # 别用上一页的 challenged 状态判断新页
            await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
            deadline = t0 + timeout_s
            last_url, stable_since = "", time.monotonic()
            while True:
                busy = doc.challenged is not False
                if not busy:
                    try:
                        busy = bool(await page.evaluate(PENDING_REFRESH_JS))
                    except Exception:
                        busy = True               # 跳转中，执行上下文被销毁
                now = time.monotonic()
                if page.url != last_url:
                    last_url, stable_since = page.url, now
                if not busy and now - stable_since >= settle_s:
                    break
                if now > deadline:
                    raise TimeoutError(f"{timeout_s:.0f}s 内页面未稳定（{await _title(page)!r}，{page.url[:120]}，点击 {clicker.clicks - clicks0} 次）")
                if doc.challenged:
                    await clicker.tick(page)
                await asyncio.sleep(0.5)
            result.url = page.url
            result.status = doc.status
            result.title = await _title(page)
            result.html = await page.content()
            result.clicks = clicker.clicks - clicks0
            result.ok = True
        except Exception as e:
            first = str(e).splitlines()[0][:200] if str(e) else ""
            result.error = f"{type(e).__name__}: {first}"
        finally:
            result.seconds = round(time.monotonic() - t0, 1)
        return result

    async def close(self) -> None:
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                logger.debug("关闭浏览器异常", exc_info=True)
            self.browser = self.page = self.doc = self.clicker = None


async def render_page(url: str, proxy: Optional[dict], timeout_s: float, settle_s: float = 2.0,
                      headed: bool = False, geoip: bool = False, webrtc_spoof: bool = True) -> RenderResult:
    """一次性渲染一个 url：起一个浏览器 → render → 关。批量用 BrowserSession 复用同一浏览器。"""
    session = BrowserSession(proxy, headed, geoip, webrtc_spoof)
    try:
        await session.start()
        r = await session.render(url, timeout_s, settle_s)
    except Exception as e:
        first = str(e).splitlines()[0][:200] if str(e) else ""
        r = RenderResult(ok=False, seconds=0, error=f"{type(e).__name__}: {first}")
    finally:
        await session.close()
    return r
