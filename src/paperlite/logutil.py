"""PaperLite 统一 JSON Lines 日志配置。"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


_STANDARD_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


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
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_KEYS and key not in data and not key.startswith("_"):
                data[key] = value
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))


def setup_logging(level: str, log_dir: str = "", stderr: bool = False) -> Path | None:
    """配置根 logger，同时写终端和可选日志目录；返回日志文件路径。"""
    formatter = JsonFormatter()
    stream = logging.StreamHandler(sys.stderr if stderr else sys.stdout)
    stream.setFormatter(formatter)
    handlers: list[logging.Handler] = [stream]
    log_path = None
    if log_dir:
        directory = Path(log_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / f"paperlite-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    logging.basicConfig(level=level.upper(), handlers=handlers, force=True)
    return log_path


def log_event(logger: logging.Logger, level: int, event: str, message: str, **fields: Any) -> None:
    """记录带事件名和业务字段的结构化日志。"""
    logger.log(level, message, extra={"event": event, **fields})
