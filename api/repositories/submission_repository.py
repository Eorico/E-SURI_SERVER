"""Submissions and the per-item responses behind them."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from api.models.enums import AssessmentType, SubmissionStatus
from api.models.tables import Assessment, Response, Submission
from api.repositories.base import SqlRepository


class SubmissionRepository(SqlRepository[Submission]):
    model = Submission

    def for_assessment(self, assessment_id: uuid.UUID) -> list[Submission]:
        stmt = (
            select(Submission)
            .options(joinedload(Submission.student))
            .filter_by(assessment_id=assessment_id)
        )
        return list(self.db.scalars(stmt))

    def approved_for_assessment(self, assessment_id: uuid.UUID) -> list[Submission]:
        """Only approved papers feed the statistics."""
        stmt = (
            select(Submission)
            .options(joinedload(Submission.student))
            .filter_by(assessment_id=assessment_id, status=SubmissionStatus.approved)
        )
        return list(self.db.scalars(stmt))

    def with_responses(self, submission_id: uuid.UUID) -> Submission | None:
        stmt = (
            select(Submission)
            .options(selectinload(Submission.responses), joinedload(Submission.student))
            .filter(Submission.id == submission_id)
        )
        return self.db.scalars(stmt).first()

    def approved_with_responses(self, assessment_id: uuid.UUID) -> list[Submission]:
        stmt = (
            select(Submission)
            .options(selectinload(Submission.responses))
            .filter_by(assessment_id=assessment_id, status=SubmissionStatus.approved)
        )
        return list(self.db.scalars(stmt))

    def for_student(self, assessment_id: uuid.UUID, student_id: uuid.UUID) -> Submission | None:
        return self.first(assessment_id=assessment_id, student_id=student_id)

    def next_awaiting_review(self, assessment_id: uuid.UUID) -> Submission | None:
        stmt = (
            select(Submission)
            .options(joinedload(Submission.student))
            .filter_by(assessment_id=assessment_id, status=SubmissionStatus.review)
            .order_by(Submission.scanned_at)
            .limit(1)
        )
        return self.db.scalars(stmt).first()

    def approved_types_for_student(
        self, student_id: uuid.UUID, class_id: uuid.UUID
    ) -> set[AssessmentType]:
        """Which assessment types this learner has an approved paper for."""
        stmt = (
            select(Assessment.type)
            .join(Submission, Submission.assessment_id == Assessment.id)
            .where(
                Submission.student_id == student_id,
                Submission.status == SubmissionStatus.approved,
                Assessment.class_id == class_id,
            )
        )
        return set(self.db.scalars(stmt))


class ResponseRepository(SqlRepository[Response]):
    model = Response

    def for_submission(self, submission_id: uuid.UUID) -> list[Response]:
        stmt = select(Response).filter_by(submission_id=submission_id).order_by(Response.item_no)
        return list(self.db.scalars(stmt))

    def replace_for_submission(
        self, submission_id: uuid.UUID, responses: list[Response]
    ) -> list[Response]:
        """A rescan supersedes the previous reading entirely."""
        self.delete_where(submission_id=submission_id)
        return self.add_all(responses)
