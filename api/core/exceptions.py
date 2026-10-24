"""Domain errors and the handlers that turn them into HTTP responses.

Services raise these; they know nothing about HTTP status codes. The mapping
lives in one place (`register_exception_handlers`), so a new error type needs one
line here rather than a try/except in every controller — DRY, and it keeps the
service layer free of framework concerns (SRP).
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from api.core.logging import get_logger, request_id_var

#: Starlette renamed this constant; pin the number so both versions work.
HTTP_422_UNPROCESSABLE = 422

logger = get_logger(__name__)


class DomainError(Exception):
    """Base class for every expected, business-level failure."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ValidationError(DomainError):
    status_code = HTTP_422_UNPROCESSABLE
    code = "validation_error"


class PermissionDeniedError(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class AuthenticationError(DomainError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"


class RateLimitedError(DomainError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after: int):
        super().__init__(message, details={"retry_after": retry_after})
        self.retry_after = retry_after


class UpstreamError(DomainError):
    """Supabase, storage or another dependency failed."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "upstream_error"


def _body(code: str, message: str, details: dict | None = None) -> dict:
    payload: dict = {"error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    if (rid := request_id_var.get()) is not None:
        payload["error"]["request_id"] = rid
    return payload


def register_exception_handlers(app: FastAPI) -> None:
    """Attach one handler per failure class. Called by the app factory."""

    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimitedError) else None
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.code, exc.message, exc.details),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's raw errors can echo submitted values; send only the location
        # and reason so a rejected payload never comes back in the response.
        safe = [
            {"field": ".".join(str(p) for p in err.get("loc", ())), "reason": err.get("msg", "")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE,
            content=_body("validation_error", "The request body failed validation.", {"fields": safe}),
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError) -> JSONResponse:
        logger.warning("integrity_error", extra={"detail": str(exc.orig)})
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_body("conflict", "That record conflicts with one that already exists."),
        )

    @app.exception_handler(SQLAlchemyError)
    async def _database(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.exception("database_error", exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_body("database_unavailable", "The database is not reachable right now."),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Never leak a traceback or driver message to the client.
        logger.exception("unhandled_error", exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_body("internal_error", "Something went wrong on our end."),
        )
