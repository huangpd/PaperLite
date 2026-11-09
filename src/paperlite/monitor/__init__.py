"""
paperlite/monitor
───────────────────
独立观测模块：每个 PaperLite 终端把运行指标发到一个公共 RabbitMQ，监控面板（paperlite-monitor serve）
消费入库（SQLite，保留 30 天）并出大屏（最近 1 / 7 / 30 天）。

  上报端（下载进程里）
    config.py    [monitor] 段配置
    schema.py    消息格式（上报端与面板端共用）
    reporter.py  埋点 API：page / browser_start / proxy_check / heartbeat；未启用时全部 no-op、不导入 pika
  面板端（paperlite-monitor）
    db.py        消息入库 + 按时间窗聚合查询
    consumer.py  从 RabbitMQ 消费
    dashboard.py HTTP 大屏 + JSON 接口
    cli.py       paperlite-monitor 命令

业务代码只依赖 reporter 的几个函数；监控挂了（RabbitMQ 连不上、pika 没装）绝不影响下载。
"""
