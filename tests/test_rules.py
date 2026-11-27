"""rules/：URL 校验（tasks.py）、页面判定（page.py）、PDF 文件头（pdf.py）。"""
import unittest

from paperlite.rules.page import classify_html, page_problem, site_key, visible_text_len
from paperlite.rules.tasks import url_problem




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


class PageProblemTest(unittest.TestCase):
    """浏览器拿到的网页能不能记成功（hw1 实测样本的缩写）。"""

    ARTICLE = "<html><head><title>Paper</title><script>var x='" + "s" * 5000 + "'</script></head><body>" \
              + "Abstract. Results and discussion. " * 30 + "</body></html>"

    def test_visible_text_skips_scripts_and_tags(self):
        self.assertEqual(visible_text_len("<title>T</title><script>abc</script><style>p{}</style><p> a  b </p>"),
                         len("T a b"))

    def test_normal_article(self):
        self.assertEqual(page_problem(self.ARTICLE, 500), "")

    def test_site_bot_check_is_blocked(self):
        # OUP 的人机验证页：状态 200、无 cf-mitigated，hw1 上 120 篇"成功"里有 19 篇是它
        oup = ("<html><head><title>Validate User</title></head><body><h1>Validate User</h1>We are sorry, but we "
               "are experiencing unusual traffic at this time. Please help us confirm that you are not a robot "
               "and we will take you to your content.<form id='captchaForm'></form></body></html>")
        self.assertEqual(page_problem(oup, 500), "blocked")
        self.assertEqual(page_problem(oup, 0), "blocked")          # 不判空壳时也认验证页

    def test_long_article_mentioning_captcha_is_fine(self):
        self.assertEqual(page_problem(self.ARTICLE.replace("Results", "CAPTCHA results"), 500), "")

    def test_shells_are_incomplete(self):
        head_only = "<html><head><title>Influence of Insects | Journal of Forestry</title><script></script></head>"
        spa_shell = "<html><head><title>Hierarchical inference | IEEE Xplore</title></head><body><div id=app></div></body></html>"
        self.assertEqual(page_problem(head_only, 500), "incomplete")
        self.assertEqual(page_problem(spa_shell, 500), "incomplete")
        self.assertEqual(page_problem(spa_shell, 0), "")             # min_text=0 关闭

    def test_site_key(self):
        self.assertEqual(site_key("ieeexplore.ieee.org"), "ieee.org")
        self.assertEqual(site_key("ieee.org"), "ieee.org")
        self.assertEqual(site_key("www.jstage.jst.go.jp"), "jst.go.jp")
        self.assertEqual(site_key("www.ox.ac.uk"), "ox.ac.uk")
        self.assertNotEqual(site_key("www.google-analytics.com"), site_key("www.sciencedirect.com"))


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
    """两种模式共用的 PDF 文件头规则（paperlite.rules.pdf）。"""

    def test_offset(self):
        from paperlite.rules.pdf import HEAD_BYTES, pdf_offset
        self.assertEqual(pdf_offset(b"%PDF-1.7\n"), 0)
        self.assertEqual(pdf_offset(b"\r\n\xef\xbb\xbf%PDF-1.7"), 5)          # 前导换行 / BOM 放行
        self.assertIsNone(pdf_offset(b"<p>see %PDF-1.4</p>"))              # 前面有正文：不是 PDF
        self.assertIsNone(pdf_offset(b" " * HEAD_BYTES + b"%PDF-1.4"))     # 超出前 1024 字节
        self.assertIsNone(pdf_offset(b"<html></html>"))
