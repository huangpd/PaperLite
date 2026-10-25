#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/config.py
─────────────────────
读 config.toml（见 config.example.toml）。查找顺序：--config 参数 > 环境变量 PAPERLITE_CONFIG > 当前目录的 config.toml。
  [proxy]    代理提取接口与鉴权
  [pool]     生产 / 消费参数
  [landing]  主机 → 过盾落地页
"""

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from paperlite.proxy import ProxySettings

CONFIG_ENV = "PAPERLITE_CONFIG"
CONFIG_NAME = "config.toml"


def resolve_path(explicit: str | None = None) -> Path:
    """--config 参数 > 环境变量 PAPERLITE_CONFIG > 当前目录的 config.toml。"""
    return Path(explicit or os.environ.get(CONFIG_ENV) or CONFIG_NAME).expanduser()


@dataclass(frozen=True)
class PoolConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    key_prefix: str = "paperlite"
    # 生产
    max_browsers: int = 20            # 同时运行的浏览器上限
    max_identities: int = 20          # 健康身份（IP+cookie）上限；实际目标 = min(本值, 在线下载线程数)
    min_identities: int = 0           # 没有下载线程时也保持的预热身份数
    refill_before_s: float = 90       # 身份剩余寿命低于此值即不算健康，提前生产替补
    pass_timeout_s: float = 90        # 单次过盾等待上限
    hedge_after_s: float = 45         # 过盾超过此时长未成功即另开一个补位（成功的多在 30s 内）
    headed: bool = False
    debug_dir: str = ""               # 过盾失败截图目录，空则不截图
    max_fresh_rejects: int = 5        # 连续这么多个新身份一篇没下成就被拒 → 熔断暂停生产
    breaker_pause_s: float = 600      # 熔断暂停时长；之后放一个身份试探
    landing: dict[str, str] = field(default_factory=dict)   # 主机 → 过盾落地页
    # 消费
    min_left_s: float = 20            # 身份剩余寿命低于此值不再发请求
    lease_ttl_s: float = 120          # 下载线程占用身份的心跳 TTL；进程崩溃后到期自动归还
    request_gap_s: tuple[float, float] = (3.0, 5.0)   # 同一 IP 两次请求的随机间隔
    download_timeout_s: float = 120
    max_tries: int = 3                # 单条 URL 的网络错误重试次数（cookie 被拒不计）
    max_url_challenges: int = 3       # 单条 URL 带有效 cookie 仍被挑战满这么多次 → failed / challenge_failed
    volume_stop_after: int = 3        # 同一卷期还没下到 PDF、已有这么多篇 404/订阅/摘要页 → 停掉该卷期其余任务；0 关闭

    def landing_for(self, host: str) -> str:
        return self.landing.get(host) or f"https://{host}/"

    @classmethod
    def from_dict(cls, raw: dict[str, Any], landing: dict[str, str] | None = None) -> "PoolConfig":
        _reject_unknown("pool", raw, cls)
        kwargs = dict(raw)
        if "request_gap_s" in kwargs:
            lo, hi = (float(x) for x in kwargs["request_gap_s"])
            if not 0 <= lo <= hi:
                raise ValueError("request_gap_s 必须是 [最小, 最大] 且 0 ≤ 最小 ≤ 最大")
            kwargs["request_gap_s"] = (lo, hi)
        kwargs["landing"] = {str(k).lower(): str(v) for k, v in (landing or {}).items()}
        return cls(**kwargs)


def proxy_settings(raw: dict[str, Any]) -> ProxySettings:
    _reject_unknown("proxy", raw, ProxySettings)
    if not raw.get("api_url"):
        raise ValueError("[proxy] api_url 必填：每个身份要一个独立出口 IP")
    return ProxySettings(**raw)


def load(path: Path | None = None) -> tuple[PoolConfig, ProxySettings]:
    path = path or resolve_path()
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path.resolve()}（从 config.example.toml 复制；"
                                f"或用 --config / 环境变量 {CONFIG_ENV} 指定）")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    _reject_unknown("顶层", raw, None, allowed={"proxy", "pool", "landing"})
    return (PoolConfig.from_dict(raw.get("pool", {}), raw.get("landing", {})),
            proxy_settings(raw.get("proxy", {})))


def _reject_unknown(section: str, raw: dict, cls, allowed: set[str] | None = None) -> None:
    known = allowed if allowed is not None else {f.name for f in fields(cls)} - {"landing"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"[{section}] 有不支持的配置项: {', '.join(sorted(unknown))}")
