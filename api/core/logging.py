"""Logging: a readable console format, and JSON when something is reading it.

Two formatters over one pipeline.

`PrettyFormatter` is for a person watching a terminal:

    08:48:44 AM  [esuri]  INFO:    127.0.0.1:52014 - "GET /api/v1/classes HTTP/1.1" 200 OK

`JsonFormatter` is for anything that parses logs. A log aggregator cannot do
much with colour codes, and a person cannot read a wall of JSON, so the choice
follows the environment rather than being a preference: pretty in development,
JSON in production. `LOG_FORMAT` overrides it either way.

Colour is dropped automatically when stdout is not a terminal, so a redirected
log file never fills up with escape sequences.

The request id lives in a ContextVar so any module can attach it without
threading a request object through the call stack, and `SensitiveFilter` is the
last line of defence against a token or password reaching the output - it runs
in both formats.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from contextvars import ContextVar
from http import HTTPStatus
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)

#: Anything that looks like a credential is replaced before the record is
#: written, as (pattern, replacement) so each can keep the parts that matter.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE), r"\1[redacted]"),
    (
        re.compile(
            r"((?:password|secret|token|api[_-]?key|authorization)\"?\s*[:=]\s*\"?)[^\s,\"}]+",
            re.IGNORECASE,
        ),
        r"\1[redacted]",
    ),
    (re.compile(r"\beyJ[A-Za-z0-9._\-]{10,}"), "[redacted]"),  # bare JWTs
    # Credentials in the authority of a URI: mongodb+srv://user:pass@host,
    # smtp://user:pass@host. The host has to survive, which is why these are
    # replacement templates rather than a prefix plus a marker.
    (
        re.compile(r"([a-zA-Z][\w+.\-]*://[^:@/\s]+:)[^@/\s]+(@)"),
        r"\1[redacted]\2",
    ),
)

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class SensitiveFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


# --------------------------------------------------------------------------- #
# Colour                                                                       #
# --------------------------------------------------------------------------- #
RESET = "\033[0m"
DIM = "\033[90m"
CYAN = "\033[36m"
BLUE = "\033[94m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD_RED = "\033[1;31m"

#: Level name -> colour. The severity should be readable at a glance from the
#: left margin without reading the words.
_LEVEL_COLOURS = {
    "DEBUG": DIM,
    "INFO": BLUE,
    "WARNING": YELLOW,
    "ERROR": RED,
    "CRITICAL": BOLD_RED,
}


def colour_supported(stream: Any) -> bool:
    """Whether to emit ANSI escapes at all.

    `NO_COLOR` is honoured because it is the agreed convention for exactly this
    (https://no-color.org), and a non-terminal stream is excluded so that
    `uvicorn ... > server.log` produces a readable file rather than one full of
    escape sequences.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def enable_windows_ansi() -> None:
    """Turn on virtual-terminal processing for the Windows console.

    Windows Terminal handles ANSI already, but a plain `conhost` window does
    not until this is set - and without it the output is littered with `←[36m`
    instead of being coloured. Failure is ignored: the worst case is the colour
    check below finding a console that cannot do it.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # -11 is STD_OUTPUT_HANDLE; 0x4 is ENABLE_VIRTUAL_TERMINAL_PROCESSING.
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x4)
    except Exception:  # noqa: BLE001 - colour is cosmetic, never fatal
        pass


def status_colour(status: int) -> str:
    if status < 300:
        return GREEN
    if status < 400:
        return CYAN
    if status < 500:
        return YELLOW
    return RED


# --------------------------------------------------------------------------- #
# Formatters                                                                   #
# --------------------------------------------------------------------------- #
class PrettyFormatter(logging.Formatter):
    """The console format - one aligned line per record.

    An access record (one carrying `method`, `path` and `status`) is rendered
    as the familiar request line; everything else prints its message followed
    by whatever extra fields it carried, dimmed so they stay out of the way.
    """

    #: Width of the level column. `INFO:` plus padding, so every message starts
    #: in the same column and the eye can run straight down it.
    LEVEL_WIDTH = 9

    def __init__(self, tag: str = "esuri", use_colour: bool = True):
        super().__init__()
        self.tag = tag
        self.colour = use_colour

    def paint(self, text: str, colour: str) -> str:
        return f"{colour}{text}{RESET}" if self.colour else text

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        # 12-hour with AM/PM: this is read by a person glancing at a terminal,
        # not parsed, and that is the clock they are wearing.
        return super().formatTime(record, datefmt or "%I:%M:%S %p")

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        parts = [
            self.paint(self.formatTime(record), DIM),
            self.paint(f"[{self.tag}]", CYAN),
            self.paint(f"{level + ':':<{self.LEVEL_WIDTH}}", _LEVEL_COLOURS.get(level, "")),
            self._body(record),
        ]
        line = "  ".join(parts[:2]) + "  " + parts[2] + parts[3]

        if record.exc_info:
            line += "\n" + self.paint(redact(self.formatException(record.exc_info)), DIM)
        return line

    def _body(self, record: logging.LogRecord) -> str:
        status = getattr(record, "status", None)
        method = getattr(record, "method", None)
        path = getattr(record, "path", None)

        if status is not None and method and path:
            return self._access_line(record, method, path, int(status))

        message = redact(record.getMessage())
        extras = self._extras(record)
        return message + (self.paint(f"  {extras}", DIM) if extras else "")

    def _access_line(
        self, record: logging.LogRecord, method: str, path: str, status: int
    ) -> str:
        """`127.0.0.1:52014 - "GET /api/v1/classes HTTP/1.1" 200 OK`"""
        client = getattr(record, "client", None) or "-"
        version = getattr(record, "http_version", None) or "1.1"
        try:
            reason = HTTPStatus(status).phrase
        except ValueError:
            # A custom or unknown code still has to print rather than blow up
            # the logging call it came from.
            reason = ""

        request = f'"{method} {path} HTTP/{version}"'
        rendered = f"{client} - {request} {self.paint(f'{status} {reason}'.strip(), status_colour(status))}"

        # The timing is useful but secondary, so it is dimmed and sits at the
        # end where it does not break the alignment of the request lines.
        if (duration := getattr(record, "duration_ms", None)) is not None:
            rendered += self.paint(f"  {duration}ms", DIM)
        return rendered

    def _extras(self, record: logging.LogRecord) -> str:
        """`key=value` for the fields a caller attached, in insertion order."""
        pairs = [
            f"{key}={value}"
            for key, value in record.__dict__.items()
            if key not in _RESERVED
            and not key.startswith("_")
            and key not in ("client", "http_version", "duration_ms")
        ]
        if (rid := request_id_var.get()) is not None:
            # Short form: enough to correlate by eye, and the whole id is in
            # the JSON output and the X-Request-ID header when it is needed.
            pairs.append(f"req={rid[:8]}")
        return redact(" ".join(pairs))


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


# --------------------------------------------------------------------------- #
# Wiring                                                                       #
# --------------------------------------------------------------------------- #
def build_formatter(fmt: str, tag: str, stream: Any) -> logging.Formatter:
    if fmt == "json":
        return JsonFormatter()
    enable_windows_ansi()
    return PrettyFormatter(tag=tag, use_colour=colour_supported(stream))


#: Libraries that log one or more lines per operation at INFO or DEBUG.
#:
#: Raised to WARNING so the stream stays readable: `httpx` prints a line for
#: every request the test client makes, `watchfiles` announces every saved file
#: under `--reload`, and `pymongo` is extremely chatty about connections. None
#: of them say anything a person watching the server needs, and all of them say
#: it often enough to bury the request that actually matters.
#:
#: They are not silenced - a real problem still surfaces at WARNING and above.
_NOISY = (
    "httpx",
    # Two httpx packages are installed side by side here and the test client
    # uses the second; its logger name differs, so naming only "httpx" leaves
    # a line printed for every single request.
    "httpx2",
    "httpcore",
    "openai",
    "openai._base_client",
    "watchfiles",
    "watchfiles.main",
    "pymongo",
    "pymongo.connection",
    "pymongo.serverSelection",
    "pymongo.command",
    "pymongo.topology",
    "asyncio",
    "multipart",
    "python_multipart",
    "PIL",
)


def quiet_noisy_libraries(floor: int = logging.WARNING) -> None:
    for name in _NOISY:
        logging.getLogger(name).setLevel(floor)


def configure_logging(
    level: str = "INFO", fmt: str | None = None, tag: str | None = None
) -> None:
    """Install one handler for the whole process, replacing uvicorn's.

    `fmt` is "pretty" or "json"; omitted, it follows the environment. Every
    logger routes through the same handler so the output is one consistent
    stream rather than uvicorn's format interleaved with ours.
    """
    from api.core.config import settings

    fmt = fmt or settings.LOG_FORMAT
    tag = tag or settings.LOG_TAG

    stream = sys.stdout
    handler = logging.StreamHandler(stream)
    handler.setFormatter(build_formatter(fmt, tag, stream))
    handler.addFilter(SensitiveFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False

    quiet_noisy_libraries()

    # uvicorn's own access logger is silenced rather than reformatted.
    #
    # `RequestContextMiddleware` already logs every request, and it knows
    # things uvicorn does not: the correlation id and how long the request
    # actually took. Leaving both enabled would print every request twice.
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    access.disabled = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
