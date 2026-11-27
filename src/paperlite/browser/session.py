#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/browser/session.py
────────────────────────────
用一个独立 CloakBrowser 进程经指定代理打开站点页面（curl 模式的生产者用站点首页），等 Cloudflare 挑战通过后导出 cookie。
遇到交互式 Turnstile（复选框）时自动点击，见 browser/turnstile.py。

每次过盾单独 launch：CloakBrowser 的指纹种子（--fingerprint）是启动参数，同一进程内所有 context 共用，
独立进程才能做到一 IP 一指纹。
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from paperlite.browser.env import browser_env
from paperlite.config import ProxySettings
from paperlite.errors import UserError
from urllib.parse import urlsplit

from paperlite.rules.page import classify_waf, site_key, visible_text_len
from paperlite.proxy import Lease
from paperlite.browser.turnstile import TurnstileClicker

if TYPE_CHECKING:
    from paperlite.browser.pdf_capture import CapturedPdf

logger = logging.getLogger(__name__)

CLEARANCE = "cf_clearance"
_REQ_GRACE_S = 2.0          # 正文已够字数且稳定时，最多再等站点在途请求这么久


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
        self.waf: str = ""                         # 本次导航途中见到的 WAF（监控用；跳转链上任一主文档识别到即记）
        # 当前主文档有没有解析完（DOMContentLoaded）。过盾后真页面与挑战页常是同一 URL，只看"地址不变"会在
        # 真页面响应头刚到、<body> 还没解析时就收页（实测存下的 HTML 只有 <head>）
        self.loaded: bool = False
        self.doc_at: float = 0.0                   # 最近一次主文档响应的时刻（monotonic）
        page.on("response", self._on_response)
        page.on("domcontentloaded", self._on_dcl)

    def _on_response(self, resp) -> None:
        try:
            if resp.request.is_navigation_request() and resp.frame == self.page.main_frame:
                self.url, self.status = resp.url, resp.status
                self.loaded, self.doc_at = False, time.monotonic()
                self.challenged = bool(resp.headers.get("cf-mitigated"))
                waf = classify_waf(resp.headers)
                if waf != "none" or not self.waf:
                    self.waf = waf
        except Exception:
            pass

    def _on_dcl(self, _page=None) -> None:
        self.loaded = True

    def reset(self) -> None:
        """下一次导航前清状态，避免用上一页的 challenged 判断新页（challenged=None 视作"跳转中"）。"""
        self.url, self.status, self.challenged = "", None, None
        self.waf = ""
        self.loaded, self.doc_at = False, 0.0


class SiteRequests:
    """
    记录页面发出、还没结束的 XHR / fetch / 脚本请求，供"内容是否加载完"判断：只算站点自己（与当前页面同一
    site_key）的请求——单页应用（IEEE Xplore 等）靠它们拉正文；第三方统计/广告请求可能一直不断，不算。
    只听 Playwright 的网络事件，不在页面里执行 JS。
    """

    TYPES = frozenset({"xhr", "fetch", "script"})
    STALE_S = 15.0                                 # 挂这么久还没结束的（长轮询、推送）不再算"在加载"

    def __init__(self, page) -> None:
        self.page = page
        self._open: dict = {}                      # request → (主机, 开始时刻)
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_done)
        page.on("requestfailed", self._on_done)

    def _on_request(self, req) -> None:
        try:
            if req.resource_type in self.TYPES:
                self._open[req] = (urlsplit(req.url).hostname or "", time.monotonic())
        except Exception:
            pass

    def _on_done(self, req) -> None:
        self._open.pop(req, None)

    def pending(self) -> int:
        site = site_key(urlsplit(self.page.url).hostname or "")
        now = time.monotonic()
        for req, (_, t) in list(self._open.items()):
            if now - t > 60:                       # 漏了结束事件的，别一直占着
                self._open.pop(req, None)
        return sum(1 for h, t in self._open.values() if now - t <= self.STALE_S and site_key(h) == site)


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


