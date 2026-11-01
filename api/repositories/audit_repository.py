"""Append-only audit trail for actions that change results or release documents."""

from __future__ import annotations

import uuid

from pymongo import DESCENDING

from api.models.documents import AuditLog
from api.repositories.base import MongoRepository


class AuditRepository(MongoRepository[AuditLog]):
    model = AuditLog
    #: Newest first, by the microsecond counter rather than by `created_at` -
    #: see `AuditLog.sequence` for why the millisecond timestamp is not enough.
    default_sort = (("sequence", DESCENDING),)

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
        return self.paginate(limit=limit)

    def for_entity(self, entity_type: str, entity_id: uuid.UUID) -> list[AuditLog]:
        """One record's history — who changed it and when."""
        return self.list(entity_type=entity_type, entity_id=entity_id)
