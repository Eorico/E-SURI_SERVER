"""Structured JSON logging with a per-request correlation id.

The id is held in a ContextVar so any module can attach it without threading a
request object through the call stack. `SensitiveFilter` is the last line of
defence against a token or password reaching the log file.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)

#: Anything that looks like a credential is replaced before the record is written.
_SECRET_PATTERNS = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"((?:password|secret|token|api[_-]?key|authorization)\"?\s*[:=]\s*\"?)[^\s,\"}]+", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9._\-]{10,}"),  # bare JWTs
)

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1) if m.lastindex else ''}[redacted]", text)
    return text


class SensitiveFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        if (rid := request_id_var.get()) is not None:
            payload["request_id"] = rid
        if (uid := user_id_var.get()) is not None:
            payload["user_id"] = uid
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return redact(json.dumps(payload, default=str))


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler once, replacing uvicorn's default formatting."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SensitiveFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
