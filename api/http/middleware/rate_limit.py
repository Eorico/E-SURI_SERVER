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
#: Paths that are never metered. Interactive docs, and the liveness probe -
#: which answers from memory and touches nothing.
EXEMPT_PREFIXES = ("/docs", "/redoc", "/openapi.json")
EXEMPT_PATHS = ("/api/v1/health",)


def is_exempt(path: str) -> bool:
    """Whether a path bypasses the limiter entirely.

    Liveness is matched exactly rather than by prefix, deliberately. Readiness
    lives under the same prefix and pings the cluster on every call, so
    exempting the prefix would leave an unauthenticated endpoint that anyone
    could use to make the database do work - a load amplifier pointed at the
    one thing in the system that is hardest to scale. It is metered on the READ
    tier instead, which is three hundred calls per five minutes: far more than
    any load balancer needs and far less than a flood.
    """
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)


def tier_for(request: Request) -> RateLimitTier:
    path = request.url.path
    # Renewing or ending a session is not a credential attempt, so it does
    # not share the sign-in budget.
    if path.endswith("/auth/refresh") or path.endswith("/auth/logout"):
        return RateLimitTier.SESSION
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
        if not settings.RATE_LIMIT_ENABLED or is_exempt(request.url.path):
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
