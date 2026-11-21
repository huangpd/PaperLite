"""
paperlite/taskq
───────────────
任务队列（PostgreSQL，按块领取）。两种下载模式共用。

  queue.py      PgTaskQueue：全部 SQL —— 建表、导入、切块、领块 / 续租 / 回写、重试、导出、规则
  source.py     PgTaskSource + LeaseKeeper：下载 worker 这一侧（本地队列、攒批回写、续租）
  title_dup.py  巡检：块内推断同标题重复（浏览器模式下载时、paperlite inspect 时用）

外部一般只用这里导出的名字。
"""

from paperlite.taskq.queue import (DEFAULT_CHUNK_SIZE, DEFAULT_LEASE_S, RETRY, SKIP, SKIP_REASONS, Chunk,
                                   PgTaskQueue, QueueStats, Result)
from paperlite.taskq.source import LeaseKeeper, PgTaskSource

__all__ = ["DEFAULT_CHUNK_SIZE", "DEFAULT_LEASE_S", "RETRY", "SKIP", "SKIP_REASONS", "Chunk", "PgTaskQueue",
           "QueueStats", "Result", "LeaseKeeper", "PgTaskSource"]
