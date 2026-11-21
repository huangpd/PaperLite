#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paperlite/browser/pdf_capture.py
────────────────────────────────
浏览器模式下载 PDF：在页面上开 CDP Fetch 拦截，主文档（或子请求）的响应是 PDF 时，
直接用 Fetch.takeResponseBodyAsStream + IO.read 把响应体流式写盘，写完中止这个请求，浏览器不渲染、不下载它。

为什么不用"打开页面再点下载 / 等 download 事件"：无界面 Chromium 打开 PDF 直链会把它当下载，
page.goto 直接报错；点按钮又要按站点写规则。拦截响应只看响应头，跟站点无关；请求走的是浏览器自己的
网络栈（同一 IP、同一 cookie、同一 TLS 指纹），过盾后的身份直接可用。参考 paper_downloader 的
downloader/cloak_fallback.py（_CdpPdfCapture），去掉了按站点的部分。

校验与 curl 模式一致：前 1024 字节内必须有 %PDF-（前导空白 / BOM 写盘时去掉）、不小于 min_bytes、
不超过 max_bytes、有 Content-Length 时长度要对上；先写 .part，校验通过再改名。

开启 Fetch 拦截后，代理鉴权（Fetch.authRequired）也要由本会话应答，否则 Chromium 报
ERR_INVALID_AUTH_CREDENTIALS（Linux CloakBrowser 146 上必现）。
"""

import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("paperlite.browser.pdf_capture")

PDF_MAGIC = b"%PDF-"
HEAD_BYTES = 1024                      # %PDF- 必须出现在这么多字节以内（与 curl 模式一致）
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$", re.IGNORECASE)


@dataclass
class CapturedPdf:
    path: Path
    size: int
    status: int
    final_url: str


class PdfCapture:
    """一个页面一次下载用；用完 close()。result 在截到合格的 PDF 后完成，errors 记校验失败原因。"""

    def __init__(self, session: Any, target_url: str, dest: Path, max_bytes: int, min_bytes: int,
                 proxy_auth: Optional[tuple[str, str]], stall_s: float = 120) -> None:
        self.session = session
        self.target_url = target_url
        self.dest = Path(dest)
        self.max_bytes = max_bytes
        self.min_bytes = min_bytes
        self.proxy_auth = proxy_auth
        self.stall_s = stall_s                 # 连续这么久读不到新数据算卡死（与 curl 模式的 download_timeout_s 一致，不限总时长）
        self.result: asyncio.Future[CapturedPdf] = asyncio.get_running_loop().create_future()
        self.errors: list[str] = []
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._closing = False

    @classmethod
    async def create(cls, context: Any, page: Any, target_url: str, dest: Path, *, max_bytes: int,
                     min_bytes: int, proxy_auth: Optional[tuple[str, str]] = None,
                     stall_s: float = 120) -> "PdfCapture":
        session = await context.new_cdp_session(page)
        cap = cls(session, target_url, dest, max_bytes, min_bytes, proxy_auth, stall_s)
        session.on("Fetch.requestPaused", cap._schedule)
        session.on("Fetch.authRequired", cap._schedule_auth)
        await session.send("Fetch.enable", {
            "patterns": [{"urlPattern": "*", "requestStage": "Request"},
                         {"urlPattern": "*", "requestStage": "Response"}],
            "handleAuthRequests": True,
        })
        return cap

    @property
    def streaming(self) -> bool:
        """正在把 PDF 写盘：原请求暂停中，页面导航会一直等，这段时间不能按超时算失败。"""
        return self._lock.locked()

    @property
    def last_error(self) -> str:
        return self.errors[-1] if self.errors else ""

    # ── 事件分发 ────────────────────────────────────────────

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.debug("PDF 拦截任务异常: %s", task.exception())

    def _schedule(self, params: dict) -> None:
        if not self._closing:
            self._spawn(self._on_paused(params))

    def _schedule_auth(self, params: dict) -> None:
        self._spawn(self._on_auth(params))

    async def _on_auth(self, params: dict) -> None:
        """代理鉴权用当前代理账号应答；网站自己的鉴权交回浏览器默认处理。"""
        challenge = params.get("authChallenge", {})
        if challenge.get("source") == "Proxy" and self.proxy_auth:
            user, password = self.proxy_auth
            response = {"response": "ProvideCredentials", "username": user, "password": password}
        else:
            response = {"response": "Default"}
        try:
            await self.session.send("Fetch.continueWithAuth",
                                    {"requestId": params["requestId"], "authChallengeResponse": response})
        except Exception:
            logger.debug("应答鉴权失败", exc_info=True)

    async def _on_paused(self, params: dict) -> None:
        request_id = params["requestId"]
        if "responseStatusCode" not in params and "responseErrorReason" not in params:
            await self._send("Fetch.continueRequest", {"requestId": request_id})      # 请求阶段：放行
            return
        if self.result.done():
            await self._send("Fetch.continueResponse", {"requestId": request_id})
            return
        headers = headers_dict(params.get("responseHeaders", []))
        url = params.get("request", {}).get("url", "")
        status = int(params.get("responseStatusCode") or 0)
        if not 200 <= status < 300 or not looks_like_pdf_response(url, self.target_url, headers):
            await self._send("Fetch.continueResponse", {"requestId": request_id})
            return
        expected = expected_response_size(status, headers)
        if status == 206 and expected is None:
            self.errors.append(f"拒绝不完整的 206 PDF 响应: {headers.get('content-range', '无 Content-Range')}")
            await self._send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
            return
        if expected is not None and expected > self.max_bytes:
            self.errors.append(f"PDF 超过大小上限: {expected} > {self.max_bytes} 字节")
            await self._send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
            return
        async with self._lock:
            if self.result.done():
                await self._send("Fetch.continueResponse", {"requestId": request_id})
                return
            await self._stream(request_id, status, url, expected)

    async def _stream(self, request_id: str, status: int, final_url: str, expected: Optional[int]) -> None:
        part = self.dest.with_name(self.dest.name + ".part")
        handle: Optional[str] = None
        taken = False
        try:
            handle = (await self.session.send("Fetch.takeResponseBodyAsStream", {"requestId": request_id}))["stream"]
            taken = True
            part.parent.mkdir(parents=True, exist_ok=True)
            head = bytearray()                 # 凑够 HEAD_BYTES 再判文件头，去掉 %PDF- 前的空白 / BOM
            started = False
            raw = written = 0
            with open(part, "wb") as out:
                while True:
                    try:
                        item = await asyncio.wait_for(
                            self.session.send("IO.read", {"handle": handle, "size": 1024 * 1024}), self.stall_s)
                    except asyncio.TimeoutError:
                        raise ValueError(f"下载卡住：{self.stall_s:.0f}s 内没有收到新数据（已收 {raw} 字节）") from None
                    data = item.get("data", "")
                    # 块恰为合法 UTF-8 时 Chrome 返回文本：按 UTF-8 还原就是原始字节（latin-1 会写坏 PDF）
                    chunk = base64.b64decode(data) if item.get("base64Encoded") else data.encode("utf-8")
                    eof = bool(item.get("eof"))
                    raw += len(chunk)
                    if raw > self.max_bytes:
                        raise ValueError(f"PDF 超过大小上限: {raw} > {self.max_bytes} 字节")
                    if not started:
                        head.extend(chunk)
                        if len(head) < HEAD_BYTES and not eof:
                            continue
                        at = bytes(head[:HEAD_BYTES]).find(PDF_MAGIC)
                        if at < 0:
                            raise ValueError("响应不是 PDF（前 1024 字节没有 %PDF-）")
                        out.write(head[at:])
                        written += len(head) - at
                        started = True
                    elif chunk:
                        out.write(chunk)
                        written += len(chunk)
                    if eof:
                        break
            if not started:
                raise ValueError("响应为空")
            if expected is not None and raw != expected:
                raise ValueError(f"PDF 不完整: 实际 {raw}，Content-Length {expected} 字节")
            if written < self.min_bytes:
                raise ValueError(f"PDF 过小: {written} < {self.min_bytes} 字节")
            part.replace(self.dest)
            if not self.result.done():
                self.result.set_result(CapturedPdf(self.dest, written, status, final_url))
            # 先发布结果再中止原请求：浏览器不再渲染 / 下载它
            await self._send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
        except asyncio.CancelledError:
            part.unlink(missing_ok=True)
            await self._send("Fetch.failRequest" if taken else "Fetch.continueResponse",
                             {"requestId": request_id, **({"errorReason": "Aborted"} if taken else {})})
            raise
        except Exception as e:
            part.unlink(missing_ok=True)
            self.errors.append(str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}")
            # 已取走响应流就不能再放行原响应，只能中止
            await self._send("Fetch.failRequest" if taken else "Fetch.continueResponse",
                             {"requestId": request_id, **({"errorReason": "Aborted"} if taken else {})})
        finally:
            if handle is not None:
                await self._send("IO.close", {"handle": handle})

    async def _send(self, method: str, params: dict) -> None:
        try:
            await self.session.send(method, params)
        except Exception:
            logger.debug("CDP %s 失败", method, exc_info=True)

    async def close(self) -> None:
        self._closing = True
        active = [t for t in self._tasks if not t.done()]
        if active:
            _, pending = await asyncio.wait(active, timeout=5)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await self._send("Fetch.disable", {})
        try:
            await self.session.detach()
        except Exception:
            pass
        if not self.result.done():
            self.result.cancel()


def headers_dict(headers: list[dict]) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in headers if h.get("name")}


def looks_like_pdf_response(url: str, target_url: str, headers: dict[str, str]) -> bool:
    """响应头说是 PDF，或地址像 PDF 且没说是 HTML。明确是 HTML 的不截：.pdf 地址上也可能是中转页或挑战页。"""
    content_type = headers.get("content-type", "").lower()
    disposition = headers.get("content-disposition", "").lower()
    if "pdf" in content_type or ".pdf" in disposition:
        return True
    if "html" in content_type:
        return False
    path = urlsplit(url).path.lower()
    same_target = url.split("#", 1)[0] == target_url.split("#", 1)[0]
    return path.endswith(".pdf") or "/pdf/" in path or (same_target and content_type in ("", "application/octet-stream"))


def expected_response_size(status: int, headers: dict[str, str]) -> Optional[int]:
    """整份响应应有的字节数：206 要求 Content-Range 从 0 覆盖到末尾；压缩传输时 Content-Length 不是正文长度，不用。"""
    if status == 206:
        m = _CONTENT_RANGE_RE.fullmatch(headers.get("content-range", "").strip())
        if m is None:
            return None
        start, end, total = (int(v) for v in m.groups())
        return total if start == 0 and end + 1 == total else None
    length = headers.get("content-length", "").strip()
    if length.isdigit() and headers.get("content-encoding", "identity").lower() in ("", "identity"):
        return int(length)
    return None
