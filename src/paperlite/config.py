#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/config.py
─────────────────────
读 config.toml（模板见包内 config.example.toml，paperlite init 生成）。查找顺序：--config 参数 > 环境变量 PAPERLITE_CONFIG > 当前目录的 config.toml。

一份 config = 一个批次（[pg] batch）。各种主机的 URL 混着导进同一个 PostgreSQL 任务队列，一台或多台机器共享，
默认用浏览器模式统一跑。

  [pg]       任务队列：PostgreSQL 连接、批次名、按块领取参数（必填）
  [proxy]    代理提取接口与鉴权
  [limits]   并发与行为（浏览器数、过盾超时、请求间隔、熔断、巡检阈值……）
  [paths]    位置：storage（输出 PDF/HTML，可挂载）、log_dir
  [run]      paperlite run config.toml 时的启动参数
  [monitor]  监控上报与面板（可选，见 monitor/config.py）
"""

import os
import tomllib
from importlib import resources
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from paperlite.proxy import ProxySettings

CONFIG_ENV = "PAPERLITE_CONFIG"
CONFIG_NAME = "config.toml"


def resolve_path(explicit: str | None = None) -> Path:
    """--config 参数 > 环境变量 PAPERLITE_CONFIG > 当前目录的 config.toml。"""
    return Path(explicit or os.environ.get(CONFIG_ENV) or CONFIG_NAME).expanduser()


def write_example(path: Path, force: bool = False) -> None:
    """把包内的配置模板写到 path（含代理密码的文件，权限 600）。已存在且没给 force 就报错。"""
    if path.exists() and not force:
        raise FileExistsError(f"{path} 已存在（覆盖用 --force）")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(resources.files("paperlite").joinpath("config.example.toml").read_text(encoding="utf-8"),
                    encoding="utf-8")
    path.chmod(0o600)


@dataclass(frozen=True)
class Paths:
    """位置。任务在 PostgreSQL（[pg]），这里只有输出与日志。"""

    storage: str = "output"           # 输出目录（PDF/HTML），可在 goofys/s3fs 等挂载上
    log_dir: str = "log"              # 日志目录；非空时同时写终端和按启动时间命名的 .jsonl；空则只打终端

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Paths":
        if "db" in raw:
            raise ValueError("[paths] db 已移除：任务队列改用 PostgreSQL，请删掉 db 并配置 [pg] 段（dsn、batch）")
        _reject_unknown("paths", raw, cls)
        return cls(**raw)

    @property
    def storage_path(self) -> Path:
        return Path(self.storage).expanduser()


@dataclass(frozen=True)
class Limits:
    """并发与行为。不含任何存储路径（那些在 [paths]）。"""

    redis_url: str = "redis://127.0.0.1:6379/0"
    key_prefix: str = "paperlite"
    # 生产
    max_browsers: int = 20            # 同时运行的浏览器上限
    max_identities: int = 20          # 健康身份（IP+cookie）上限；实际目标 = min(本值, 在线下载线程数)
    min_identities: int = 0           # 已废弃、不再生效（生产完全由下载线程驱动）；保留只为兼容旧配置，非 0 时打警告
    refill_before_s: float = 90       # 身份剩余寿命低于此值即不算健康，提前生产替补
    pass_timeout_s: float = 90        # 单次过盾等待上限
    hedge_after_s: float = 45         # 过盾超过此时长未成功即另开一个补位（成功的多在 30s 内）
    headed: bool = False
    webrtc_spoof: bool = True         # 给浏览器加 --fingerprint-webrtc-ip=auto：把 WebRTC 出口伪造成代理出口 IP，防泄露真实 IP。不需要 GeoIP 库
    geoip: bool = True                # 按出口 IP 对齐浏览器时区/语言（重要的指纹一致性）；需 ~70MB GeoIP 库，首次启动会限时下载，连不上则报错给手动预置指引
    debug_dir: str = ""               # 过盾失败截图目录，空则不截图
    max_fresh_rejects: int = 5        # 连续这么多个新身份一篇没下成就被拒 → 熔断暂停生产
    breaker_pause_s: float = 600      # 熔断暂停时长；之后放一个身份试探
    # 消费
    min_left_s: float = 20            # 身份剩余寿命低于此值不再发请求
    lease_ttl_s: float = 120          # 下载线程占用身份的心跳 TTL；进程崩溃后到期自动归还
    request_gap_s: tuple[float, float] = (3.0, 5.0)   # 同一 IP 两次请求的随机间隔
    page_interval_s: tuple[float, float] = (10.0, 30.0)   # 浏览器模式：同一浏览器两篇之间的随机间隔
    connect_timeout_s: float = 15
    download_timeout_s: float = 120   # 流式下载的卡顿超时：连续这么久收不到数据才算超时（不是总时长上限）
    min_file_size: int = 1024         # PDF 小于此字节数视为不完整，计一次重试
    max_file_size_mb: int = 200       # 超过就中止，判 failed / extract_failed
    max_tries: int = 3                # 单条 URL 的网络错误重试次数（cookie 被拒不计）
    max_url_challenges: int = 3       # 单条 URL 带有效 cookie 仍被挑战满这么多次 → failed / challenge_failed
    # 巡检：同标题重复到阈值就归纳 doi/url 规则，把疑似失效/封面的 pending 标记跳过（见 inspect.py）
    title_dup_trigger: int = 10       # 同一标题重复这么多篇就触发巡检；0 关闭巡检
    title_window_size: int = 500      # 实时巡检只保留本次运行最近这么多条已加载页面
    rule_min_precision: float = 0.98  # 归纳出的规则在已采数据上的最低精确率，防止把真文章误标
    rule_min_support: int = 10        # 规则在已采数据上的最低命中数

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Limits":
        if "volume_stop_after" in raw:
            raise ValueError("[limits] volume_stop_after 已移除（卷期停止只适用 ACM 式 DOI，不通用），删掉这一行即可")
        _reject_unknown("limits", raw, cls)
        kwargs = dict(raw)
        for key in ("request_gap_s", "page_interval_s"):
            if key in kwargs:
                lo, hi = (float(x) for x in kwargs[key])
                if not 0 <= lo <= hi:
                    raise ValueError(f"{key} 必须是 [最小, 最大] 且 0 ≤ 最小 ≤ 最大")
                kwargs[key] = (lo, hi)
        trigger = int(kwargs.get("title_dup_trigger", cls.title_dup_trigger))
        window = int(kwargs.get("title_window_size", cls.title_window_size))
        if trigger < 0:
            raise ValueError("title_dup_trigger 不能小于 0")
        if trigger and window < trigger:
            raise ValueError("title_window_size 不能小于 title_dup_trigger")
        return cls(**kwargs)


@dataclass(frozen=True)
class RunConfig:
    """paperlite run config.toml（或裸 paperlite）启动时采用的参数。command 为空则裸命令只显示帮助。"""

    command: str = ""
    mode: str = "browser"             # 默认浏览器模式（可混多主机）；curl 为单主机 cookie 池
    proxy: bool = True
    workers: int = 20
    urls: str = ""
    url_type: str = "pdf"
    headed: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RunConfig":
        if "pg" in raw:
            raise ValueError("[run] pg 已移除：任务队列只用 PostgreSQL（[pg] 段），删掉这一行即可")
        _reject_unknown("run", raw, cls)
        cfg = cls(**raw)
        if cfg.command not in ("", "run"):
            raise ValueError("[run] command 目前只支持 run")
        if cfg.mode not in ("browser", "curl"):
            raise ValueError("[run] mode 必须是 browser 或 curl")
        if cfg.workers < 1:
            raise ValueError("[run] workers 必须大于 0")
        if cfg.url_type not in ("pdf", "html"):
            raise ValueError("[run] url_type 必须是 pdf 或 html")
        if not cfg.proxy and cfg.mode != "browser":
            raise ValueError("[run] proxy = false 只支持 browser 模式")
        return cfg


@dataclass(frozen=True)
class PgConfig:
    """[pg]：任务队列（paperlite.pgqueue）。同一 batch 的机器共享一个批次。"""

    dsn: str = ""                     # postgresql://user:pass@host:5432/db（含密码，config.toml 不要提交）
    batch: str = ""                   # 批次名：同一批次的机器填同一个
    schema: str = ""                  # 可选：表所在 schema，空 = 默认 search_path
    chunk_size: int = 1000            # 每块任务数
    lease_s: float = 1800             # 块租约秒数，后台每 lease_s/6 续一次
    flush_n: int = 500                # 结果攒够这么多条回写一次
    flush_s: float = 5.0              # 或者攒了这么久回写一次
    retry_delay_s: float = 0.0        # 要重试的任务至少等这么久再进下一代块

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PgConfig":
        _reject_unknown("pg", raw, cls)
        cfg = cls(**raw)
        if not cfg.dsn or not cfg.batch:
            raise ValueError("[pg] dsn 和 batch 必填")
        if cfg.chunk_size < 1 or cfg.lease_s <= 0 or cfg.flush_n < 1 or cfg.flush_s <= 0:
            raise ValueError("[pg] chunk_size / lease_s / flush_n / flush_s 必须大于 0")
        return cfg


def proxy_settings(raw: dict[str, Any]) -> ProxySettings:
    _reject_unknown("proxy", raw, ProxySettings)
    if not raw.get("api_url"):
        raise ValueError("[proxy] api_url 必填：每个身份要一个独立出口 IP")
    return ProxySettings(**raw)


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path.resolve()}（先执行 paperlite init 生成；"
                                f"或用 --config / 环境变量 {CONFIG_ENV} 指定）")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    if "landing" in raw:
        raise ValueError("[landing] 已移除：不再用固定落地页热身，浏览器直接拿队列里的真实链接过盾，删掉 [landing] 即可")
    if "queue" in raw:
        raise ValueError("[queue] 已移除：任务队列只用 PostgreSQL（[pg] 段），删掉 [queue] 即可")
    _reject_unknown("顶层", raw, None, allowed={"proxy", "limits", "paths", "run", "monitor", "pg"})   # [monitor] 见 monitor/config.py
    return raw


def load_limits(path: Path | None = None) -> Limits:
    """只读 [limits]，不校验代理。"""
    raw = _read(path or resolve_path())
    return Limits.from_dict(raw.get("limits", {}))


def load_paths(path: Path | None = None) -> Paths:
    """只读 [paths]（db / storage / log_dir）。import / status / retry / export 只需要这个。"""
    raw = _read(path or resolve_path())
    return Paths.from_dict(raw.get("paths", {}))


def load(path: Path | None = None) -> tuple[Limits, ProxySettings]:
    """读 [limits] + [proxy]（校验代理）。run / produce / download 用。"""
    raw = _read(path or resolve_path())
    return (Limits.from_dict(raw.get("limits", {})),
            proxy_settings(raw.get("proxy", {})))


def load_run(path: Path | None = None) -> RunConfig:
    """读 [run]。"""
    raw = _read(path or resolve_path())
    return RunConfig.from_dict(raw.get("run", {}))


def load_pg(path: Path | None = None) -> PgConfig:
    """读 [pg]（任务队列）；没有这一段报错。"""
    raw = _read(path or resolve_path())
    if "pg" not in raw:
        raise ValueError("配置文件里没有 [pg] 段：任务队列在 PostgreSQL，至少要填 dsn 和 batch（见 paperlite init 生成的模板）")
    return PgConfig.from_dict(raw["pg"])


def _reject_unknown(section: str, raw: dict, cls, allowed: set[str] | None = None) -> None:
    known = allowed if allowed is not None else {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"[{section}] 有不支持的配置项: {', '.join(sorted(unknown))}")
