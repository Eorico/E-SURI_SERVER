"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter

from api.core.cache import stats as cache_stats
from api.core.config import settings
from api.core.mongo import ping

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
def health() -> dict:
    return {"status": "ok", "environment": settings.ENVIRONMENT}


@router.get("/health/ready", summary="Readiness probe")
def ready() -> dict:
    """Confirms the database answers before traffic is sent here.

    Reports the round-trip time as well as the verdict, because the usual
    problem with a managed cluster is not that it is down but that it is far
    away - and a readiness probe that only says "ok" hides that.
    """
    try:
        latency = ping()
    except Exception:
        return {"status": "degraded", "database": "unreachable"}

    # Counters only - hit rate, entry count - so this is safe to expose. A low
    # hit rate under real traffic is the signal that something is invalidating
    # more than it needs to.
    return {
        "status": "ok",
        "database": "ok",
        "latency_ms": latency,
        "cache": cache_stats(),
    }
