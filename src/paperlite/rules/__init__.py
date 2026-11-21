"""
paperlite/rules
───────────────
纯规则，两种下载模式共用：只看传进来的数据给结论，不联网、不读写文件和数据库、不依赖本包其他模块。
（这条由 tests/test_layering.py 检查。）

  tasks.py  任务的公共定义：状态、错误分类、Task、URL 规范化与校验、DOI 推导、文件路径
  page.py   页面判定：Cloudflare 挑战 / 封禁、WAF 识别、non-PDF 分类
  pdf.py    PDF 文件头规则
"""