def require_geoip(cfg) -> None:
    """geoip=true：启动前确保 GeoIP 库就位——缺失则限时下载一次；下不动就快速报错给手动预置指引
    （而不是让 cloakbrowser 每次启动都卡 5 分钟重试）。"""
    if not cfg.geoip:
        return
    ok, path, url = ensure_geoip_db()
    if ok:
        return
    raise UserError(
        f"geoip = true，但 GeoIP 库下载不下来（服务器/受限网络常连不上 GitHub），这里先拦下不让它卡死。\n"
        f"目标路径：{path}\n"
        f"请手动装好再跑，二选一：\n"
        f"  1) 能连 GitHub 的机器上下载后放到该路径（或在别处下好 scp 过去）：\n"
        f"     curl -L --create-dirs -o '{path}' '{url}'\n"
        f"  2) 不用 geoip（WebRTC 伪造仍由 webrtc_spoof 保留）：在 config.toml 的 [limits] 设 geoip = false")


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
    waf: str = ""                 # 识别到的 WAF（cloudflare/aws_waf/datadome/none），监控统计用
    pdf: Optional["CapturedPdf"] = None   # 要 PDF 时：截到并写盘的 PDF（见 pdf_capture.py）
    pdf_error: str = ""           # 要 PDF 时：截到了 PDF 响应但校验没过（不是 PDF、过小、过大、不完整）


