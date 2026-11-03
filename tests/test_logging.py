"""The console format, and what must never reach it.

Two things are being protected here. The shape of the line, because it is read
by a person at a glance and small drifts in alignment make a wall of text; and
redaction, because a log file is the easiest place in a system to leak a
credential and the hardest place to notice one.
"""

from __future__ import annotations

import logging
import re

import pytest

from api.core.logging import (
    JsonFormatter,
    PrettyFormatter,
    colour_supported,
    redact,
    request_id_var,
)

#: Matches the shape the format is meant to have:
#:   08:48:44 AM  [esuri]  INFO:    <message>
LINE = re.compile(r"^\d{2}:\d{2}:\d{2} (AM|PM)  \[esuri\]  (\w+):\s+(.*)$")


def record(level: int = logging.INFO, message: str = "hello", **extra) -> logging.LogRecord:
    made = logging.LogRecord(
        name="test", level=level, pathname=__file__, lineno=1, msg=message, args=(), exc_info=None
    )
    for key, value in extra.items():
        setattr(made, key, value)
    return made


@pytest.fixture
def pretty() -> PrettyFormatter:
    return PrettyFormatter(tag="esuri", use_colour=False)


def access(**overrides) -> logging.LogRecord:
    fields = {
        "method": "GET",
        "path": "/api/v1/classes",
        "status": 200,
        "client": "123.253.51.137:0",
        "http_version": "1.1",
    }
    fields.update(overrides)
    return record(message="request", **fields)


# --------------------------------------------------------------------------- #
# The shape                                                                    #
# --------------------------------------------------------------------------- #
class TestFormat:
    def test_an_access_line_reads_as_a_request(self, pretty):
        line = pretty.format(access())
        assert '123.253.51.137:0 - "GET /api/v1/classes HTTP/1.1" 200 OK' in line

    def test_the_columns_are_where_they_should_be(self, pretty):
        matched = LINE.match(pretty.format(access()))
        assert matched, "the line no longer has the expected shape"
        assert matched.group(2) == "INFO"

    def test_the_clock_is_twelve_hour(self, pretty):
        """It is read by a person glancing at a terminal, not parsed."""
        assert LINE.match(pretty.format(record())).group(1) in ("AM", "PM")

    def test_every_level_starts_the_message_in_the_same_column(self, pretty):
        """A ragged left edge is what makes a log wall unreadable."""
        starts = set()
        for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
            line = pretty.format(record(level=level, message="MARK"))
            starts.add(line.index("MARK"))
        assert len(starts) == 1, f"message column moves between levels: {starts}"

    def test_a_non_access_record_prints_its_message_and_fields(self, pretty):
        line = pretty.format(record(message="database_ready", event="db_warm", latency_ms=31.6))
        assert "database_ready" in line
        assert "event=db_warm" in line
        assert "latency_ms=31.6" in line

    def test_the_duration_is_appended_not_interleaved(self, pretty):
        """It is useful but secondary, so it must not break the request column."""
        line = pretty.format(access(duration_ms=12.4))
        assert line.rstrip().endswith("12.4ms")
        assert '"GET /api/v1/classes HTTP/1.1" 200 OK' in line

    def test_the_request_id_is_shown_short(self, pretty):
        """Enough to correlate by eye; the whole id is in the header and JSON."""
        token = request_id_var.set("9f3a1c7e5b2d4a8f0011")
        try:
            assert "req=9f3a1c7e" in pretty.format(record(message="x"))
        finally:
            request_id_var.reset(token)


class TestStatus:
    @pytest.mark.parametrize(
        "status, phrase",
        [
            (200, "200 OK"),
            (201, "201 Created"),
            (401, "401 Unauthorized"),
            (404, "404 Not Found"),
            (429, "429 Too Many Requests"),
            (500, "500 Internal Server Error"),
        ],
    )
    def test_the_reason_phrase_is_spelled_out(self, pretty, status, phrase):
        assert phrase in pretty.format(access(status=status))

    def test_an_unknown_status_still_prints(self, pretty):
        """A custom code must not blow up the logging call it came from."""
        assert "599" in pretty.format(access(status=599))

    def test_a_request_with_no_client_prints_a_dash(self, pretty):
        """An internal call has no peer, and that is not a reason to crash."""
        assert ' - "GET' in pretty.format(access(client=None))


