"""HTTP middleware: cross-cutting concerns that apply to every request.

Order matters and is set in `install`: context first so everything downstream
can log a request id, then the body cap (cheapest rejection), then rate
limiting, then security headers on the way out.
"""

from __future__ import annotations

from fastapi import FastAPI

from api.http.middleware.body_limit import BodySizeLimitMiddleware
from api.http.middleware.rate_limit import RateLimitMiddleware
from api.http.middleware.request_context import RequestContextMiddleware
from api.http.middleware.security_headers import SecurityHeadersMiddleware

__all__ = [
    "BodySizeLimitMiddleware",
    "RateLimitMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
    "install",
]


def install(app: FastAPI) -> None:
    """Starlette runs middleware in reverse registration order, so the last
    one added is the outermost. Register from innermost to outermost."""
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
