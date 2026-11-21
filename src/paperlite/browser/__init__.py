"""
paperlite/browser
─────────────────
浏览器模式（默认）：每个 worker 一个反检测浏览器 + 一个代理 IP，直接在浏览器里打开每篇文献。

  session.py      单个浏览器会话：启动、过盾、渲染网页、地理库
  pool.py         worker 池：领任务、首篇探测、换 IP、结果回写
  pdf_capture.py  PDF 流式下载（CDP Fetch 拦截响应体，不靠页面渲染）
  turnstile.py    Turnstile 点击器 + paperlite turnstile token 服务

curl 模式的生产者也借用 session.pass_challenge 过盾（唯一允许的跨模式依赖）。
"""
