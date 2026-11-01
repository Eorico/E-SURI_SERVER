"""HTTP middleware: cross-cutting concerns that apply to every request.

Order matters and is set in `install`: context first so everything downstream
can log a request id, then the body cap (cheapest rejection), then rate
limiting, then security headers on the way out.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware

from api.http.middleware.body_limit import BodySizeLimitMiddleware
from api.http.middleware.rate_limit import RateLimitMiddleware
from api.http.middleware.request_context import RequestContextMiddleware
from api.http.middleware.security_headers import SecurityHeadersMiddleware

#: Response types that are already compressed. Gzipping them spends CPU on
#: every download for a percent or two - the workbook and the PDF are the ones
#: this project actually serves; the rest Starlette already excludes.
INCOMPRESSIBLE = (
    "application/vnd.ms-excel.sheet.macroEnabled.12",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/pdf",
    *GZipMiddleware.__init__.__kwdefaults__["exclude_content_types"],
)

__all__ = [
    "INCOMPRESSIBLE",
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
    # Outermost of these, so it compresses every body including error payloads.
    #
    # The report and analytics responses are long, repetitive JSON - the shape
    # gzip is best at. Level 5 rather than 9: the last four levels cost several
    # times the CPU for a few percent, which is the wrong trade on a response
    # the teacher is waiting for.
    app.add_middleware(
        GZipMiddleware,
        minimum_size=1024,
        compresslevel=5,
        exclude_content_types=INCOMPRESSIBLE,
    )
