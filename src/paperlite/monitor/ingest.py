"""
面板端消费：从 events 中转表按批取走事件、汇总入库（MonitorDB.consume，取、写、删同一个事务）。
面板停机期间事件留在表里（上限 backlog_max，超出丢最旧的），重启后补齐。
"""

import logging
import threading

from paperlite.monitor.config import MonitorConfig
from paperlite.monitor.db import MonitorDB

logger = logging.getLogger(__name__)

_BATCH = 2000
_IDLE_S = 1.0          # 没有新事件时隔这么久再看


def consume_forever(cfg: MonitorConfig, db: MonitorDB, stop: threading.Event) -> None:
    logger.info("开始消费监控事件（PostgreSQL schema %s 的 events 表）", cfg.schema)
    backoff = 1.0
    while not stop.is_set():
        try:
            n = db.consume(_BATCH)
            backoff = 1.0
            if n < _BATCH:                 # 没积压了：歇一下，顺手做到期清理（每小时一次）
                db.prune()
                stop.wait(_IDLE_S)
        except Exception as e:
            logger.warning("消费监控事件出错，%.0fs 后重试: %s", backoff,
                           str(e).splitlines()[0][:160] if str(e) else type(e).__name__)
            stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
            try:
                db.reconnect_if_broken()
            except Exception:
                pass                       # 还连不上，下一轮再试
