"""
上报端：业务代码里的埋点 API。

  from paperlite.monitor import reporter as mon
  mon.page(host=..., passed=True, ok=True, status=200, waf="cloudflare", elapsed_ms=1234)

未调用 start()（没配 [monitor] 或 enabled=false）时，所有函数立即返回：不建线程、不导入 pika。
启用后事件进本地有界队列，由后台线程发往 RabbitMQ；连不上就按退避重连，队列满了丢最旧的——
监控永远不能拖慢或打断下载。
"""

import collections
import logging
import threading
import time
from typing import Any, Callable, Optional

from paperlite.monitor import schema
from paperlite.monitor.config import MonitorConfig

logger = logging.getLogger(__name__)

_RECONNECT_MAX_S = 30.0


class Reporter:
    def __init__(self, cfg: Optional[MonitorConfig] = None, batch: str = "") -> None:
        self.cfg = cfg or MonitorConfig()
        self.batch = batch
        self.enabled = False
        self._buf: collections.deque = collections.deque(maxlen=max(1, self.cfg.buffer_size))
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._log_handler: Optional[logging.Handler] = None
        self.sent = 0
        self.dropped = 0

    # ── 生命周期 ──
    def start(self) -> bool:
        """启用上报；pika 未安装则告警并保持 no-op。返回是否真正启用。"""
        if not self.cfg.enabled:
            return False
        try:
            import pika  # noqa: F401
        except ImportError:
            logger.warning("[monitor] enabled = true 但未安装 pika，监控上报已关闭（pip install 'paperlite[monitor]'）")
            return False
        logging.getLogger("pika").setLevel(logging.WARNING)    # pika 的 INFO 连接细节会刷屏下载日志
        self.enabled = True
        self._thread = threading.Thread(target=self._loop, name="monitor-publisher", daemon=True)
        self._thread.start()
        if self.cfg.forward_logs:
            self._log_handler = _ForwardHandler(self)
            logging.getLogger().addHandler(self._log_handler)
        logger.info("监控上报已启用 → %s exchange=%s 终端 %s", _redact(self.cfg.amqp_url), self.cfg.exchange,
                    self.cfg.instance)
        return True

    def close(self, timeout_s: float = 3.0) -> None:
        """尽量把缓冲发完（最多等 timeout_s），再停后台线程。"""
        if not self.enabled:
            return
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        deadline = time.monotonic() + timeout_s
        while self._buf and time.monotonic() < deadline:
            time.sleep(0.05)
        self.enabled = False
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=max(0.1, deadline - time.monotonic() + 1))

    # ── 发事件 ──
    def emit(self, type_: str, **data: Any) -> None:
        if not self.enabled:
            return
        msg = schema.envelope(type_, time.time(), self.cfg.node_name, self.cfg.instance, self.batch,
                              self.cfg.vendor_name, data)
        with self._cv:
            if len(self._buf) == self._buf.maxlen:
                self.dropped += 1                  # deque(maxlen) 自动挤掉最旧的
            self._buf.append(msg)
            self._cv.notify()

    # ── 后台发送 ──
    def _loop(self) -> None:
        import pika

        backoff = 1.0
        warned_at = 0.0
        while not self._stop.is_set():
            conn = None
            try:
                params = pika.URLParameters(self.cfg.amqp_url)
                params.heartbeat = 60
                params.blocked_connection_timeout = 30
                params.socket_timeout = 10
                conn = pika.BlockingConnection(params)
                ch = conn.channel()
                ch.exchange_declare(self.cfg.exchange, exchange_type="topic", durable=True)
                backoff = 1.0
                props = pika.BasicProperties(content_type="application/json", delivery_mode=2)
                while not self._stop.is_set():
                    with self._cv:
                        if not self._buf:
                            self._cv.wait(timeout=1.0)
                        batch = [self._buf.popleft() for _ in range(min(len(self._buf), 500))]
                    for i, msg in enumerate(batch):
                        try:
                            ch.basic_publish(self.cfg.exchange, msg["type"], schema.encode(msg), props)
                        except Exception:
                            with self._cv:                      # 没发出去的放回队头，重连后再发
                                self._buf.extendleft(reversed(batch[i:]))
                            raise
                        self.sent += 1
                    conn.process_data_events(0)                 # 维持 AMQP 心跳
            except Exception as e:
                if time.monotonic() - warned_at > 300:         # 连不上时 5 分钟最多提示一次，别刷屏
                    warned_at = time.monotonic()
                    logger.warning("监控上报连接 RabbitMQ 失败（不影响下载，%.0fs 后重连）: %s", backoff,
                                   str(e).splitlines()[0][:160] if str(e) else type(e).__name__,
                                   extra={"_monitor_internal": True})
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX_S)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass


