"""Uploaded report templates."""

from __future__ import annotations

from pymongo import DESCENDING

from api.core.cache import cached
from api.models.documents import ReportTemplate
from api.models.enums import TemplateStatus
from api.repositories.base import MongoRepository


class ReportTemplateRepository(MongoRepository[ReportTemplate]):
    model = ReportTemplate
    #: Newest first: an administrator reviewing uploads wants the last one.
    default_sort = (("created_at", DESCENDING),)

    def by_checksum(self, checksum: str) -> ReportTemplate | None:
        return self.first(checksum=checksum)

    @cached()
    def active_for(self, school_year: str, grade_level: int) -> list[ReportTemplate]:
        """Which template an export should fill. Asked by every export."""
        return self.list(
            school_year=school_year, grade_level=grade_level, status=TemplateStatus.active
        )

    def newest_first(self, limit: int = 50) -> list[ReportTemplate]:
        return self.paginate(limit=limit)
