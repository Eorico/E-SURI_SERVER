"""Item analysis, EQWART reports, least-mastered competencies and certificates."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from api.models.enums import AssessmentType, CertificateStatus
from api.models.tables import Certificate, EqwartReport, ItemAnalysis, LeastMastered
from api.repositories.base import SqlRepository


class ItemAnalysisRepository(SqlRepository[ItemAnalysis]):
    model = ItemAnalysis

    def for_assessment(self, assessment_id: uuid.UUID) -> list[ItemAnalysis]:
        stmt = (
            select(ItemAnalysis)
            .filter_by(assessment_id=assessment_id)
            .order_by(ItemAnalysis.item_no)
        )
        return list(self.db.scalars(stmt))

    def replace_for_assessment(
        self, assessment_id: uuid.UUID, rows: list[ItemAnalysis]
    ) -> list[ItemAnalysis]:
        """Analysis is derived data; recompute wholesale rather than patch."""
        self.delete_where(assessment_id=assessment_id)
        return self.add_all(rows)

    def weakest(self, assessment_id: uuid.UUID, limit: int) -> list[ItemAnalysis]:
        """The lowest-scoring items — the least mastered."""
        stmt = (
            select(ItemAnalysis)
            .filter_by(assessment_id=assessment_id)
            .order_by(ItemAnalysis.percent_correct.asc(), ItemAnalysis.item_no.asc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt))


class EqwartReportRepository(SqlRepository[EqwartReport]):
    model = EqwartReport

    def find(
        self, learning_area_id: uuid.UUID, assessment_type: AssessmentType, school_year: str
    ) -> EqwartReport | None:
        return self.first(
            learning_area_id=learning_area_id,
            assessment_type=assessment_type,
            school_year=school_year,
        )

    def for_school_year(self, school_year: str) -> list[EqwartReport]:
        return self.list(school_year=school_year)


class LeastMasteredRepository(SqlRepository[LeastMastered]):
    model = LeastMastered

    def for_report(self, report_id: uuid.UUID) -> list[LeastMastered]:
        stmt = select(LeastMastered).filter_by(report_id=report_id).order_by(LeastMastered.position)
        return list(self.db.scalars(stmt))

    def at_position(self, report_id: uuid.UUID, position: int) -> LeastMastered | None:
        return self.first(report_id=report_id, position=position)


class CertificateRepository(SqlRepository[Certificate]):
    model = Certificate

    def for_class(self, class_id: uuid.UUID) -> list[Certificate]:
        return self.list(class_id=class_id)

    def for_student(self, student_id: uuid.UUID, class_id: uuid.UUID) -> Certificate | None:
        return self.first(student_id=student_id, class_id=class_id)

    def released_for_class(self, class_id: uuid.UUID) -> list[Certificate]:
        return self.list(class_id=class_id, status=CertificateStatus.released)
