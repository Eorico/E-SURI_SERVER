"""FastAPI application factory.

Assembles the app from its parts and nothing else: middleware, exception
handlers and routers each own their own wiring, so adding a feature never means
editing this file (OCP).
"""

from __future__ import annotations

import utils.session
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from api.core.config import settings
from api.core.exceptions import register_exception_handlers
from api.core.logging import configure_logging, get_logger
from api.http import middleware
from api.routes import register

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging("DEBUG" if settings.DEBUG else "INFO")
    logger.info(
        "api_start",
        extra={"environment": settings.ENVIRONMENT, "rate_limit_window": settings.RATE_LIMIT_WINDOW_SECONDS},
    )
    yield
    logger.info("api_stop")


def create_app() -> FastAPI:
    app = FastAPI(
        title="IMECAR API",
        version="1.0.0",
        summary="Examination checker and EQWART analysis for Paliparan III Senior High School",
        lifespan=lifespan,
        # Interactive docs are useful in development and a map of the attack
        # surface in production, so they are switched off there.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # Host check first: reject a spoofed Host before anything else runs.
    if settings.TRUSTED_HOSTS and settings.TRUSTED_HOSTS != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.TRUSTED_HOSTS)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        # Explicit lists: a wildcard here would let any site read the API with
        # the user's token.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-RateLimit-Remaining", "Retry-After"],
        max_age=600,
    )

    middleware.install(app)
    register_exception_handlers(app)
    register(app)
    return app


app = create_app()
