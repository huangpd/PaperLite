#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite.preprocess
────────────────────
PaperLite 预处理与态势感知引擎。
提供 DOI 流式聚类、多通道落地域名嗅探、双视角语义保真度体检、以及不可变路由快照导出。
"""

from paperlite.preprocess.auditor import DifferentialJudge, DomainAuditor
from paperlite.preprocess.cluster import PatternExtractor, StreamClusterer
from paperlite.preprocess.manager import PreprocessManager
from paperlite.preprocess.models import (
    DoiSample,
    DomainAuditResult,
    GroundTruth,
    ResolvedTarget,
    RouteMode,
)
from paperlite.preprocess.resolver import DomainResolver
from paperlite.preprocess.store import PreprocessStore

__all__ = [
    "PreprocessManager",
    "StreamClusterer",
    "PatternExtractor",
    "DomainResolver",
    "DomainAuditor",
    "DifferentialJudge",
    "PreprocessStore",
    "RouteMode",
    "DoiSample",
    "ResolvedTarget",
    "GroundTruth",
    "DomainAuditResult",
]
