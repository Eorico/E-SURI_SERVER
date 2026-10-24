"""Per-client rate limiting on a five-minute window.

The tier is chosen from the path and method, so a new route inherits a sensible
limit without being registered anywhere (OCP). Authenticated callers are keyed
by user id; everyone else by client address, which stops one signed-in account
from being throttled by someone else behind the same NAT.
"""

from __future__ import annotations

import jwt
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from api.core.config import settings
from api.core.rate_limit import RateLimitTier, limiter
from api.core.security import client_ip

#: Paths that must stay reachable for health checks and docs.
EXEMPT_PREFIXES = ("/api/v1/health", "/docs", "/redoc", "/openapi.json")


def tier_for(request: Request) -> RateLimitTier:
    path = request.url.path
    if "/auth/" in path:
        return RateLimitTier.AUTH
    if "/scan" in path or path.endswith("/submissions/upload"):
        return RateLimitTier.SCAN
    if "/export" in path or "/download" in path or "/sheets" in path:
        return RateLimitTier.EXPORT
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        return RateLimitTier.WRITE
    return RateLimitTier.READ


def _caller_key(request: Request) -> str:
    """User id when we can read one cheaply, else the client address.

    The token is decoded without verification here — a forged id only ever
    costs the forger their own bucket, and real verification happens in the
    auth dependency.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        try:
            claims = jwt.decode(header[7:], options={"verify_signature": False})
            if subject := claims.get("sub"):
                return f"user:{subject}"
        except jwt.PyJWTError:
            pass
    return f"ip:{client_ip(request)}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not settings.RATE_LIMIT_ENABLED or request.url.path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        tier = tier_for(request)
        result = limiter.check(_caller_key(request), tier)

        if not result.allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many requests. Try again shortly.",
                        "details": {"retry_after": result.retry_after},
                    }
                },
                headers={
                    "Retry-After": str(result.retry_after),
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        response.headers["X-RateLimit-Reset"] = str(result.reset_after)
        return response
