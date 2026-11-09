"""
上报消息格式（上报端与面板端共用）。每条消息一个 JSON 信封，发到 topic exchange，routing key = type：

  {"v": 1, "type": "page", "ts": 1731111111.2, "node": "gpu-01", "inst": "gpu-01:4242",
   "batch": "sciencedirect", "vendor": "qingguo", "data": {...}}

type 与 data 字段：
  proxy_check  一个新 IP 的质检结果（每 IP 一条，不按域名）
               ok, total_ms, reason
  browser      一次浏览器启动 + 过落地页的盾
               ok, kind(ok|challenge_failed|exception), host, waf, seconds, error
  page         一次页面采集尝试（含失败后稍后重试的那次）
               host, passed(页面真加载出来=过了盾), ok(passed 且 HTTP<400), status, waf, elapsed_ms, error
  heartbeat    终端实时量快照
               mode, browsers_online, workers, pending, running, success, failed
  log          WARNING 及以上日志（实时事件流）
               level, event, message

只发 IP 与统计，不发代理账号密码、不发页面内容。
"""

import json
from typing import Any

SCHEMA_VERSION = 1

PROXY_CHECK = "proxy_check"
BROWSER = "browser"
PAGE = "page"
HEARTBEAT = "heartbeat"
LOG = "log"
TYPES = (PROXY_CHECK, BROWSER, PAGE, HEARTBEAT, LOG)

# WAF 识别结果（browser.classify_waf）
WAF_CLOUDFLARE = "cloudflare"
WAF_AWS = "aws_waf"
WAF_DATADOME = "datadome"
WAF_NONE = "none"
WAFS = (WAF_CLOUDFLARE, WAF_AWS, WAF_DATADOME)


def envelope(type_: str, ts: float, node: str, inst: str, batch: str, vendor: str,
             data: dict[str, Any]) -> dict[str, Any]:
    return {"v": SCHEMA_VERSION, "type": type_, "ts": ts, "node": node, "inst": inst,
            "batch": batch, "vendor": vendor, "data": data}


def encode(msg: dict[str, Any]) -> bytes:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def decode(body: bytes) -> dict[str, Any]:
    """解析并做最低限度校验；不认识的版本/类型抛 ValueError（面板端丢弃该条）。"""
    msg = json.loads(body)
    if not isinstance(msg, dict) or msg.get("v") != SCHEMA_VERSION or msg.get("type") not in TYPES:
        raise ValueError(f"不支持的监控消息: {str(msg)[:120]}")
    if not isinstance(msg.get("data"), dict):
        raise ValueError("监控消息缺少 data")
    return msg
