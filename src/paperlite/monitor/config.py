"""[monitor] 段：上报端（下载进程）与面板端（paperlite-monitor serve）共用一份配置结构。
两端都只和 PostgreSQL 交互：上报端按批写入监控 schema 的 events 中转表，面板端从中取走、汇总入库。"""

import dataclasses
import os
import socket
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MonitorConfig:
    # ── 上报端 ──
    enabled: bool = False                 # true 才上报；false / 没有 [monitor] 段时埋点全部 no-op
    node: str = ""                        # 终端名；空则用主机名。同机多进程靠 pid 区分（见 instance）
    vendor: str = ""                      # 代理厂商标签（如 qingguo），面板按厂商×域名聚合；空则记 unknown
    heartbeat_s: float = 15               # 心跳间隔：在线浏览器数、待下载数等实时量
    buffer_size: int = 10000              # 本地发送缓冲；PG 不通时超出就丢最旧的，绝不阻塞下载
    flush_s: float = 2.0                  # 攒这么久按批写一次（一次 INSERT 多条），不逐条写库
    forward_logs: bool = True             # 把 WARNING 及以上日志也作为实时事件上报
    # ── 两端共用 ──
    dsn: str = ""                         # 监控用的 PostgreSQL；空 = 用 [pg] 的 dsn（与任务队列同一个库）
    schema: str = "paperlite_monitor"     # 监控表所在 schema，与任务表分开
    # ── 面板端 ──
    backlog_max: int = 2_000_000          # 中转表积压上限：面板长期不开时超出的丢最旧的，防表无限长
    bind: str = "127.0.0.1"
    port: int = 8700
    username: str = "admin"               # 面板登录用户名（HTTP Basic，浏览器弹框输入）
    password: str = ""                    # 面板登录密码；非空即开启登录。监听非本机地址时必须设（或设 token）
    token: str = ""                       # 可选：给脚本/监控系统用的只读令牌（?token=… 或请求头 X-Token）
    retention_days: int = 30              # 小时汇总（大屏 1/7/30 天）保留天数
    raw_retention_hours: int = 6          # 原始事件（实时事件流、近 5 分钟频率、排查明细）保留小时数；大屏只读小时汇总

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MonitorConfig":
        if "db" in raw:
            raise ValueError("[monitor] db 已移除：面板改存 PostgreSQL（[monitor] dsn，留空则用 [pg] 的 dsn），删掉 db 即可")
        old = sorted(set(raw) & {"amqp_url", "exchange", "queue", "queue_max_length"})
        if old:
            raise ValueError(f"[monitor] {', '.join(old)} 已移除：监控的上报和面板都直接连 PostgreSQL"
                             "（[monitor] dsn，留空则用 [pg] 的 dsn），删掉这几行即可")
        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"[monitor] 有不支持的配置项: {', '.join(sorted(unknown))}")
        cfg = cls(**raw)
        if cfg.heartbeat_s <= 0:
            raise ValueError("[monitor] heartbeat_s 必须大于 0")
        if cfg.retention_days < 1:
            raise ValueError("[monitor] retention_days 至少 1")
        if cfg.raw_retention_hours < 2:
            raise ValueError("[monitor] raw_retention_hours 至少 2（实时事件流看最近 1 小时）")
        if cfg.flush_s <= 0:
            raise ValueError("[monitor] flush_s 必须大于 0")
        return cfg

    @property
    def auth_enabled(self) -> bool:
        return bool(self.password or self.token)

    @property
    def loopback_only(self) -> bool:
        return self.bind in ("127.0.0.1", "localhost", "::1")

    @property
    def node_name(self) -> str:
        return self.node or socket.gethostname()

    @property
    def instance(self) -> str:
        """一个运行中的进程：node:pid。面板按它判断"在线"（最近有心跳）。"""
        return f"{self.node_name}:{os.getpid()}"

    @property
    def vendor_name(self) -> str:
        return self.vendor or "unknown"



def load_monitor(path: Path) -> MonitorConfig:
    """读 config.toml 的 [monitor]；文件不存在或没有该段 → 未启用的默认配置。"""
    from paperlite import config as config_mod
    try:
        raw = config_mod._read(path)
    except FileNotFoundError:
        return MonitorConfig()
    cfg = MonitorConfig.from_dict(raw.get("monitor", {}))
    if not cfg.dsn and isinstance(raw.get("pg"), dict) and raw["pg"].get("dsn"):
        cfg = dataclasses.replace(cfg, dsn=raw["pg"]["dsn"])
    if cfg.enabled and not cfg.dsn:
        raise ValueError("[monitor] enabled = true 时要有 PostgreSQL：填 [monitor] dsn，或 [pg] dsn")
    return cfg
