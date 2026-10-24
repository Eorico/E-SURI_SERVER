"""Assessments, answer keys and generated answer sheets."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from api.models.enums import AssessmentStatus, AssessmentType
from api.models.tables import AnswerKey, AnswerSheet, Assessment, Class
from api.repositories.base import SqlRepository


class AssessmentRepository(SqlRepository[Assessment]):
    model = Assessment

    def by_class(self, class_id: uuid.UUID) -> list[Assessment]:
        stmt = select(Assessment).filter_by(class_id=class_id).order_by(Assessment.date)
        return list(self.db.scalars(stmt))

    def by_code(self, test_code: str) -> Assessment | None:
        return self.first(test_code=test_code)

    def get_full(self, assessment_id: uuid.UUID) -> Assessment | None:
        stmt = (
            select(Assessment)
            .options(joinedload(Assessment.class_).joinedload(Class.section))
            .filter(Assessment.id == assessment_id)
        )
        return self.db.scalars(stmt).first()

    def for_learning_area(
        self, learning_area_id: uuid.UUID, assessment_type: AssessmentType, school_year: str
    ) -> list[Assessment]:
        """Every section's copy of one assessment — the columns of an EQWART sheet."""
        stmt = (
            select(Assessment)
            .join(Class, Class.id == Assessment.class_id)
            .options(joinedload(Assessment.class_).joinedload(Class.section))
            .where(
                Class.learning_area_id == learning_area_id,
                Class.school_year == school_year,
                Assessment.type == assessment_type,
            )
        )
        return list(self.db.scalars(stmt))

    def for_section(self, section_id: uuid.UUID, school_year: str) -> list[Assessment]:
        stmt = (
            select(Assessment)
            .join(Class, Class.id == Assessment.class_id)
            .where(Class.section_id == section_id, Class.school_year == school_year)
        )
        return list(self.db.scalars(stmt))

    def mark_in_progress(self, assessment: Assessment) -> Assessment:
        if assessment.status in (AssessmentStatus.draft, AssessmentStatus.scheduled):
            return self.update(assessment, status=AssessmentStatus.in_progress)
        return assessment


class AnswerKeyRepository(SqlRepository[AnswerKey]):
    model = AnswerKey

    def for_assessment(self, assessment_id: uuid.UUID) -> list[AnswerKey]:
        stmt = select(AnswerKey).filter_by(assessment_id=assessment_id).order_by(AnswerKey.item_no)
        return list(self.db.scalars(stmt))

    def as_map(self, assessment_id: uuid.UUID) -> dict[int, str]:
        """Maps item number to correct option — what the scorer needs."""
        return {key.item_no: key.correct_option for key in self.for_assessment(assessment_id)}

    def item(self, assessment_id: uuid.UUID, item_no: int) -> AnswerKey | None:
        return self.first(assessment_id=assessment_id, item_no=item_no)


class AnswerSheetRepository(SqlRepository[AnswerSheet]):
    model = AnswerSheet

    def for_assessment(self, assessment_id: uuid.UUID) -> list[AnswerSheet]:
        return self.list(assessment_id=assessment_id)

    def for_student(self, assessment_id: uuid.UUID, student_id: uuid.UUID) -> AnswerSheet | None:
        return self.first(assessment_id=assessment_id, student_id=student_id)
