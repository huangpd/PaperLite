"""
面板 HTTP 服务（标准库，无额外依赖）：
  GET /                      大屏页面（每 10s 自动刷新）
  GET /api/summary?window=1d|7d|30d   全部聚合数字（JSON）
  GET /api/domains?window=&sort=&order=asc|desc&q=&offset=&limit=&abnormal=1&group=1
                             域名表：服务端搜索/排序/分页（见 MonitorDB.domains）
  GET /health
鉴权（/health 除外）：配了 password → HTTP Basic（浏览器弹框）；配了 token → ?token=… 或请求头 X-Token。
两者都配时任一通过即可。都没配则不鉴权（只应在 bind 本机时这样用，cli 会拦）。
Basic 鉴权密码在 HTTP 里是明文：对外开放时要么设 tls_cert/tls_key 让面板自己走 HTTPS，
要么放在 HTTPS 反代后面（见 docs/MONITOR.md）。
"""

import base64
import hmac
import json
import logging
import socket
import ssl
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
        timeout = 30                                   # 慢连接/半开连接最多占一个线程 30s

        def setup(self):
            if isinstance(self.request, ssl.SSLSocket):  # TLS 握手放在本请求线程里做，不卡 accept
                self.request.settimeout(15)
                self.request.do_handshake()
            super().setup()

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
            if not cfg.auth_enabled:
                return True
            if cfg.token:
                given = (qs.get("token") or [""])[0] or self.headers.get("X-Token", "")
                if given and hmac.compare_digest(given.encode(), cfg.token.encode()):
                    return True
            if cfg.password:
                return _basic_ok(self.headers.get("Authorization", ""), cfg.username, cfg.password)
            return False

        def do_GET(self):
            u = urlsplit(self.path)
            qs = parse_qs(u.query)
            if u.path == "/health":
                return self._json(200, {"ok": True})
            if not self._authorized(qs):
                body = json.dumps({"error": "需要登录"}, ensure_ascii=False).encode()
                self.send_response(401)
                if cfg.password:
                    self.send_header("WWW-Authenticate", 'Basic realm="PaperLite monitor", charset="UTF-8"')
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                logger.info("面板鉴权失败 %s %s", self.address_string(), u.path)
                return
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
            if u.path == "/api/domains":
                a = {k: v[0] for k, v in qs.items() if k != "token"}
                try:
                    return self._json(200, db.domains(
                        a.get("window", "1d"), sort=a.get("sort", "attempts"), order=a.get("order", "desc"),
                        q=a.get("q", "")[:100], offset=int(a.get("offset", 0)), limit=int(a.get("limit", 20)),
                        abnormal=a.get("abnormal") == "1", group=a.get("group") == "1"))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                except Exception as e:
                    logger.exception("域名查询失败")
                    return self._json(500, {"error": str(e)})
            return self._json(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            logger.debug("%s " + fmt, self.address_string(), *args)

    srv = (_TLSServer if cfg.tls_cert else ThreadingHTTPServer)((cfg.bind, cfg.port), Handler)
    srv.daemon_threads = True
    if cfg.tls_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(_expand(cfg.tls_cert), _expand(cfg.tls_key))
        srv.tls_context = ctx
    return srv


class _TLSServer(ThreadingHTTPServer):
    """HTTPS：accept 后只包装、不握手（握手在 Handler.setup 的工作线程里），握手失败的连接静默丢弃。"""
    tls_context: ssl.SSLContext

    def get_request(self):
        sock, addr = self.socket.accept()
        return self.tls_context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, ConnectionError, socket.timeout, TimeoutError)):
            logger.debug("丢弃 TLS 连接 %s: %s", client_address, exc)     # 如有人用 http:// 访问 https 端口
            return
        super().handle_error(request, client_address)


def _expand(path: str) -> str:
    from pathlib import Path
    return str(Path(path).expanduser())


def _basic_ok(header: str, username: str, password: str) -> bool:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return False
    try:
        user, _, pw = base64.b64decode(value.strip(), validate=True).decode("utf-8").partition(":")
    except Exception:
        return False
    # 两项都比完再判，避免按用户名是否正确产生时间差
    ok_user = hmac.compare_digest(user.encode(), username.encode())
    ok_pw = hmac.compare_digest(pw.encode(), password.encode())
    return ok_user and ok_pw
