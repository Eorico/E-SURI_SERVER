"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.core.config import settings
from api.core.database import get_db

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
def health() -> dict:
    return {"status": "ok", "environment": settings.ENVIRONMENT}


@router.get("/health/ready", summary="Readiness probe")
def ready(db: Session = Depends(get_db)) -> dict:
    """Confirms the database answers before traffic is sent here."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return {"status": "degraded", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}
