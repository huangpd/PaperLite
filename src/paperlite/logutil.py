"""PaperLite 日志配置：一份事件流，两个渲染器。

- 文件：永远 JSON Lines（字段齐全、可检索）。
- 终端：按去向选——交互式 TTY 给人类可读文本（时间 + 级别 + 消息 + key=val，可带颜色），
  被重定向 / 管道 / docker·systemd 采集（非 TTY）则给 JSON。``--log-format`` 可手动覆盖。
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

LOG_FORMATS = ("auto", "text", "json")

_STANDARD_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
# 文本行里不重复打印的字段：这些要么已在前缀里（时间/级别/消息），要么对盯进度没用
_TEXT_HIDDEN_FIELDS = {"event", "logger", "module", "thread"}
_LEVEL_COLOR = {"DEBUG": "90", "INFO": "", "WARNING": "33", "ERROR": "31", "CRITICAL": "1;31"}


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    """取通过 ``extra`` 传入的业务字段（排除 logging 内建字段）。"""
    return {k: v for k, v in record.__dict__.items()
            if k not in _STANDARD_RECORD_KEYS and not k.startswith("_")}


class JsonFormatter(logging.Formatter):
    """把 LogRecord 格式化为单行 JSON，并保留通过 ``extra`` 传入的结构化字段。"""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": getattr(record, "event", "log"),
            "logger": record.name,
            "module": record.module,
            "thread": record.threadName,
            "message": record.getMessage(),
        }
        for key, value in _extra_fields(record).items():
            data.setdefault(key, value)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """人类可读单行：``HH:MM:SS LEVEL  消息  k=v k=v``；extra 字段附在后面。"""

    def __init__(self, color: bool = False) -> None:
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        level = record.levelname
        line = f"{ts} {level:<7} {record.getMessage()}"
        extras = {k: v for k, v in _extra_fields(record).items() if k not in _TEXT_HIDDEN_FIELDS}
        if extras:
            line += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        if self.color and (code := _LEVEL_COLOR.get(level)):
            line = f"\033[{code}m{line}\033[0m"
        return line


def _console_formatter(fmt: str, stream: Any) -> logging.Formatter:
    is_tty = hasattr(stream, "isatty") and stream.isatty()
    if fmt == "json" or (fmt == "auto" and not is_tty):
        return JsonFormatter()
    return ConsoleFormatter(color=is_tty and os.environ.get("NO_COLOR") is None)


def setup_logging(level: str, log_dir: str = "", stderr: bool = False, fmt: str = "auto") -> Path | None:
    """配置根 logger：终端按 ``fmt`` 选文本/JSON，日志文件永远 JSON；返回日志文件路径。"""
    dest = sys.stderr if stderr else sys.stdout
    stream = logging.StreamHandler(dest)
    stream.setFormatter(_console_formatter(fmt, dest))
    handlers: list[logging.Handler] = [stream]
    log_path = None
    if log_dir:
        directory = Path(log_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / f"paperlite-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        handlers.append(file_handler)
    logging.basicConfig(level=level.upper(), handlers=handlers, force=True)
    return log_path


def log_event(logger: logging.Logger, level: int, event: str, message: str, **fields: Any) -> None:
    """记录带事件名和业务字段的结构化日志。"""
    logger.log(level, message, extra={"event": event, **fields})
