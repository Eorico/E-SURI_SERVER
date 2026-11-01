"""Item analysis, EQWART reports, least-mastered competencies and certificates."""

from __future__ import annotations

import uuid

from pymongo import ASCENDING

from api.models.documents import (
    Certificate,
    EqwartReport,
    ItemAnalysis,
    LeastMastered,
)
from api.models.enums import AssessmentType, CertificateStatus
from api.repositories.base import EmbeddedRepository, MongoRepository


class ItemAnalysisRepository(MongoRepository[ItemAnalysis]):
    model = ItemAnalysis
    default_sort = (("item_no", ASCENDING),)

    def for_assessment(self, assessment_id: uuid.UUID) -> list[ItemAnalysis]:
        return self.list(assessment_id=assessment_id)

    def replace_for_assessment(
        self, assessment_id: uuid.UUID, rows: list[ItemAnalysis]
    ) -> list[ItemAnalysis]:
        """Analysis is derived data; recompute wholesale rather than patch."""
        self.delete_where(assessment_id=assessment_id)
        return self.add_all(rows)

    def weakest(self, assessment_id: uuid.UUID, limit: int) -> list[ItemAnalysis]:
        """The lowest-scoring items — the least mastered.

        Sorted on the server rather than in Python: the item number is the
        tie-break, so two items at the same percentage always come back in the
        same order and the five a coordinator encodes do not shuffle between
        two runs of the same report.
        """
        cursor = (
            self.collection.find({"assessment_id": assessment_id})
            .sort([("percent_correct", ASCENDING), ("item_no", ASCENDING)])
            .limit(limit)
        )
        return self._many(cursor)


class EqwartReportRepository(MongoRepository[EqwartReport]):
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


class LeastMasteredRepository(EmbeddedRepository[LeastMastered]):
    """The five least-mastered competencies, inside their report.

    Embedded: there are at most five, they are read with the report every time,
    and `position` orders them rather than being queried on.
    """

    model = LeastMastered
    parent_model = EqwartReport
    array_field = "least_mastered"
    parent_key = "report_id"
    order_by = "position"

    def for_report(self, report_id: uuid.UUID) -> list[LeastMastered]:
        return self.for_parent(report_id)

    def at_position(self, report_id: uuid.UUID, position: int) -> LeastMastered | None:
        return self.first(report_id=report_id, position=position)


class CertificateRepository(MongoRepository[Certificate]):
    model = Certificate

    def for_class(self, class_id: uuid.UUID) -> list[Certificate]:
        return self.list(class_id=class_id)

    def for_student(self, student_id: uuid.UUID, class_id: uuid.UUID) -> Certificate | None:
        return self.first(student_id=student_id, class_id=class_id)

    def released_for_class(self, class_id: uuid.UUID) -> list[Certificate]:
        return self.list(class_id=class_id, status=CertificateStatus.released)