class _ForwardHandler(logging.Handler):
    """把 WARNING+ 日志作为 log 事件上报（实时事件流）。跳过监控模块自己的日志，防自激。"""

    def __init__(self, reporter: Reporter) -> None:
        super().__init__(level=logging.WARNING)
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("paperlite.monitor") or getattr(record, "_monitor_internal", False):
            return
        try:
            self.reporter.emit(schema.LOG, level=record.levelname, event=getattr(record, "event", "log"),
                               message=record.getMessage()[:500])
        except Exception:
            pass


def _redact(url: str) -> str:
    """amqp://user:pass@host → amqp://user:***@host，日志里不露密码。"""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    cred, host = rest.rsplit("@", 1)
    user = cred.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


# ── 进程级单例 + 埋点函数 ──────────────────────────────────────────────

_reporter = Reporter()


def get() -> Reporter:
    return _reporter


def start(cfg: MonitorConfig, batch: str = "") -> Reporter:
    """按配置启用进程级上报（重复调用先关掉旧的）。"""
    global _reporter
    _reporter.close()
    _reporter = Reporter(cfg, batch)
    _reporter.start()
    return _reporter


def shutdown(timeout_s: float = 3.0) -> None:
    _reporter.close(timeout_s)


def proxy_check(ok: bool, total_ms: float = 0.0, reason: str = "") -> None:
    _reporter.emit(schema.PROXY_CHECK, ok=bool(ok), total_ms=round(total_ms, 1), reason=reason[:120])


def browser_start(ok: bool, kind: str, host: str = "", waf: str = "", seconds: float = 0.0,
                  error: str = "") -> None:
    _reporter.emit(schema.BROWSER, ok=bool(ok), kind=kind, host=host, waf=waf or schema.WAF_NONE,
                   seconds=round(seconds, 1), error=error[:200])


def page(host: str, passed: bool, ok: bool, status: Optional[int] = None, waf: str = "",
         elapsed_ms: int = 0, error: str = "") -> None:
    _reporter.emit(schema.PAGE, host=host, passed=bool(passed), ok=bool(ok), status=status,
                   waf=waf or schema.WAF_NONE, elapsed_ms=int(elapsed_ms), error=error[:200])


def heartbeat(**snapshot: Any) -> None:
    _reporter.emit(schema.HEARTBEAT, **snapshot)


class Heartbeat:
    """后台线程每 heartbeat_s 调 snapshot() 上报一次实时量；未启用上报时不建线程。"""

    def __init__(self, snapshot: Callable[[], dict[str, Any]], interval_s: Optional[float] = None) -> None:
        self._snapshot = snapshot
        self._interval = interval_s or _reporter.cfg.heartbeat_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if _reporter.enabled:
            self._thread = threading.Thread(target=self._run, name="monitor-heartbeat", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            self.beat()
            if self._stop.wait(self._interval):
                return

    def beat(self) -> None:
        try:
            heartbeat(**self._snapshot())
        except Exception:
            logger.debug("心跳快照失败", exc_info=True)

    def stop(self, final: bool = True) -> None:
        """停心跳；final=True 再发最后一次（让面板立刻看到浏览器数归零）。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if final:
                self.beat()


def attach(config_path, batch: str, snapshot: Callable[[], dict[str, Any]]) -> Optional["Attached"]:
    """下载命令一行接入：读 [monitor] → 启用上报 + 心跳。未启用返回 None。配置错误直接抛 ValueError。"""
    from paperlite.monitor.config import load_monitor
    cfg = load_monitor(config_path)
    if not cfg.enabled:
        return None
    if not start(cfg, batch).enabled:
        return None
    return Attached(Heartbeat(snapshot, cfg.heartbeat_s))


class Attached:
    def __init__(self, hb: Heartbeat) -> None:
        self.hb = hb

    def close(self) -> None:
        self.hb.stop(final=True)
        shutdown()
