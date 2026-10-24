"""Reject oversized request bodies before they are read into memory.

A large upload is the cheapest way to exhaust a server, so the declared length
is checked first and the stream is cut off if the declaration lies.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from api.core.config import settings


def _too_large() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": "payload_too_large",
                "message": "That upload is larger than this endpoint accepts.",
            }
        },
    )


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > settings.MAX_REQUEST_BYTES:
                    return _too_large()
            except ValueError:
                return _too_large()

        # A chunked body carries no length, so count the bytes as they arrive.
        if declared is None and request.method in {"POST", "PUT", "PATCH"}:
            request = _CappedRequest(request, settings.MAX_REQUEST_BYTES)

        return await call_next(request)


class _CappedRequest(Request):
    """Wraps the receive channel and stops once the cap is passed."""

    def __init__(self, request: Request, limit: int):
        super().__init__(request.scope, request.receive)
        self._limit = limit
        self._seen = 0
        self._inner_receive = request.receive

    async def receive(self):  # type: ignore[override]
        message = await self._inner_receive()
        if message.get("type") == "http.request":
            self._seen += len(message.get("body", b""))
            if self._seen > self._limit:
                raise _BodyTooLarge()
        return message


class _BodyTooLarge(Exception):
    """Raised when a chunked body exceeds the cap; surfaces as a 413."""
