"""Append-only audit trail for actions that change results or release documents."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from api.models.tables import AuditLog
from api.repositories.base import SqlRepository


class AuditRepository(SqlRepository[AuditLog]):
    model = AuditLog

    def record(
        self,
        *,
        actor_id: uuid.UUID | None,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None = None,
        metadata: dict | None = None,
        ip_address: str | None = None,
    ) -> AuditLog:
        return self.add(
            AuditLog(
                actor_id=actor_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                metadata_json=metadata,
                ip_address=ip_address,
            )
        )

    def recent(self, limit: int = 100) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        return list(self.db.scalars(stmt))
