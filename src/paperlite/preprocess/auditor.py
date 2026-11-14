#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite.preprocess.auditor
────────────────────────────
出版商反爬机制体检与模式裁决器。
基于无头浏览器与 cURL 的【双视角差分核验】，彻底识破 SPA 假空壳页，定级 curl vs browser。
"""

import asyncio
import logging
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

from curl_cffi import requests as cf_requests

from paperlite.browser import browser_env

from paperlite.preprocess.models import DomainAuditResult, GroundTruth, RouteMode
from paperlite.turnstile import TurnstileClicker

logger = logging.getLogger("paperlite.preprocess.auditor")

# SPA / 空壳页典型的泛化标题黑名单
GENERIC_PLACEHOLDERS = [
    "just a moment",
    "请稍候",
    "loading",
    "attention required",
    "security check",
    "checking your browser",
    "access denied",
    "robot",
    "captcha",
]


class DifferentialJudge:
    """语义差分与真值保真度判决器"""

    @classmethod
    def evaluate(
        cls,
        ground_truth: GroundTruth,
        curl_status: int,
        curl_headers: dict,
        curl_body: str,
    ) -> tuple[bool, float, str]:
        """
        比对 cURL 响应与浏览器真值基准。
        返回: (is_fidelity_ok: bool, score: float, reason: str)
        """
        # 1. 状态码与 WAF 质询检查
        if curl_status != 200:
            return False, 0.0, f"HTTP状态码异常 ({curl_status})"

        lower_headers = {str(k).lower(): str(v).lower() for k, v in curl_headers.items()}
        if "cf-mitigated" in lower_headers or "access denied" in curl_body.lower():
            return False, 0.0, "触发 WAF 阻断/质询"

        m = re.search(r"<title[^>]*>(.*?)</title>", curl_body, re.I | re.S)
        curl_title = m.group(1).strip() if m else ""

        # 2. 检查 cURL 标题是否为 SPA 占位符
        if any(p in curl_title.lower() for p in GENERIC_PLACEHOLDERS) or not curl_title:
            return False, 0.1, f"标题为通用占位符/未水合 ({curl_title!r})"

        # 3. 检查页面正文长度比率 (SPA 往往只有几 KB 骨架)
        # 剥除标签提取可见字符
        text_only = re.sub(r"<[^>]+>", " ", curl_body)
        text_only = re.sub(r"\s+", " ", text_only).strip()
        visible_len = len(text_only)

        if visible_len < 500:
            return False, 0.2, f"正文可见字符过少 ({visible_len} 字符，疑似 SPA 空壳)"

        # 4. 核心摘要/正文锚点匹配 (浏览器真值里的前 50 字是否在 cURL 返回文本中)
        if ground_truth.abstract_anchor and ground_truth.abstract_anchor.lower() not in curl_body.lower():
            # 真实文章摘要未在静态 HTML 出现，高度疑似前端异步 JSON 水合
            return False, 0.3, "未检测到真实摘要锚点文本 (前端异步渲染)"

        return True, 1.0, "语义内容保真度良好"


class DomainAuditor:
    """独立域名体检器"""

    def __init__(self, proxy: Optional[str] = None, timeout_s: float = 45.0):
        self.proxy = proxy
        self.timeout_s = timeout_s

    async def get_ground_truth(self, url: str) -> GroundTruth:
        """步骤 A: 使用 CloakBrowser 加载目标页面，建立真值基准"""
        from cloakbrowser import get_default_stealth_args, launch_async

        host = urlparse(url).netloc.lower()
        args = [a for a in get_default_stealth_args() if a.split("=", 1)[0] != "--no-sandbox"]
        browser_proxy = {"server": self.proxy} if self.proxy else None

        browser = None
        try:
            browser = await launch_async(
                headless=True,
                proxy=browser_proxy,
                stealth_args=False,
                args=args + ["--disable-dev-shm-usage", "--window-size=1440,900"],
                env=browser_env(),
            )
            context = await browser.new_context()
            page = await context.new_page()
            clicker = TurnstileClicker()

            await page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout_s * 1000))

            # 等待过盾或加载稳定 (遵守配置的超时时间)
            deadline = time.monotonic() + self.timeout_s
            while time.monotonic() < deadline:
                await asyncio.sleep(1)
                t = (await page.title()).strip()
                if not any(p in t.lower() for p in GENERIC_PLACEHOLDERS) and t:
                    break
                await clicker.tick(page)

            final_title = (await page.title()).strip()
            content = await page.content()
            ua = await page.evaluate("navigator.userAgent")
            cookies = await context.cookies([url])

            # 提取正文文本与核心摘要锚点 (前 50 字符)
            clean_text = re.sub(r"<[^>]+>", " ", content)
            clean_text = re.sub(r"\s+", " ", clean_text).strip()

            # 从剥除 HTML 标签后的纯文本中检索 Abstract 真实内容
            anchor = ""
            m_abs = re.search(r"\b(?:abstract|summary)\b[\s:]*([^\n\r]{30,80})", clean_text, re.I)
            if m_abs:
                anchor = m_abs.group(1).strip()[:50]

            has_clearance = any(c["name"] == "cf_clearance" for c in cookies)

            return GroundTruth(
                url=url,
                host=host,
                title=final_title,
                visible_text_len=len(clean_text),
                abstract_anchor=anchor,
                has_clearance=has_clearance,
                cookies=cookies,
                user_agent=ua,
            )
        except Exception as e:
            logger.warning(f"获取浏览器真值失败 [{host}]: {e}")
            return GroundTruth(
                url=url,
                host=host,
                title="",
                visible_text_len=0,
                error=str(e),
            )
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass

    def probe_curl(
        self, url: str, cookies: Optional[dict] = None, ua: Optional[str] = None
    ) -> tuple[int, dict, str]:
        """步骤 B: 使用 curl_cffi 模拟请求"""
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if ua:
            headers["User-Agent"] = ua

        proxies = {"https": self.proxy, "http": self.proxy} if self.proxy else None
        try:
            resp = cf_requests.get(
                url,
                headers=headers,
                cookies=cookies or {},
                proxies=proxies,
                impersonate="chrome120",
                timeout=25,
                allow_redirects=True,
            )
            return resp.status_code, dict(resp.headers), resp.text
        except Exception as e:
            return 0, {}, f"CURL_ERROR: {e}"

    async def audit_domain(self, domain: str, sample_url: str) -> DomainAuditResult:
        """对单个独立域名执行完整的三态裁决"""
        logger.info(f"正在对域名执行模式体检: {domain} -> {sample_url}")

        # 1. 建立浏览器真值基准
        gt = await self.get_ground_truth(sample_url)
        if gt.error:
            # 浏览器自身都加载失败，强制降级并报警
            return DomainAuditResult(
                domain=domain,
                mode=RouteMode.BROWSER_ONLY,
                sample_url=sample_url,
                reason=f"浏览器建立真值基准失败 ({gt.error})，强制浏览器兜底",
                error=gt.error,
            )

        # 2. 先做裸 cURL 探测
        bare_status, bare_headers, bare_body = await asyncio.to_thread(
            self.probe_curl, sample_url
        )

        ok_bare, score_bare, reason_bare = DifferentialJudge.evaluate(
            gt, bare_status, bare_headers, bare_body
        )

        if ok_bare:
            logger.info(f"✅ [{domain}] 判定为 CURL_DIRECT (静态直出且内容真实)")
            return DomainAuditResult(
                domain=domain,
                mode=RouteMode.CURL_DIRECT,
                sample_url=sample_url,
                fidelity_score=score_bare,
                reason=reason_bare,
            )

        # 如果裸 cURL 提示是 SPA 空壳（状态码是 200 但没有真内容）
        if bare_status == 200 and not ok_bare:
            logger.info(f"⚠️ [{domain}] 判定为 BROWSER_ONLY (识破 SPA 客户端渲染假 200 空壳)")
            return DomainAuditResult(
                domain=domain,
                mode=RouteMode.BROWSER_ONLY,
                is_spa_shell=True,
                sample_url=sample_url,
                reason=f"SPA 空壳防御拦截: {reason_bare}",
            )

        # 3. 如果是遇到反爬质询 (403 / WAF)，测试 clearance 是否可复用 (cURL 池模式)
        # 注意: ScienceDirect / Cell 等顶级高防学术站具备企业级动态交互式 Turnstile 与动态 PDF 令牌，cURL 无法端到端下载
        if any(domain.endswith(d) for d in ("sciencedirect.com", "cell.com")):
            logger.info(f"🛡️ [{domain}] 强制判定为 BROWSER_ONLY (高防大站企业级动态行为风控/动态PDF签名)")
            return DomainAuditResult(
                domain=domain,
                mode=RouteMode.BROWSER_ONLY,
                sample_url=sample_url,
                reason="企业级动态行为风控与动态PDF令牌链，必须使用真实浏览器",
            )

        if gt.has_clearance:
            cookie_dict = {c["name"]: c["value"] for c in gt.cookies}
            pool_status, pool_headers, pool_body = await asyncio.to_thread(
                self.probe_curl, sample_url, cookie_dict, gt.user_agent
            )
            ok_pool, score_pool, reason_pool = DifferentialJudge.evaluate(
                gt, pool_status, pool_headers, pool_body
            )
            if ok_pool:
                logger.info(f"🎉 [{domain}] 判定为 CURL_POOL (通过 cf_clearance 身份池复用)")
                return DomainAuditResult(
                    domain=domain,
                    mode=RouteMode.CURL_POOL,
                    sample_url=sample_url,
                    fidelity_score=score_pool,
                    reason="cf_clearance 复用成功且内容真实",
                )

        # 4. 其余情况统统收拢为稳健的无头浏览器模式
        logger.info(f"🛡️ [{domain}] 判定为 BROWSER_ONLY ({reason_bare})")
        return DomainAuditResult(
            domain=domain,
            mode=RouteMode.BROWSER_ONLY,
            sample_url=sample_url,
            reason=f"WAF 限制或 clearance 无法复用 ({reason_bare})",
        )
