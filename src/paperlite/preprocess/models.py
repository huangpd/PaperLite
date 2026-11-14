#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite.preprocess.models
───────────────────────────
预处理模块的基础领域数据模型与枚举定义。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RouteMode(str, Enum):
    """站点抓取模式判定结果"""
    CURL_DIRECT = "curl_direct"     # 纯 cURL 直通模式（无盾、页面完全静态直出）
    CURL_POOL = "curl_pool"         # cURL 身份池模式（有盾，但 cf_clearance 可被 cURL 复用）
    BROWSER_ONLY = "browser_only"   # 强制无头浏览器模式（动态行为 WAF、不发静态 clearance、或 SPA 渲染假空壳）


@dataclass
class DoiSample:
    """从输入源抽样出的代表性种子 DOI"""
    doi: str
    pattern_key: str              # 族群特征签名，如 '10.1016/j.ajog'
    publisher_doi: str            # 出版商大类代码，如 '10.1016'
    raw_url: Optional[str] = None # 如果输入就是完整 URL


@dataclass
class ResolvedTarget:
    """真实跳转嗅探结果"""
    pattern_key: str
    sample_doi: str
    real_host: str                # 真实落地的主机名，如 'www.sciencedirect.com'
    final_url: str                # 真实落地的终点 URL
    hops: int = 1                 # 经历的跳转跳数
    has_meta_refresh: bool = False
    error: Optional[str] = None


@dataclass
class GroundTruth:
    """浏览器端建立的页面真值基准"""
    url: str
    host: str
    title: str                    # 完整渲染出的真实标题
    visible_text_len: int         # 可见文本字符数
    abstract_anchor: str = ""     # 核心摘要前 50 字锚点
    has_clearance: bool = False   # 浏览器是否获取到了 cf_clearance
    cookies: list[dict] = field(default_factory=list)
    user_agent: str = ""
    error: Optional[str] = None


@dataclass
class DomainAuditResult:
    """独立站点的反爬体检与模式裁决结果"""
    domain: str
    mode: RouteMode
    waf_type: str = "none"        # cloudflare / aws_waf / akamai / none 等
    is_spa_shell: bool = False    # 是否为 SPA 客户端渲染假空壳
    fidelity_score: float = 1.0   # 语义保真度评分 (0.0 ~ 1.0)
    sample_url: str = ""
    reason: str = ""
    error: Optional[str] = None