class TestColour:
    def test_colour_is_dropped_when_output_is_not_a_terminal(self):
        """Otherwise `uvicorn > server.log` fills the file with escapes."""

        class NotATty:
            def isatty(self):
                return False

        assert colour_supported(NotATty()) is False

    def test_no_color_is_honoured(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")

        class Tty:
            def isatty(self):
                return True

        assert colour_supported(Tty()) is False

    def test_colouring_wraps_and_resets(self):
        coloured = PrettyFormatter(tag="esuri", use_colour=True).format(access())
        assert "\033[" in coloured
        assert coloured.endswith("\033[0m")

    def test_without_colour_there_are_no_escapes(self, pretty):
        assert "\033[" not in pretty.format(access(status=500, duration_ms=9.1))


# --------------------------------------------------------------------------- #
# Redaction                                                                    #
# --------------------------------------------------------------------------- #
class TestRedaction:
    @pytest.mark.parametrize(
        "secret, text",
        [
            ("tok-abc123", "Authorization: Bearer tok-abc123"),
            ("hunter2", "password=hunter2"),
            ("sk-abc123", "api_key=sk-abc123"),
            ("S3cretP4ss", "mongodb+srv://esuri:S3cretP4ss@cluster0.ab12c.mongodb.net/"),
        ],
    )
    def test_credentials_never_survive(self, secret, text):
        assert secret not in redact(text)
        assert "[redacted]" in redact(text)

    def test_a_connection_string_keeps_its_host(self):
        """Redaction has to leave enough to tell which cluster it was."""
        out = redact("mongodb+srv://esuri:S3cretP4ss@cluster0.ab12c.mongodb.net/")
        assert "cluster0.ab12c.mongodb.net" in out
        assert "esuri" in out

    def test_a_bare_jwt_is_caught(self):
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"
        assert token not in redact(f"token {token}")

    def test_an_ordinary_url_is_left_alone(self):
        """Over-redacting makes the log useless in its own way."""
        url = "http://localhost:8000/api/v1/health"
        assert redact(url) == url

    def test_a_message_with_no_secret_is_unchanged(self):
        assert redact("GET /api/v1/classes 200 OK") == "GET /api/v1/classes 200 OK"

    def test_redaction_applies_in_both_formats(self, pretty):
        leaky = record(message="Bearer tok-abc123")
        assert "tok-abc123" not in pretty.format(leaky)
        assert "tok-abc123" not in JsonFormatter().format(record(message="Bearer tok-abc123"))


# --------------------------------------------------------------------------- #
# Choosing a format                                                            #
# --------------------------------------------------------------------------- #
class TestFormatChoice:
    def test_development_gets_the_readable_one(self, monkeypatch):
        from api.core.config import Settings

        settings = Settings(ENVIRONMENT="development", JWT_SECRET="x" * 48)
        assert settings.LOG_FORMAT == "pretty"

    def test_production_gets_the_parseable_one(self):
        """A log aggregator cannot do anything with colour codes."""
        from api.core.config import Settings

        settings = Settings(
            ENVIRONMENT="production",
            MONGODB_URI="mongodb+srv://u:p@real.mongodb.net/",
            JWT_SECRET="x" * 48,
        )
        assert settings.LOG_FORMAT == "json"

    def test_it_can_be_forced_either_way(self):
        from api.core.config import Settings

        for mode in ("pretty", "json"):
            settings = Settings(
                ENVIRONMENT="development", JWT_SECRET="x" * 48, LOG_FORMAT_MODE=mode
            )
            assert settings.LOG_FORMAT == mode

    def test_json_stays_machine_readable(self):
        import json

        payload = json.loads(JsonFormatter().format(access(duration_ms=12.4)))
        assert payload["status"] == 200
        assert payload["method"] == "GET"
        assert payload["duration_ms"] == 12.4


def test_uvicorns_access_logger_is_silenced():
    """It and `RequestContextMiddleware` both log every request.

    Ours knows the correlation id and the real duration, so uvicorn's is turned
    off - otherwise every request appears twice.
    """
    from api.core.logging import configure_logging

    configure_logging("INFO", fmt="pretty")
    access_logger = logging.getLogger("uvicorn.access")
    assert access_logger.disabled
    assert access_logger.handlers == []


def test_the_chatty_libraries_are_raised_to_warning():
    """`httpx2` prints a line per request and would bury the access log."""
    from api.core.logging import configure_logging

    configure_logging("INFO", fmt="pretty")
    for name in ("httpx", "httpx2", "watchfiles", "pymongo"):
        assert logging.getLogger(name).level >= logging.WARNING, name
