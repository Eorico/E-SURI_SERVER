"""Router registry.

`register` is the one place that knows which routers exist, so the app factory
stays unchanged as the API grows (OCP).
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from api.core.config import settings
from api.routes import (
    analysis,
    assessments,
    auth,
    certificates,
    classes,
    health,
    reports,
    scans,
    templates,
)

ROUTERS: tuple[APIRouter, ...] = (
    health.router,
    auth.router,
    classes.router,
    assessments.router,
    scans.router,
    analysis.router,
    reports.router,
    certificates.router,
    templates.router,
)


def register(app: FastAPI) -> None:
    for router in ROUTERS:
        app.include_router(router, prefix=settings.API_PREFIX)
