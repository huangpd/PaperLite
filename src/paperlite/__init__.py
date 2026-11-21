"""
PaperLite — 大批量 PDF / 网页下载（过 Cloudflare）：真实浏览器 + 短效代理，PostgreSQL 任务队列（一台或多台机器共享）。

curl 模式：生产者（curl/producer.py）按需提取新 IP、用独立 CloakBrowser 过 Cloudflare，把身份（IP+cookie+UA+过期时间）
写进身份池（curl/identity.py，也在 PostgreSQL）；下载线程（curl/downloader.py）独占领用身份，经同一 IP 带 cookie 下载。两边只通过身份池交互。
配置模板见 config.example.toml（paperlite init 生成 config.toml），用法见 README.md 与 docs/。
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("paperlite")
except PackageNotFoundError:          # 未安装（直接从源码跑）
    __version__ = "0.0.0+local"
