"""FastAPI application factory.

Assembles the app from its parts and nothing else: middleware, exception
handlers and routers each own their own wiring, so adding a feature never means
editing this file (OCP).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from api.core.config import settings
from api.core.exceptions import register_exception_handlers
from api.core.logging import configure_logging, get_logger
from api.core.mongo import ping, reset_client
from api.http import middleware
from api.routes import register

logger = get_logger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("DEBUG" if settings.DEBUG else "INFO")
    # `message` is the line a person reads when they start the server; `event`
    # is the stable key a log search filters on. Keeping them apart means the
    # wording can change without breaking anything that greps for api_start.
    logger.info(
        "E-SURI SERVER IS RUNNING",
        extra={
            "event": "api_start",
            "server": app.title,
            "version": app.version,
        },
    )
    # Dial the cluster now rather than on somebody's first request.
    #
    # PyMongo connects lazily, so without this the first request after a
    # deploy pays for DNS, the TLS handshake and server selection - which
    # against a managed cluster in another region is most of a second, charged
    # to whoever happened to arrive first. Failure here is logged and ignored:
    # a cluster that is briefly unreachable must not stop the server starting,
    # and the driver reconnects on its own.
    try:
        logger.info(
            "database_ready",
            extra={"event": "db_warm", "latency_ms": ping()},
        )
    except Exception as exc:  # noqa: BLE001 - starting up matters more
        logger.warning(
            "database_not_reachable_at_startup",
            extra={"event": "db_cold", "error": str(exc)[:200]},
        )

    yield

    # Close the pool on the way out, so a reload does not leave sockets open.
    reset_client()
    logger.info("E-SURI SERVER HAS STOPPED", extra={"event": "api_stop"})


def create_app() -> FastAPI:
    app = FastAPI(
        title="Esuri Server",
        version="1.0.0",
        summary="Esuri for Paliparan III Senior High School",
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

    middleware.install(app)

    # CORS is registered LAST, which makes it the OUTERMOST layer.
    #
    # That ordering matters for error responses, not for successful ones. The
    # rate limiter answers a 429 itself without calling the rest of the stack,
    # so with CORS inside it that reply carried no Access-Control-Allow-Origin
    # and the browser reported a CORS failure instead of "too many requests" -
    # hiding the real cause behind a misleading one.
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
    register_exception_handlers(app)
    register(app)
    return app


app = create_app()
