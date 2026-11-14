#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite.preprocess.resolver
─────────────────────────────
基于代理 IP 网络的轻量真实落地域名嗅探器。
通过遵循 HTTP 302 重定向与 Meta-Refresh 解析，快速解包 DOI 真实落地 Host。
严格限速，防止冲击目标站。
"""

import asyncio
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import unquote, urljoin, urlparse

from curl_cffi.requests import AsyncSession

from paperlite.preprocess.models import DoiSample, ResolvedTarget

logger = logging.getLogger("paperlite.preprocess.resolver")

# 匹配 HTML 中的 meta http-equiv="refresh" 中的跳转目标 (如 Elsevier linkinghub)
_META_REFRESH_REDIRECT_RE = re.compile(
    r"""Redirect=([^\s\"\'&>]+)""", re.IGNORECASE
)
_META_REFRESH_URL_RE = re.compile(
    r"""<meta\s+[^>]*http-equiv=["']?refresh["']?[^>]*content=["']?[0-9.]*\s*;\s*url=['"]?([^"'>\s]+)""",
    re.IGNORECASE,
)


class DomainResolver:
    """基于代理 IP 网络的域名嗅探器"""

    def __init__(
        self,
        proxy: Optional[str] = None,
        concurrency: int = 6,
        timeout_s: float = 20.0,
        impersonate: str = "chrome120",
    ):
        self.proxy = proxy
        self.concurrency = concurrency
        self.timeout_s = timeout_s
        self.impersonate = impersonate

    async def resolve_sample(
        self, sample: DoiSample, session: AsyncSession, sem: asyncio.Semaphore
    ) -> ResolvedTarget:
        """解析单条代表样本的真实落地域名"""
        target_url = sample.raw_url or f"https://doi.org/{sample.doi}"
        async with sem:
            proxies = {"https": self.proxy, "http": self.proxy} if self.proxy else None
            try:
                # 步骤 1: 发起初始跟随重定向请求
                resp = await session.get(
                    target_url,
                    proxies=proxies,
                    timeout=self.timeout_s,
                    allow_redirects=True,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                )

                curr_url = str(resp.url)
                curr_host = urlparse(curr_url).netloc.lower()

                # 步骤 2: 检查是否落入 Elsevier linkinghub 等中间页并包含 Meta-Refresh
                if "linkinghub.elsevier.com" in curr_host or "redirect=" in resp.text.lower():
                    m_red = _META_REFRESH_REDIRECT_RE.search(resp.text)
                    if m_red:
                        final_real_url = unquote(m_red.group(1))
                        final_host = urlparse(final_real_url).netloc.lower()
                        return ResolvedTarget(
                            pattern_key=sample.pattern_key,
                            sample_doi=sample.doi,
                            real_host=final_host,
                            final_url=final_real_url,
                            has_meta_refresh=True,
                        )

                # 检查标准 HTML <meta http-equiv="refresh" content="...; url=..."> 跳转
                m_meta = _META_REFRESH_URL_RE.search(resp.text)
                if m_meta:
                    raw_target = unquote(m_meta.group(1).strip().strip("'\""))
                    final_real_url = urljoin(curr_url, raw_target)
                    final_host = urlparse(final_real_url).netloc.lower()
                    if final_host:
                        return ResolvedTarget(
                            pattern_key=sample.pattern_key,
                            sample_doi=sample.doi,
                            real_host=final_host,
                            final_url=final_real_url,
                            has_meta_refresh=True,
                        )

                # 正常跟随 302 后的最终页面
                return ResolvedTarget(
                    pattern_key=sample.pattern_key,
                    sample_doi=sample.doi,
                    real_host=curr_host,
                    final_url=curr_url,
                    has_meta_refresh=False,
                )

            except Exception as e:
                logger.warning(f"解析样本失败 [{sample.pattern_key}] ({sample.doi}): {e}")
                return ResolvedTarget(
                    pattern_key=sample.pattern_key,
                    sample_doi=sample.doi,
                    real_host="error",
                    final_url="",
                    error=str(e),
                )

    async def resolve_all(
        self, samples: List[DoiSample]
    ) -> List[ResolvedTarget]:
        """批量温和并发解析所有代表样本"""
        total = len(samples)
        logger.info(f"开始批量嗅探 {total} 个族群样本的落地域名 (并发: {self.concurrency}, 代理: {self.proxy})")
        sem = asyncio.Semaphore(self.concurrency)
        completed = 0

        async with AsyncSession(impersonate=self.impersonate) as session:
            async def _worker(s: DoiSample):
                nonlocal completed
                res = await self.resolve_sample(s, session, sem)
                completed += 1
                if completed % 200 == 0 or completed == total:
                    logger.info(f"嗅探进度: {completed} / {total} ({completed / total * 100:.1f}%)")
                return res

            tasks = [_worker(s) for s in samples]
            results = await asyncio.gather(*tasks)

        success_count = sum(1 for r in results if r.real_host != "error")
        logger.info(f"落地域名嗅探完成: 成功 {success_count} / {len(samples)}")
        return results
