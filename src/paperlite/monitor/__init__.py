"""
paperlite/monitor
───────────────────
独立观测模块（可选）：每个 PaperLite 终端把运行指标按批写进 PostgreSQL（监控 schema 的 events 中转表），
监控面板（paperlite-monitor serve）从中取走、汇总入库（保留 30 天），出大屏（最近 1 / 7 / 30 天）。
两端都只和 PG 交互，不需要别的中间件；监控表放在自己的 schema 里，与任务表分开。

  上报端（下载进程里）
    config.py    [monitor] 段配置
    schema.py    消息格式（上报端与面板端共用）
    events.py    events 中转表：建表、按批写入
    reporter.py  埋点 API：page / browser_start / proxy_check / heartbeat；未启用时全部 no-op、不连库
  面板端（paperlite-monitor）
    db.py        取走事件、汇总入库 + 按时间窗聚合查询
    consumer.py  消费循环
    dashboard.py HTTP 大屏 + JSON 接口
    cli.py       paperlite-monitor 命令

业务代码只依赖 reporter 的几个函数；监控挂了（PG 连不上、写失败）绝不影响下载。
"""
