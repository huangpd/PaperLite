"""
面板端消费：声明 topic exchange + 面板自己的持久队列（绑定 #），批量入库后一次性 ack。
面板停机期间消息留在队列里（上限 queue_max_length，超出丢最旧），重启后补齐。
"""

import logging
import threading

from paperlite.monitor import schema
from paperlite.monitor.config import MonitorConfig
from paperlite.monitor.db import MonitorDB

logger = logging.getLogger(__name__)

_BATCH = 500


def declare(ch, cfg: MonitorConfig) -> None:
    ch.exchange_declare(cfg.exchange, exchange_type="topic", durable=True)
    ch.queue_declare(cfg.queue, durable=True,
                     arguments={"x-max-length": cfg.queue_max_length, "x-overflow": "drop-head"})
    ch.queue_bind(cfg.queue, cfg.exchange, routing_key="#")


def consume_forever(cfg: MonitorConfig, db: MonitorDB, stop: threading.Event) -> None:
    import pika

    backoff = 1.0
    while not stop.is_set():
        conn = None
        try:
            params = pika.URLParameters(cfg.amqp_url)
            params.heartbeat = 60
            conn = pika.BlockingConnection(params)
            ch = conn.channel()
            declare(ch, cfg)
            ch.basic_qos(prefetch_count=_BATCH * 2)
            logger.info("已连接 RabbitMQ，消费队列 %s", cfg.queue)
            backoff = 1.0
            pending_tag, n = None, 0
            for method, _props, body in ch.consume(cfg.queue, inactivity_timeout=1.0):
                if method is not None:
                    try:
                        db.ingest([schema.decode(body)], commit=False)
                    except Exception as e:
                        logger.debug("丢弃无法解析的监控消息: %s", e)
                    pending_tag, n = method.delivery_tag, n + 1
                if pending_tag is not None and (n >= _BATCH or method is None):
                    db.commit()                                   # 先落盘再 ack：崩溃最多重放，不丢
                    ch.basic_ack(pending_tag, multiple=True)
                    pending_tag, n = None, 0
                if method is None:
                    db.prune()
                if stop.is_set():
                    break
            ch.cancel()
        except Exception as e:
            logger.warning("RabbitMQ 消费中断，%.0fs 后重连: %s", backoff,
                           str(e).splitlines()[0][:160] if str(e) else type(e).__name__)
            stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
