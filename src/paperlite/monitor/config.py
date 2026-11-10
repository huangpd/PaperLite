"""[monitor] 段：上报端（下载进程）与面板端（paperlite-monitor serve）共用一份配置结构。"""

import os
import socket
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MonitorConfig:
    # ── 上报端 ──
    enabled: bool = False                 # true 才上报；false / 没有 [monitor] 段时埋点全部 no-op
    amqp_url: str = "amqp://guest:guest@127.0.0.1:5672/%2F"   # 公共 RabbitMQ（含密码，只在本地配置里）
    exchange: str = "paperlite.monitor"   # topic exchange，routing key = 事件类型
    node: str = ""                        # 终端名；空则用主机名。同机多进程靠 pid 区分（见 instance）
    vendor: str = ""                      # 代理厂商标签（如 qingguo），面板按厂商×域名聚合；空则记 unknown
    heartbeat_s: float = 15               # 心跳间隔：在线浏览器数、待下载数等实时量
    buffer_size: int = 10000              # 本地发送缓冲；RabbitMQ 不通时超出就丢最旧的，绝不阻塞下载
    forward_logs: bool = True             # 把 WARNING 及以上日志也作为实时事件上报
    # ── 面板端 ──
    queue: str = "paperlite.monitor.dashboard"   # 面板的持久队列：面板停机期间消息在 RabbitMQ 里排队
    queue_max_length: int = 2_000_000     # 队列上限，超出丢最旧的，防面板长期离线撑爆 RabbitMQ
    db: str = "monitor.db"                # 面板入库的 SQLite（本地盘）
    bind: str = "127.0.0.1"
    port: int = 8700
    username: str = "admin"               # 面板登录用户名（HTTP Basic，浏览器弹框输入）
    password: str = ""                    # 面板登录密码；非空即开启登录。监听非本机地址时必须设（或设 token）
    token: str = ""                       # 可选：给脚本/监控系统用的只读令牌（?token=… 或请求头 X-Token）
    retention_days: int = 30              # 小时汇总（大屏 1/7/30 天）保留天数
    raw_retention_hours: int = 48         # 原始事件（实时事件流、明细）保留小时数；量大时调小

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MonitorConfig":
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
        if cfg.enabled and not cfg.amqp_url:
            raise ValueError("[monitor] enabled = true 时 amqp_url 必填")
        return cfg

    def amqp_params(self):
        """pika 连接参数。"""
        import pika
        return pika.URLParameters(self.amqp_url)

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

    @property
    def db_path(self) -> Path:
        return Path(self.db).expanduser()


def load_monitor(path: Path) -> MonitorConfig:
    """读 config.toml 的 [monitor]；文件不存在或没有该段 → 未启用的默认配置。"""
    from paperlite import config as config_mod
    try:
        raw = config_mod._read(path)
    except FileNotFoundError:
        return MonitorConfig()
    return MonitorConfig.from_dict(raw.get("monitor", {}))
