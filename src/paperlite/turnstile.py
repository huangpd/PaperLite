#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/turnstile.py
────────────────────────
Cloudflare Turnstile：点复选框 + 按 sitekey 取 token。

两种用法：
1. TurnstileClicker：页面上已经有 Turnstile 控件（CF 挑战页、站点自己嵌的 "Are you a robot?" 页），
   在轮询循环里反复调 tick(page)，看到控件就点，卡在 "Verifying…" 超过 reclick_s 再点一次。
2. solve_turnstile(url, sitekey)：通用取 token。在目标 URL 的 origin 下（page.route 拦截文档请求）
   渲染一个只含该 sitekey 的最小页面，点复选框，从 callback 拿 token。
   Turnstile 校验的是页面 hostname，所以必须用目标站 URL，不能用 about:blank 或本地页。

控件在 challenges.cloudflare.com 的跨域 iframe 里，外层往往是 closed shadow root，选择器够不到；
这里通过 page.frames 找到该 frame，取其宿主 iframe 的 bounding box，按坐标用鼠标点（真实输入事件）。
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urldefrag, urlsplit

logger = logging.getLogger(__name__)

TURNSTILE_HOST = "challenges.cloudflare.com"

# 实测：第一次点击后偶尔一直 "Verifying…"，过几秒再点一次就放行；只点一次会卡到超时
RECLICK_S = 8.0


async def find_widget(page) -> Optional[dict]:
    """返回可见 Turnstile iframe 的 bounding box（页面坐标），没有则 None。"""
    for frame in page.frames:
        if TURNSTILE_HOST not in frame.url:
            continue
        try:
            el = await frame.frame_element()
            box = await el.bounding_box()
        except Exception:
            continue              # frame 正在销毁/重建
        # invisible 模式或尚未展开的控件尺寸是 0，没有可点的复选框
        if box and box["width"] >= 100 and box["height"] >= 50:
            return box
    return None


def checkbox_point(box: dict) -> tuple[float, float]:
    """复选框在控件内的位置：normal / flexible 是左侧约 16–40px、垂直居中；compact（150×140）在左上部。"""
    if box["height"] > 100:      # compact
        return box["x"] + random.uniform(20, 36), box["y"] + random.uniform(36, 52)
    return box["x"] + random.uniform(18, 34), box["y"] + box["height"] / 2 + random.uniform(-5, 5)


async def human_click(page, x: float, y: float) -> None:
    vw = (page.viewport_size or {"width": 1280, "height": 720})
    await page.mouse.move(random.uniform(0.2, 0.8) * vw["width"], random.uniform(0.4, 0.9) * vw["height"])
    await page.mouse.move(x, y, steps=random.randint(15, 30))
    await asyncio.sleep(random.uniform(0.1, 0.35))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.up()


@dataclass
class TurnstileClicker:
    """在轮询循环里调用 tick()：控件出现后稍等渲染完成再点，之后每 reclick_s 秒补点一次。"""
    reclick_s: float = RECLICK_S
    settle_s: float = 1.0             # 控件出现到第一次点击的等待，太早点复选框还没画出来
    clicks: int = 0
    _seen_at: float = 0.0
    _last_click: float = 0.0

    async def tick(self, page) -> bool:
        """需要时点一次；返回本次是否点击。"""
        box = await find_widget(page)
        now = time.monotonic()
        if box is None:
            self._seen_at = 0.0
            return False
        if not self._seen_at:
            self._seen_at = now
        if now - self._seen_at < self.settle_s or now - self._last_click < self.reclick_s:
            return False
        x, y = checkbox_point(box)
        try:
            await human_click(page, x, y)
        except Exception:
            logger.debug("点击 Turnstile 失败", exc_info=True)
            return False
        self._last_click = time.monotonic()
        self.clicks += 1
        logger.debug("点击 Turnstile 第 %d 次 (%.0f, %.0f)", self.clicks, x, y)
        return True


# ── 按 sitekey 取 token ──────────────────────────────────────────────

_HARNESS = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<script>
window.__ts = {{token: null, error: null}};
function __tsLoad() {{
  turnstile.render('#ts', Object.assign({opts}, {{
    callback: function (t) {{ window.__ts.token = t; }},
    'error-callback': function (e) {{ window.__ts.error = String(e); }},
    'expired-callback': function () {{ window.__ts.token = null; }}
  }}));
}}
</script>
<script src="https://{host}/turnstile/v0/api.js?onload=__tsLoad&render=explicit" async defer></script>
</head><body style="margin:0;font-family:sans-serif">
<div style="padding:{top}px 0 0 {left}px"><div id="ts"></div></div>
</body></html>"""

_TOKEN_JS = """() => window.__ts.token
  || (window.turnstile && (() => { try { return turnstile.getResponse() } catch (e) { return null } })())
  || null"""


@dataclass
class TokenResult:
    ok: bool
    seconds: float
    token: str = ""
    user_agent: str = ""
    clicks: int = 0
    error: str = ""
    cookies: list[dict] = field(default_factory=list)


def harness_html(sitekey: str, action: str = "", cdata: str = "", title: str = "") -> str:
    opts = {"sitekey": sitekey, "size": "normal", "retry": "auto", "refresh-expired": "auto"}
    if action:
        opts["action"] = action
    if cdata:
        opts["cData"] = cdata
    return _HARNESS.format(
        # < 转义成 <：参数里的 </script> 不能截断内联脚本
        title=title or "Just a moment", opts=json.dumps(opts).replace("<", "\\u003c"), host=TURNSTILE_HOST,
        top=random.randint(80, 220), left=random.randint(40, 320),
    )


def parse_proxy(url: str) -> Optional[dict]:
    """http://user:pass@host:port → Playwright proxy dict。"""
    if not url:
        return None
    u = urlsplit(url)
    if not u.hostname or not u.port:
        raise ValueError(f"代理格式应为 scheme://[user:pass@]host:port：{url!r}")
    proxy = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port}"}
    if u.username:
        proxy.update(username=u.username, password=u.password or "")
    return proxy


