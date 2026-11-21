"""
paperlite/pdf.py
────────────────
PDF 文件头判定。curl 模式的下载线程和浏览器模式的 CDP 拦截（browser/pdf_capture.py）共用这一条规则，
改规则只改这里。
"""

from typing import Optional

PDF_MAGIC = b"%PDF-"
HEAD_BYTES = 1024                                # %PDF- 须在前 1024 字节内（PDF 规范）
LEADING_JUNK = b" \t\r\n\x00\x0c\xef\xbb\xbf"    # %PDF- 之前只允许这些（有的 PHP 下载脚本会输出前导换行 / BOM）


def pdf_offset(head: bytes) -> Optional[int]:
    """%PDF- 在前 HEAD_BYTES 字节内、之前只有空白或 BOM 时返回它的位置；否则不是 PDF（与 paper_downloader 一致）。"""
    i = head.find(PDF_MAGIC, 0, HEAD_BYTES)
    if i < 0 or head[:i].strip(LEADING_JUNK):
        return None
    return i
