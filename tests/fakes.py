"""测试共用的替身与常量（curl 模式、浏览器模式、任务队列的测试都用）。"""
import threading
import time

from paperlite.curl.cookies import jar_from_records
from paperlite.curl.identity import Identity
from paperlite.proxy import ProxySettings

FAKE_PDF = b"%PDF-1.4\n" + b"0" * 2048          # 超过最小文件大小（1024 字节）
PROXY = ProxySettings(api_url="https://api.example/get?key=k", username="u", password="p@ss")


def ident(**kw) -> Identity:
    d = dict(host="dl.acm.org", exit_ip="1.2.3.4", proxy_server="1.2.3.4:8000", user_agent="Mozilla/5.0 Chrome/146.0",
             cookies=[{"name": "cf_clearance", "value": "v", "domain": ".acm.org", "path": "/"}],
             expires_at=time.time() + 600)
    d.update(kw)
    d["cookies"] = jar_from_records(d["cookies"])
    return Identity(**d)


class Resp:
    """curl_cffi 响应替身：支持 stream=True 的 iter_content / close。fail_after 字节后模拟断流。"""

    def __init__(self, status, body, headers=None, fail_after=None):
        self.status_code, self.content, self.headers = status, body, headers or {}
        self.fail_after = fail_after
        self.closed = False

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self.content), 100):          # 小块，覆盖"首块不够 1024 字节"的情况
            if self.fail_after is not None and i >= self.fail_after:
                raise ConnectionError("curl: (56) Connection closed abruptly")
            yield self.content[i:i + 100]

    def close(self):
        self.closed = True


class FakeStore:
    """内存版 IdentityStore，只实现消费者用到的部分。"""

    def __init__(self, idents):
        self.ready = list(idents)
        self.discarded, self.released = [], []
        self.fresh_rejects = 0
        self.lock = threading.Lock()

    def note_success(self, host):
        self.fresh_rejects = 0

    def note_fresh_reject(self, host):
        self.fresh_rejects += 1
        return self.fresh_rejects

    def claim(self, host, holder, min_left_s, lease_ttl_s):
        with self.lock:
            return self.ready.pop(0) if self.ready else None

    def heartbeat(self, ident, ttl):
        pass

    def discard(self, ident):
        self.discarded.append(ident.exit_ip)

    def release(self, ident):
        self.released.append(ident.exit_ip)

    def worker_alive(self, host, worker):
        self.alive_calls = getattr(self, "alive_calls", 0) + 1

    def worker_gone(self, host, worker):
        pass

    def report_pending(self, host, consumer, n):
        self.pending_reports = getattr(self, "pending_reports", []) + [n]

    def clear_pending(self, host, consumer):
        self.pending_cleared = True

    def note_good_url(self, host, url):
        self.good = url

    def good_url(self, host):
        return getattr(self, "good", None)