async def solve_turnstile(
    url: str,
    sitekey: str,
    *,
    action: str = "",
    cdata: str = "",
    proxy: Optional[dict] = None,
    timeout_s: float = 60,
    headed: bool = False,
    user_agent: str = "",
) -> TokenResult:
    """
    在 url 的 origin 下渲染 sitekey 对应的 Turnstile，点击直到拿到 token。
    token 要和出口 IP / UA 一起用：提交时请用同一代理和返回的 user_agent。
    """
    from cloakbrowser import get_default_stealth_args, launch_async
    from paperlite.browser import browser_env

    t0 = time.monotonic()
    result = TokenResult(ok=False, seconds=0)
    target, _ = urldefrag(url)
    html = harness_html(sitekey, action, cdata)
    browser = None
    try:
        args = [a for a in get_default_stealth_args() if a.split("=", 1)[0] != "--no-sandbox"]
        browser = await launch_async(
            headless=not headed, proxy=proxy, stealth_args=False,
            args=args + ["--disable-dev-shm-usage", "--window-size=1440,900"], env=browser_env(),
        )
        context = await browser.new_context(**({"user_agent": user_agent} if user_agent else {}))

        async def fulfill(route):
            if route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
            else:
                await route.continue_()

        # 谓词匹配：URL 里的 ? * 等在 glob 里有特殊含义
        await context.route(lambda u: urldefrag(u)[0] == target, fulfill)
        page = await context.new_page()
        await page.goto(target, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))

        clicker = TurnstileClicker()
        deadline = t0 + timeout_s
        while True:
            token = await page.evaluate(_TOKEN_JS)
            if token:
                result.token = token
                break
            if time.monotonic() > deadline:
                err = await page.evaluate("() => window.__ts.error")
                raise TimeoutError(f"{timeout_s:.0f}s 内未拿到 token" + (f"（error {err}）" if err else ""))
            await clicker.tick(page)
            await asyncio.sleep(0.5)
        result.clicks = clicker.clicks
        result.user_agent = await page.evaluate("navigator.userAgent")
        result.cookies = await context.cookies()
        result.ok = True
    except Exception as e:
        first = str(e).splitlines()[0][:200] if str(e) else ""
        result.error = f"{type(e).__name__}: {first}"
    finally:
        result.seconds = round(time.monotonic() - t0, 1)
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.debug("关闭浏览器异常", exc_info=True)
    return result


# ── HTTP 服务：POST /solve ───────────────────────────────────────────

def serve(host: str = "127.0.0.1", port: int = 8191, concurrency: int = 4, headed: bool = False,
          default_proxy: str = "", max_timeout_s: float = 120) -> None:
    """
    POST /solve  {"url": ..., "sitekey": ..., "action"?, "cdata"?, "proxy"?: "http://u:p@h:port", "timeout"?: 60}
    → {"ok", "token", "user_agent", "seconds", "clicks", "error"}
    每个请求独立起一个浏览器进程（一请求一指纹），并发由 concurrency 限制，超出的排队。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    slots = threading.BoundedSemaphore(concurrency)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(200, {"ok": True}) if self.path == "/health" else self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/solve":
                return self._send(404, {"error": "not found"})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                url, sitekey = req["url"], req["sitekey"]
                if urlsplit(url).scheme not in ("http", "https"):
                    raise ValueError("url 须为 http(s)")
                proxy = parse_proxy(req.get("proxy") or default_proxy)
                timeout_s = min(float(req.get("timeout") or 60), max_timeout_s)
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
                return self._send(400, {"ok": False, "error": f"参数错误: {e}"})
            with slots:
                r = asyncio.run(solve_turnstile(
                    url, sitekey, action=req.get("action") or "", cdata=req.get("cdata") or "",
                    proxy=proxy, timeout_s=timeout_s, headed=headed))
            logger.info("solve %s %s → %s %.1fs clicks=%d %s", urlsplit(url).hostname, sitekey[:12],
                        "ok" if r.ok else "fail", r.seconds, r.clicks, r.error)
            self._send(200, {"ok": r.ok, "token": r.token, "user_agent": r.user_agent,
                             "seconds": r.seconds, "clicks": r.clicks, "error": r.error})

        def log_message(self, fmt, *args):
            logger.debug("%s " + fmt, self.address_string(), *args)

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    logger.info("Turnstile 服务 http://%s:%d  POST /solve，并发 %d", host, port, concurrency)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
