"""
面板 HTTP 服务（标准库，无额外依赖）：
  GET /                      大屏页面（每 10s 自动刷新）
  GET /api/summary?window=1d|7d|30d   全部聚合数字（JSON）
  GET /health
配置了 token 时，页面与接口都要带 ?token=…（或请求头 X-Token）。
"""

import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlsplit

from paperlite.monitor.config import MonitorConfig
from paperlite.monitor.db import WINDOWS, MonitorDB

logger = logging.getLogger(__name__)


def make_server(cfg: MonitorConfig, db: MonitorDB) -> ThreadingHTTPServer:
    page = resources.files("paperlite.monitor").joinpath("dashboard.html").read_bytes()
    online_s = max(60.0, cfg.heartbeat_s * 4)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _authorized(self, qs) -> bool:
            if not cfg.token:
                return True
            given = (qs.get("token") or [""])[0] or self.headers.get("X-Token", "")
            return hmac.compare_digest(given.encode(), cfg.token.encode())

        def do_GET(self):
            u = urlsplit(self.path)
            qs = parse_qs(u.query)
            if u.path == "/health":
                return self._json(200, {"ok": True})
            if not self._authorized(qs):
                return self._json(401, {"error": "需要 token"})
            if u.path == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            if u.path == "/api/summary":
                window = (qs.get("window") or ["1d"])[0]
                if window not in WINDOWS:
                    return self._json(400, {"error": f"window 只支持 {', '.join(WINDOWS)}"})
                try:
                    return self._json(200, db.summary(window, online_s=online_s))
                except Exception as e:
                    logger.exception("聚合查询失败")
                    return self._json(500, {"error": str(e)})
            return self._json(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            logger.debug("%s " + fmt, self.address_string(), *args)

    srv = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    srv.daemon_threads = True
    return srv