class BrowserSession:
    """
    一个常驻 CloakBrowser 进程 + 一个出口 IP（由传入的 proxy 决定）。
    过盾一次后在同一个 context 里连续 render() 多篇：同站第二篇起多半不再弹挑战，省掉反复启动浏览器。
    用于浏览器下载模式（browser/pool.py），也被一次性的 render_page() 复用。
    """

    def __init__(self, proxy: Optional[dict], headed: bool = False, geoip: bool = False,
                 webrtc_spoof: bool = True) -> None:
        self.proxy = proxy
        self.headed = headed
        self.geoip = geoip
        self.webrtc_spoof = webrtc_spoof
        self.browser = None
        self.context = None
        self.page = None
        self.doc: Optional[DocTracker] = None
        self.reqs: Optional[SiteRequests] = None
        self.clicker = None

    async def start(self) -> None:
        from cloakbrowser import get_default_stealth_args, launch_async
        from paperlite.browser.turnstile import TurnstileClicker

        args = [a for a in get_default_stealth_args() if a.split("=", 1)[0] != "--no-sandbox"]
        self.browser = await launch_async(
            headless=not self.headed, proxy=self.proxy, stealth_args=False, geoip=self.geoip,
            args=args + ["--disable-dev-shm-usage", "--window-size=1440,900"]
            + (["--fingerprint-webrtc-ip=auto"] if self.webrtc_spoof else []), env=browser_env(),
        )
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.doc = DocTracker(self.page)
        self.reqs = SiteRequests(self.page)
        self.clicker = TurnstileClicker()

    async def render(self, url: str, timeout_s: float, settle_s: float = 2.0, *, pdf_dest: Optional[Path] = None,
                     max_bytes: int = 200 * 1024 * 1024, min_bytes: int = 1024, stall_s: float = 120,
                     content_wait_s: float = 0, min_text: int = 0) -> RenderResult:
        """
        在常驻浏览器里打开 url，让它自己完成跳转（HTTP 3xx、meta refresh、JS 跳转）和 Cloudflare 挑战
        （交互式 Turnstile 自动点），等页面不再是挑战页、无待执行 meta refresh、地址 settle_s 秒不变后返回。
        clicks 记本次新增的点击数。失败（超时/异常）时 ok=False，浏览器仍可复用于下一篇。

        content_wait_s > 0（只用于要网页的任务）：地址稳定后再等正文加载完——可见文字约 1 秒不变（±2%）、不少于
        min_text（HTTP ≥ 400 的错误页不看字数），且站点自己的请求都结束（字数够时最多再给它们 2 秒），
        最多再等 content_wait_s 秒，到时照常收页
        （是否空壳由调用方用 rules.page.page_problem 判）。服务端渲染的页面在地址稳定的那 settle_s 秒里就满足，不多等。

        给了 pdf_dest：打开前开 CDP 拦截（pdf_capture.PdfCapture），途中任何一个响应是 PDF 就流式写到 pdf_dest，
        立刻返回（result.pdf）；最后停在网页上就照常返回网页（调用方按 non_pdf 处理）。
        """
        page, doc, clicker = self.page, self.doc, self.clicker
        t0 = time.monotonic()
        result = RenderResult(ok=False, seconds=0)
        clicks0 = clicker.clicks
        capture = None
        try:
            doc.reset()                       # 别用上一页的 challenged 状态判断新页
            if pdf_dest is not None:
                from paperlite.browser.pdf_capture import PdfCapture
                auth = ((self.proxy["username"], self.proxy.get("password", ""))
                        if self.proxy and self.proxy.get("username") else None)
                capture = await PdfCapture.create(self.context, page, url, pdf_dest, max_bytes=max_bytes,
                                                  min_bytes=min_bytes, proxy_auth=auth, stall_s=stall_s)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
            except Exception:
                # PDF 被截下后原请求会被中止，goto 报 ERR_ABORTED / 下载开始之类的错，属正常
                if not (capture and await _settle_capture(capture)):
                    raise
            deadline = t0 + timeout_s
            last_url, stable_since = "", time.monotonic()
            wait_content = content_wait_s > 0 and capture is None
            html, text_len, text_since = "", -1, 0.0
            content_from: Optional[float] = None     # 地址稳定（可以收页）的时刻；内容等待从这里算
            while True:
                if capture and capture.result.done():
                    break
                busy = doc.challenged is not False or not doc.loaded
                if not busy:
                    try:
                        busy = bool(await page.evaluate(PENDING_REFRESH_JS))
                    except Exception:
                        busy = True               # 跳转中，执行上下文被销毁
                now = time.monotonic()
                if page.url != last_url:
                    last_url, stable_since = page.url, now
                stable_since = max(stable_since, doc.doc_at)          # 同一 URL 换了文档（过盾后）也重新计时
                if busy:
                    content_from, text_len = None, -1
                elif wait_content:
                    try:
                        html = await page.content()
                    except Exception:
                        html = ""                 # 正在换文档
                    n = visible_text_len(html)
                    if abs(n - text_len) > max(50, text_len // 50):     # 2% 以内的变动（计数器、广告）算没变
                        text_len, text_since = n, now
                if not busy and now - stable_since >= settle_s:
                    if not wait_content:
                        break
                    content_from = content_from or now
                    enough = text_len >= min_text or (doc.status or 200) >= 400
                    # 字数够且 1 秒没变：站点请求都结束就收；还有请求在途也最多再给 _REQ_GRACE_S
                    # （服务端渲染的页面常有站点自己的埋点/推荐脚本迟迟不结束，不必等它们）
                    settled = html and enough and now - text_since >= 1.0
                    if (settled and (self.reqs.pending() == 0 or now - content_from >= _REQ_GRACE_S)) \
                            or now - content_from >= content_wait_s:
                        break
                if now > deadline and content_from is not None:
                    break                         # 页面已可收，只是在等正文：到总时限就按现状收（空壳由调用方判）
                if now > deadline and not (capture and capture.streaming):     # 正在写 PDF：慢但没坏，接着等
                    raise TimeoutError(f"{timeout_s:.0f}s 内页面未稳定（{await _title(page)!r}，{page.url[:120]}，点击 {clicker.clicks - clicks0} 次）")
                if doc.challenged:
                    await clicker.tick(page)
                await asyncio.sleep(0.5)
            result.clicks = clicker.clicks - clicks0
            if capture and capture.result.done() and not capture.result.cancelled():
                got = capture.result.result()
                result.pdf, result.url, result.status, result.ok = got, got.final_url, got.status, True
                return result
            result.url = page.url
            result.status = doc.status
            result.title = await _title(page)
            result.html = html if wait_content and html else await page.content()
            result.ok = True
            if capture:
                result.pdf_error = capture.last_error
        except Exception as e:
            first = str(e).splitlines()[0][:200] if str(e) else ""
            result.error = f"{type(e).__name__}: {first}"
        finally:
            if capture is not None:
                await capture.close()
            result.seconds = round(time.monotonic() - t0, 1)
            result.waf = doc.waf or "none"
        return result

    async def close(self) -> None:
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                logger.debug("关闭浏览器异常", exc_info=True)
            self.browser = self.page = self.doc = self.clicker = None


async def _settle_capture(capture, wait_s: float = 3.0, stream_limit_s: float = 1800) -> bool:
    """goto 报错后等截获结果：PDF 被截下后原请求中止（goto 报 ERR_ABORTED），或大文件写盘期间 goto 超时。
    正在写盘就一直等到写完（最多 stream_limit_s），否则再等 wait_s；截到合格 PDF 返回 True。"""
    loop = asyncio.get_running_loop()
    start = loop.time()
    while not capture.result.done():
        elapsed = loop.time() - start
        if elapsed > stream_limit_s or (not capture.streaming and elapsed > wait_s):
            return False
        await asyncio.sleep(0.2)
    return not capture.result.cancelled()


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
