"""Assessments and answer keys.

Owns two rules the rest of the system depends on:

* the item count is fixed by type — 30 for summative, 60 for pre/post/term;
* the answer key is immutable once the first sheet has been scanned, so a score
  can never change underneath a paper that was already marked.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from api.core.exceptions import ConflictError, NotFoundError, ValidationError
from api.models.enums import (
    ANSWER_OPTIONS,
    ASSESSMENT_LABELS,
    ITEM_COUNT_BY_TYPE,
    AssessmentStatus,
    AssessmentType,
)
from api.models.tables import AnswerKey, Assessment
from api.repositories.assessment_repository import AnswerKeyRepository, AssessmentRepository
from api.repositories.class_repository import ClassRepository

#: Test codes are short enough to print on a sheet and read back by eye.
TEST_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TEST_CODE_LENGTH = 6
MAX_CODE_ATTEMPTS = 8


class AssessmentService:
    def __init__(
        self,
        assessments: AssessmentRepository,
        answer_keys: AnswerKeyRepository,
        classes: ClassRepository,
    ):
        self.assessments = assessments
        self.answer_keys = answer_keys
        self.classes = classes

    # --- assessments ------------------------------------------------------
    def create(
        self,
        *,
        class_id: uuid.UUID,
        assessment_type: AssessmentType,
        title: str | None,
        date=None,
        created_by: uuid.UUID | None = None,
    ) -> Assessment:
        if self.classes.get(class_id) is None:
            raise NotFoundError("That class does not exist.")
        if self.assessments.exists(class_id=class_id, type=assessment_type):
            raise ConflictError(
                f"This class already has a {ASSESSMENT_LABELS[assessment_type]}."
            )

        assessment = Assessment(
            title=title or ASSESSMENT_LABELS[assessment_type],
            type=assessment_type,
            class_id=class_id,
            item_count=ITEM_COUNT_BY_TYPE[assessment_type],
            test_code=self._unique_code(),
            options="".join(ANSWER_OPTIONS),
            date=date,
            status=AssessmentStatus.draft,
            created_by=created_by,
        )
        return self.assessments.add(assessment)

    def get(self, assessment_id: uuid.UUID) -> Assessment:
        assessment = self.assessments.get_full(assessment_id)
        if assessment is None:
            raise NotFoundError("That assessment does not exist.")
        return assessment

    def list_for_class(self, class_id: uuid.UUID) -> list[Assessment]:
        return self.assessments.by_class(class_id)

    def by_test_code(self, test_code: str) -> Assessment:
        assessment = self.assessments.by_code(test_code.strip().upper())
        if assessment is None:
            raise NotFoundError("No assessment carries that test code.")
        return assessment

    def schedule(self, assessment_id: uuid.UUID, when) -> Assessment:
        assessment = self.get(assessment_id)
        return self.assessments.update(
            assessment, date=when, status=AssessmentStatus.scheduled
        )

    # --- answer key -------------------------------------------------------
    def set_answer_key(
        self, assessment_id: uuid.UUID, answers: dict[int, str]
    ) -> list[AnswerKey]:
        """Replace the whole key. Rejected once scanning has begun."""
        assessment = self.get(assessment_id)
        self._guard_key_mutable(assessment)
        self._validate_answers(assessment, answers)

        self.answer_keys.delete_where(assessment_id=assessment_id)
        rows = [
            AnswerKey(assessment_id=assessment_id, item_no=item_no, correct_option=option)
            for item_no, option in sorted(answers.items())
        ]
        return self.answer_keys.add_all(rows)

    def update_answer(self, assessment_id: uuid.UUID, item_no: int, option: str) -> AnswerKey:
        """Change one item, for fixing a typo before the first scan."""
        assessment = self.get(assessment_id)
        self._guard_key_mutable(assessment)
        self._validate_answers(assessment, {item_no: option})

        existing = self.answer_keys.item(assessment_id, item_no)
        if existing is None:
            return self.answer_keys.add(
                AnswerKey(assessment_id=assessment_id, item_no=item_no, correct_option=option)
            )
        return self.answer_keys.update(existing, correct_option=option)

    def get_answer_key(self, assessment_id: uuid.UUID) -> list[AnswerKey]:
        return self.answer_keys.for_assessment(assessment_id)

    def lock_answer_key(self, assessment: Assessment) -> Assessment:
        """Called by the scanner the first time a sheet is read."""
        if assessment.key_locked_at is not None:
            return assessment
        return self.assessments.update(
            assessment,
            key_locked_at=datetime.now(timezone.utc),
            status=AssessmentStatus.in_progress,
        )

    def key_is_complete(self, assessment: Assessment) -> bool:
        return len(self.answer_keys.for_assessment(assessment.id)) == assessment.item_count

    # --- helpers ----------------------------------------------------------
    def _unique_code(self) -> str:
        for _ in range(MAX_CODE_ATTEMPTS):
            code = "".join(secrets.choice(TEST_CODE_ALPHABET) for _ in range(TEST_CODE_LENGTH))
            if self.assessments.by_code(code) is None:
                return code
        raise ConflictError("Could not allocate a unique test code. Try again.")

    @staticmethod
    def _guard_key_mutable(assessment: Assessment) -> None:
        if assessment.key_locked_at is not None:
            raise ConflictError(
                "The answer key is locked because sheets have already been scanned."
            )

    @staticmethod
    def _validate_answers(assessment: Assessment, answers: dict[int, str]) -> None:
        allowed = set(assessment.options)
        for item_no, option in answers.items():
            if not 1 <= item_no <= assessment.item_count:
                raise ValidationError(
                    f"Item {item_no} is outside this {assessment.item_count}-item assessment."
                )
            if option not in allowed:
                raise ValidationError(
                    f"Item {item_no}: '{option}' is not one of {assessment.options}."
                )
