#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/browser.py
──────────────────────
用一个独立 CloakBrowser 进程经指定代理打开落地页，等 Cloudflare 挑战通过后导出 cookie。

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

logger = logging.getLogger(__name__)

CLEARANCE = "cf_clearance"

# Cloudflare 挑战页特征（按脚本/元素判断；标题会被本地化，如"请稍候…"）
CHALLENGE_JS = """() => !!(window._cf_chl_opt || document.querySelector(
    '#challenge-form, #challenge-stage, .cf-turnstile, script[src*="/cdn-cgi/challenge-platform/"]'))"""


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


async def pass_challenge(
    landing: str,
    lease: Lease,
    proxy: ProxySettings,
    timeout_s: float,
    headed: bool = False,
    debug_dir: str = "",
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
            headless=not headed, proxy=lease.browser_proxy(proxy), stealth_args=False,
            args=args + ["--disable-dev-shm-usage", "--window-size=1440,900"], env=browser_env(),
        )
        context = await browser.new_context()
        page = await context.new_page()
        deadline = t0 + timeout_s
        await page.goto(landing, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
        while True:
            try:
                challenged = bool(await page.evaluate(CHALLENGE_JS))
            except Exception:
                challenged = True     # 挑战通过后的跳转会销毁执行上下文，下一轮再看
            result.challenged |= challenged
            if not challenged:
                cookies = await context.cookies([landing])
                if any(c["name"] == CLEARANCE for c in cookies) or not result.challenged:
                    result.cookies = cookies
                    break
            if time.monotonic() > deadline:
                raise TimeoutError(f"{timeout_s:.0f}s 内未通过挑战（{await _title(page)!r}）")
            await asyncio.sleep(0.5)
        result.user_agent = await page.evaluate("navigator.userAgent")
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


async def _title(page) -> str:
    try:
        return (await page.title())[:60]
    except Exception:
        return ""


async def _screenshot(page, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path))
    except Exception:
        pass
