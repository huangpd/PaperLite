"""共享内核：URL 校验（tasks.py）、页面判定（page.py）、PDF 文件头（pdf.py）。"""
import unittest

from paperlite.page import classify_html
from paperlite.tasks import url_problem




class ClassifyTest(unittest.TestCase):
    NAV = '<a href="/premium" title="Basic Edition, Upgrade">'

    def test_categories(self):
        self.assertEqual(classify_html(f"<title>ACM Error: 404</title>{self.NAV}"), "not_found")
        self.assertEqual(classify_html(
            f"<title>Paper</title>{self.NAV}You are using the Basic Edition. Features requiring a subscription"),
            "paywall")
        self.assertEqual(classify_html("<title>ACM Error: IP blocked</title>"), "blocked")
        # 导航栏的 Basic Edition 每页都有，不算订阅
        self.assertEqual(classify_html(f"<title>Paper</title>{self.NAV}"), "landing_page")


class UrlProblemTest(unittest.TestCase):
    """导入时剔除明显下载不到的链接（来自 hw1 两个批次里的真实坏数据）。"""

    def test_obviously_bad(self):
        cases = {
            "https://10.65.16.77/index.php/sociobiology/article/view/3300": "private_ip",
            "http://192.168.1.111/index.php/ForoEducacional/article/view/779": "private_ip",
            "https://192.168.7.25:443/index.php/assc/article/view/6870": "private_ip",
            "http://172.20.1.1/a": "private_ip",
            "http://127.0.0.1/a": "private_ip",
            "http://[::1]/a": "private_ip",
            "https://.asm.org/content/177/11/3358": "bad_host",
            "http://0": "bad_host",
            "http://null/10.1017/S0022112011000036": "bad_host",
            "http://http://sociologiaitaliana.egeaonline.it/it/21": "bad_host",
            "https://opgadmin/oe/abstract.cfm?doi=10.1364/OE.473012": "bad_host",
            "https://x..org/a": "bad_host",
            "http://x.org:abc/": "bad_host",
            "ftp://ftp.example.org/a.pdf": "bad_scheme",
            "http:///path": "no_host",
            "": "bad_scheme",
        }
        for url, reason in cases.items():
            self.assertEqual(url_problem(url), reason, url)

    def test_normal_kept(self):
        for url in ("https://www.sciencedirect.com/science/article/pii/S0092867424000011",
                    "https://linkinghub.elsevier.com/retrieve/pii/0001691855900019",
                    "HTTPS://WWW.Nature.COM/articles/x", "https://example.com./a", "https://a_b.example.com/x",
                    "https://xn--fiqs8s.cn/a",
                    "http://219.238.6.200/x", "http://155.206.11.181:90/content/x",     # 公网 IP 建站：放行
                    "http://10040.indexcopernicus.com/abstracted.php?level=5&ICID=1138423"):   # 合法但打不开：交给下载
            self.assertIsNone(url_problem(url), url)


class PdfHeaderTest(unittest.TestCase):
    """两种模式共用的 PDF 文件头规则（paperlite.pdf）。"""

    def test_offset(self):
        from paperlite.pdf import HEAD_BYTES, pdf_offset
        self.assertEqual(pdf_offset(b"%PDF-1.7\n"), 0)
        self.assertEqual(pdf_offset(b"\r\n\xef\xbb\xbf%PDF-1.7"), 5)          # 前导换行 / BOM 放行
        self.assertIsNone(pdf_offset(b"<p>see %PDF-1.4</p>"))              # 前面有正文：不是 PDF
        self.assertIsNone(pdf_offset(b" " * HEAD_BYTES + b"%PDF-1.4"))     # 超出前 1024 字节
        self.assertIsNone(pdf_offset(b"<html></html>"))
