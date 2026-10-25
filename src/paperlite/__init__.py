"""
PaperLite — 单站点大批量 PDF 下载：浏览器过盾 cookie 池（Redis）+ curl_cffi 下载 + SQLite 任务库，生产与消费解耦。

生产者（producer.py）按需提取新 IP、用独立 CloakBrowser 过 Cloudflare，把身份（IP+cookie+UA+过期时间）写进 Redis；
消费者（consumer.py）从 Redis 独占领用身份，经同一 IP 带 cookie 下载。两边只通过 store.py 交互。
配置见 config.example.toml，用法见 README.md。
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("paperlite")
except PackageNotFoundError:          # 未安装（直接从源码跑）
    __version__ = "0.0.0+local"
