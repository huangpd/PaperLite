"""浏览器模式下载 PDF：CDP 拦截响应、流式写盘（paperlite.pdf_capture）。用假的 CDP 会话驱动，不开浏览器。"""
import asyncio
import base64
import tempfile
import unittest
from pathlib import Path

from paperlite.pdf_capture import PdfCapture, expected_response_size, looks_like_pdf_response


class FakeCdp:
    """记录发出的 CDP 命令；IO.read 按 chunks 依次返回（base64）。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []

    async def send(self, method, params=None):
        self.sent.append((method, params or {}))
        if method == "Fetch.takeResponseBodyAsStream":
            return {"stream": "h1"}
        if method == "IO.read":
            data = self.chunks.pop(0) if self.chunks else b""
            return {"data": base64.b64encode(data).decode(), "base64Encoded": True, "eof": not self.chunks}
        return {}

    def methods(self):
        return [m for m, _ in self.sent]


def paused(url, status=200, headers=None):
    return {"requestId": "r1", "request": {"url": url}, "responseStatusCode": status,
            "responseHeaders": [{"name": k, "value": v} for k, v in (headers or {}).items()]}


class PdfCaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "a" / "x.pdf"

    def run_capture(self, chunks, headers, url="https://dl.acm.org/doi/pdf/10.1145/1", min_bytes=10):
        async def go():
            cdp = FakeCdp(chunks)
            cap = PdfCapture(cdp, url, self.dest, max_bytes=1000, min_bytes=min_bytes, proxy_auth=("u", "p"))
            await cap._on_paused(paused(url, headers=headers))
            return cap, cdp
        return asyncio.run(go())

    def test_streams_pdf_strips_leading_junk_and_aborts_request(self):
        body = b"\r\n\xef\xbb\xbf%PDF-1.7\n" + b"x" * 200
        cap, cdp = self.run_capture([body[:3], body[3:100], body[100:]], {"Content-Type": "application/pdf",
                                                                          "Content-Length": str(len(body))})
        got = cap.result.result()
        self.assertTrue(self.dest.read_bytes().startswith(b"%PDF-1.7"))
        self.assertEqual(got.size, len(body) - 5)                      # 前导换行和 BOM 已去掉
        self.assertFalse(self.dest.with_name("x.pdf.part").exists())
        self.assertIn("Fetch.failRequest", cdp.methods())               # 写完中止原请求，浏览器不再渲染它
        self.assertIn("IO.close", cdp.methods())

    def test_rejects_non_pdf_short_and_incomplete(self):
        for chunks, headers, needle in (
            ([b"<html>login</html>" * 30], {"Content-Type": "application/pdf"}, "不是 PDF"),
            ([b"%PDF-1.4"], {"Content-Type": "application/pdf"}, "过小"),
            ([b"%PDF-1.4" + b"x" * 50], {"Content-Type": "application/pdf", "Content-Length": "500"}, "不完整"),
            ([b"%PDF-1.4" + b"x" * 1200], {"Content-Type": "application/pdf"}, "超过大小上限"),
        ):
            cap, _ = self.run_capture(chunks, headers)
            self.assertFalse(cap.result.done(), needle)
            self.assertIn(needle, cap.last_error)
            self.assertFalse(self.dest.exists() or self.dest.with_name("x.pdf.part").exists(), needle)

    def test_stalled_stream_gives_up(self):
        """连续 stall_s 秒读不到新数据算卡死：放弃、删掉半截文件，不无限等。"""
        class Stalling(FakeCdp):
            reads = 0

            async def send(self, method, params=None):
                if method == "IO.read":
                    self.reads += 1
                    if self.reads == 1:                     # 第一块正常、没结束
                        data = b"%PDF-1.4" + b"x" * 2000
                        return {"data": base64.b64encode(data).decode(), "base64Encoded": True, "eof": False}
                    await asyncio.sleep(10)                 # 之后再也不来数据
                return await super().send(method, params)

        async def go():
            cdp = Stalling([])
            cap = PdfCapture(cdp, "https://x.org/a.pdf", self.dest, 10_000, 10, None, stall_s=0.05)
            await cap._on_paused(paused("https://x.org/a.pdf", headers={"Content-Type": "application/pdf"}))
            return cap, cdp
        cap, cdp = asyncio.run(go())
        self.assertFalse(cap.result.done())
        self.assertIn("下载卡住", cap.last_error)
        self.assertFalse(self.dest.exists() or self.dest.with_name("x.pdf.part").exists())
        self.assertIn("Fetch.failRequest", cdp.methods())          # 取走了响应流，只能中止原请求

    def test_html_response_passes_through(self):
        cap, cdp = self.run_capture([], {"Content-Type": "text/html; charset=utf-8"})
        self.assertFalse(cap.result.done())
        self.assertEqual(cdp.methods(), ["Fetch.continueResponse"])     # 网页照常交给浏览器

    def test_proxy_auth_answered_with_lease_credentials(self):
        async def go():
            cdp = FakeCdp([])
            cap = PdfCapture(cdp, "https://x.org/a.pdf", self.dest, 1000, 10, ("user", "pw"))
            await cap._on_auth({"requestId": "a1", "authChallenge": {"source": "Proxy"}})
            await cap._on_auth({"requestId": "a2", "authChallenge": {"source": "Server"}})
            return cdp
        sent = asyncio.run(go()).sent
        self.assertEqual(sent[0][1]["authChallengeResponse"],
                         {"response": "ProvideCredentials", "username": "user", "password": "pw"})
        self.assertEqual(sent[1][1]["authChallengeResponse"], {"response": "Default"})

    def test_looks_like_pdf(self):
        t = "https://dl.acm.org/doi/pdf/10.1145/1"
        self.assertTrue(looks_like_pdf_response(t, t, {"content-type": "application/pdf"}))
        self.assertTrue(looks_like_pdf_response("https://x.org/a", t, {"content-disposition": 'attachment; filename="a.pdf"'}))
        self.assertFalse(looks_like_pdf_response(t, t, {"content-type": "text/html"}))    # .pdf 地址上的挑战页
        self.assertTrue(looks_like_pdf_response("https://x.org/files/a.PDF", t, {}))
        self.assertTrue(looks_like_pdf_response(t, t, {"content-type": "application/octet-stream"}))
        self.assertFalse(looks_like_pdf_response("https://x.org/app.js", t, {"content-type": "application/javascript"}))

    def test_expected_size(self):
        self.assertEqual(expected_response_size(200, {"content-length": "123"}), 123)
        self.assertIsNone(expected_response_size(200, {"content-length": "123", "content-encoding": "gzip"}))
        self.assertEqual(expected_response_size(206, {"content-range": "bytes 0-99/100"}), 100)
        self.assertIsNone(expected_response_size(206, {"content-range": "bytes 10-99/100"}))


if __name__ == "__main__":
    unittest.main()
