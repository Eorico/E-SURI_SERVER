"""Uploaded report templates."""

from __future__ import annotations

from sqlalchemy import select

from api.models.enums import TemplateStatus
from api.models.tables import ReportTemplate
from api.repositories.base import SqlRepository


class ReportTemplateRepository(SqlRepository[ReportTemplate]):
    model = ReportTemplate

    def by_checksum(self, checksum: str) -> ReportTemplate | None:
        return self.first(checksum=checksum)

    def active_for(self, school_year: str, grade_level: int) -> list[ReportTemplate]:
        return self.list(
            school_year=school_year, grade_level=grade_level, status=TemplateStatus.active
        )

    def newest_first(self, limit: int = 50) -> list[ReportTemplate]:
        stmt = (
            select(ReportTemplate).order_by(ReportTemplate.created_at.desc()).limit(limit)
        )
        return list(self.db.scalars(stmt))
