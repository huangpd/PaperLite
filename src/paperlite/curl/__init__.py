"""
paperlite/curl
──────────────
curl 模式：生产者用浏览器过盾拿 cookie，存进身份池；下载线程用 curl_cffi 带着这些身份批量下载。

  producer.py    生产者：按需租 IP、开浏览器过盾、验收后把身份放进池（借用 browser.session.pass_challenge）
  downloader.py  下载线程：从身份池领身份、从任务队列领任务，curl_cffi 下载并校验
  identity.py    身份池（PostgreSQL UNLOGGED 表）：领取 / 续约 / 释放 / 熔断，只有 curl 模式用
  cookies.py     cookie 与 JSON 记录互转
"""
