"""Correlation id, access logging and response timing."""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from api.core.logging import get_logger, request_id_var, user_id_var

logger = get_logger("api.access")

#: Accept an inbound id only if it looks like one we would have generated.
_MAX_INBOUND_ID = 64


def client_address(request: Request) -> str:
    """`host:port`, as the access line prints it.

    The port is part of it because two requests from one host are otherwise
    indistinguishable in the log. A request with no client - an internal call,
    or a test transport - prints a dash rather than crashing the log line it
    was supposed to describe.
    """
    client = request.client
    if client is None:
        return "-"
    return f"{client.host}:{client.port}"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if 0 < len(incoming) <= _MAX_INBOUND_ID and incoming.isprintable() else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        user_token = user_id_var.set(None)

        # Captured once: the access line needs them, and reading them off the
        # request after the response has been built is not safe for a
        # streaming response.
        client = client_address(request)
        http_version = request.scope.get("http_version", "1.1")

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The exception handlers build the body; just record the attempt.
            logger.exception(
                "request_failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "client": client,
                    "http_version": http_version,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            raise
        finally:
            request_id_var.reset(token)
            user_id_var.reset(user_token)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "client": client,
                "http_version": http_version,
                "duration_ms": duration_ms,
            },
        )
        return response
