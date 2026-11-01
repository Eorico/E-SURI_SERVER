"""Submissions and the per-item responses behind them.

The responses are embedded, which changes the cost of this file more than any
other. `with_responses` and `approved_with_responses` were the two heaviest
reads in the system: the first needed a second query for the responses, the
second needed one for a whole section's worth. Both are now a plain find —
the answers arrive inside the document that owns them.
"""

from __future__ import annotations

import uuid
from typing import Sequence

from pymongo import ASCENDING

from api.models.documents import Response, Student, Submission
from api.models.enums import AssessmentType, SubmissionStatus
from api.repositories.base import EmbeddedRepository, MongoRepository


class SubmissionRepository(MongoRepository[Submission]):
    model = Submission
    default_sort = (("created_at", ASCENDING),)

    def for_assessment(self, assessment_id: uuid.UUID) -> list[Submission]:
        return self._with_students(self.list(assessment_id=assessment_id))

    def approved_for_assessment(self, assessment_id: uuid.UUID) -> list[Submission]:
        """Only approved papers feed the statistics."""
        return self._with_students(
            self.list(assessment_id=assessment_id, status=SubmissionStatus.approved)
        )

    def with_responses(self, submission_id: uuid.UUID) -> Submission | None:
        """One paper, its answers and the learner who sat it.

        Two reads: the submission, whose answers arrive with it because they
        are embedded, and the learner. Held separately the answers would be a
        third read of sixty documents that then had to be reassembled in
        memory.
        """
        record = self.get(submission_id)
        return self._with_students([record])[0] if record else None

    def approved_with_responses(self, assessment_id: uuid.UUID) -> list[Submission]:
        """Every approved paper with its answers — what item analysis consumes.

        One query. As separate tables this was a submissions query plus a
        responses query of 60 x N rows that then had to be grouped back
        together in memory.
        """
        return self.list(assessment_id=assessment_id, status=SubmissionStatus.approved)

    def for_student(self, assessment_id: uuid.UUID, student_id: uuid.UUID) -> Submission | None:
        return self.first(assessment_id=assessment_id, student_id=student_id)

    def next_awaiting_review(self, assessment_id: uuid.UUID) -> Submission | None:
        """The oldest paper still waiting for a teacher to look at it."""
        document = self.collection.find_one(
            {"assessment_id": assessment_id, "status": SubmissionStatus.review.value},
            sort=[("scanned_at", ASCENDING)],
        )
        record = self._one(document)
        return self._with_students([record])[0] if record else None

    def approved_types_for_student(
        self, student_id: uuid.UUID, class_id: uuid.UUID
    ) -> set[AssessmentType]:
        """Which assessment types this learner has an approved paper for.

        Used to decide which certificate someone has earned, so it must not
        over-report: the assessments are narrowed to the class first, then the
        learner's approved papers are matched against them.
        """
        # Both sides of this stay as stored strings: the ids are only ever
        # compared with each other, never with a UUID a caller holds.
        assessments = {
            document["_id"]: document["type"]
            for document in self.other("assessments").find(
                {"class_id": class_id}, projection={"type": 1}
            )
        }
        if not assessments:
            return set()

        approved = self.collection.find(
            {
                "student_id": student_id,
                "status": SubmissionStatus.approved.value,
                "assessment_id": {"$in": list(assessments)},
            },
            projection={"assessment_id": 1},
        )
        return {
            AssessmentType(assessments[document["assessment_id"]]) for document in approved
        }

    def _with_students(self, records: Sequence[Submission]) -> list[Submission]:
        """Fill the `student` slot.

        One query for the whole batch, so a section of seventy papers costs one
        extra read rather than seventy.
        """
        present = [record for record in records if record is not None]
        if not present:
            return []

        wanted = {record.student_id for record in present if record.student_id}
        found = (
            self.other("students").find({"_id": {"$in": list(wanted)}}) if wanted else []
        )
        # Keyed on the model's UUID, not the raw string id - see `as_uuid`.
        learners = {
            record.id: record
            for record in (Student.model_validate(document) for document in found)
        }
        for record in present:
            record.student = learners.get(record.student_id)
        return list(present)


class ResponseRepository(EmbeddedRepository[Response]):
    """One item's answer, inside its submission.

    This is the embed that mattered most. As a table it was one row per item
    per learner — 60 items across 70 examinees is 4,200 rows for a single
    section's exam — and every score, item analysis and report had to join them
    back together.
    """

    model = Response
    parent_model = Submission
    array_field = "responses"
    parent_key = "submission_id"
    order_by = "item_no"

    def for_submission(self, submission_id: uuid.UUID) -> list[Response]:
        return self.for_parent(submission_id)

    def replace_for_submission(
        self, submission_id: uuid.UUID, responses: list[Response]
    ) -> list[Response]:
        """A rescan supersedes the previous reading entirely.

        One write now. It used to be a delete of sixty rows followed by an
        insert of sixty more, and the two were not atomic — a failure between
        them left a paper with no answers at all.
        """
        return self.replace_for_parent(submission_id, responses)
