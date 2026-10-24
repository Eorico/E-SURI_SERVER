"""Scan orchestration.

Composes the OMR pipeline with the repositories: run the scanner, score against
the answer key, persist the submission and its responses, store the image. The
pixel work lives in `utils.omr`; the scoring rules live here (SRP).

A paper is `review` while anything is flagged and only becomes `approved` once a
teacher confirms it, because only approved papers feed the statistics.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from api.core.exceptions import ConflictError, NotFoundError, ValidationError
from api.models.enums import AssessmentStatus, ResponseFlag, SubmissionStatus
from api.models.tables import Assessment, Response, Student, Submission
from api.repositories.assessment_repository import AnswerKeyRepository, AssessmentRepository
from api.repositories.class_repository import ClassRepository
from api.repositories.student_repository import StudentRepository
from api.repositories.submission_repository import ResponseRepository, SubmissionRepository
from api.services.assessment_service import AssessmentService
from utils.omr.scanner import ScanResult, Scanner
from utils.omr.sheet_layout import SheetLayout


@dataclass(frozen=True)
class ScoredPaper:
    """The result the scanning screen shows immediately after a capture."""

    submission: Submission
    student: Student
    score: int
    possible: int
    percent: float
    flagged_items: list[int]
    responses: list[Response]


class ScanService:
    def __init__(
        self,
        assessments: AssessmentRepository,
        assessment_service: AssessmentService,
        submissions: SubmissionRepository,
        responses: ResponseRepository,
        answer_keys: AnswerKeyRepository,
        students: StudentRepository,
        classes: ClassRepository,
        scanner: Scanner,
        storage,
    ):
        self.assessments = assessments
        self.assessment_service = assessment_service
        self.submissions = submissions
        self.responses = responses
        self.answer_keys = answer_keys
        self.students = students
        self.classes = classes
        self.scanner = scanner
        self.storage = storage

    # --- scanning ---------------------------------------------------------
    def process(
        self,
        *,
        assessment_id: uuid.UUID,
        image_bytes: bytes,
        scanned_by: uuid.UUID,
        content_type: str = "image/jpeg",
    ) -> ScoredPaper:
        assessment = self._require_assessment(assessment_id)
        key = self.answer_keys.as_map(assessment_id)
        if len(key) != assessment.item_count:
            raise ConflictError(
                "Finish the answer key before scanning: "
                f"{len(key)} of {assessment.item_count} items are set."
            )

        layout = SheetLayout.for_item_count(assessment.item_count, assessment.options)
        result = self.scanner.scan(image_bytes, layout)
        self._guard_sheet_matches(assessment, result)

        student = self._resolve_student(assessment, result)
        submission = self._upsert_submission(assessment, student, scanned_by)

        rows = self._score(result, key, submission.id)
        self.responses.replace_for_submission(submission.id, rows)

        score = sum(1 for row in rows if row.is_correct)
        flagged = [row.item_no for row in rows if row.flag is not ResponseFlag.none]

        path = self.storage.upload_scan(
            assessment_id=assessment_id,
            submission_id=submission.id,
            data=image_bytes,
            content_type=content_type,
        )

        submission = self.submissions.update(
            submission,
            image_path=path,
            score=score,
            flagged_count=len(flagged),
            status=SubmissionStatus.review if flagged else SubmissionStatus.approved,
            scanned_at=datetime.now(timezone.utc),
            scanned_by=scanned_by,
            approved_at=None if flagged else datetime.now(timezone.utc),
            approved_by=None if flagged else scanned_by,
        )

        # The key is frozen from the first scan onwards.
        self.assessment_service.lock_answer_key(assessment)

        return ScoredPaper(
            submission=submission,
            student=student,
            score=score,
            possible=assessment.item_count,
            percent=round(score / assessment.item_count * 100, 2) if assessment.item_count else 0.0,
            flagged_items=flagged,
            responses=rows,
        )

    # --- review -----------------------------------------------------------
    def resolve_flagged(
        self,
        *,
        submission_id: uuid.UUID,
        decisions: dict[int, str | None],
        resolved_by: uuid.UUID,
    ) -> ScoredPaper:
        """Apply a teacher's rulings on flagged items and re-score the paper.

        `None` means "leave it blank", which scores zero. The scanner's original
        reading is kept in `detected_option` so the change stays auditable.
        """
        submission = self.submissions.with_responses(submission_id)
        if submission is None:
            raise NotFoundError("That submission does not exist.")

        assessment = self._require_assessment(submission.assessment_id)
        key = self.answer_keys.as_map(assessment.id)
        allowed = set(assessment.options)

        by_item = {row.item_no: row for row in submission.responses}
        for item_no, choice in decisions.items():
            row = by_item.get(item_no)
            if row is None:
                raise ValidationError(f"Item {item_no} is not part of this paper.")
            if choice is not None and choice not in allowed:
                raise ValidationError(f"Item {item_no}: '{choice}' is not a valid option.")

            if row.detected_option is None:
                row.detected_option = row.marked_option
            row.marked_option = choice
            row.flag = ResponseFlag.none
            row.is_correct = choice is not None and key.get(item_no) == choice

        rows = list(by_item.values())
        score = sum(1 for row in rows if row.is_correct)
        remaining = [row.item_no for row in rows if row.flag is not ResponseFlag.none]

        submission = self.submissions.update(
            submission,
            score=score,
            flagged_count=len(remaining),
            status=SubmissionStatus.review if remaining else SubmissionStatus.approved,
            approved_at=None if remaining else datetime.now(timezone.utc),
            approved_by=None if remaining else resolved_by,
        )

        return ScoredPaper(
            submission=submission,
            student=submission.student,
            score=score,
            possible=assessment.item_count,
            percent=round(score / assessment.item_count * 100, 2) if assessment.item_count else 0.0,
            flagged_items=remaining,
            responses=rows,
        )

    def approve(self, submission_id: uuid.UUID, approved_by: uuid.UUID) -> Submission:
        submission = self.submissions.get(submission_id)
        if submission is None:
            raise NotFoundError("That submission does not exist.")
        if submission.status is SubmissionStatus.missing:
            raise ConflictError("That paper has not been scanned yet.")
        return self.submissions.update(
            submission,
            status=SubmissionStatus.approved,
            flagged_count=0,
            approved_at=datetime.now(timezone.utc),
            approved_by=approved_by,
        )

    # --- helpers ----------------------------------------------------------
    def _score(
        self, result: ScanResult, key: dict[int, str], submission_id: uuid.UUID
    ) -> list[Response]:
        """One point per item; blank and multiple marks score zero and flag."""
        rows: list[Response] = []
        for item in result.items:
            flag = ResponseFlag(item.flag)
            correct = (
                flag is ResponseFlag.none
                and item.marked_option is not None
                and key.get(item.item_no) == item.marked_option
            )
            rows.append(
                Response(
                    submission_id=submission_id,
                    item_no=item.item_no,
                    marked_option=item.marked_option,
                    detected_option=item.marked_option,
                    is_correct=correct,
                    flag=flag,
                )
            )
        return rows

    def _guard_sheet_matches(self, assessment: Assessment, result: ScanResult) -> None:
        if result.test_code and result.test_code.upper() != assessment.test_code.upper():
            raise ValidationError(
                "That sheet belongs to a different assessment "
                f"(code {result.test_code}, expected {assessment.test_code})."
            )

    def _resolve_student(self, assessment: Assessment, result: ScanResult) -> Student:
        if not result.student_no:
            raise ValidationError(
                "The student ID could not be read. Re-shade the ID grid and scan again."
            )

        record = self.classes.get_full(assessment.class_id)
        if record is None:
            raise NotFoundError("That class does not exist.")

        student = self.students.by_student_no(record.section.school_id, result.student_no)
        if student is None:
            raise NotFoundError(f"No learner has student number {result.student_no}.")

        roster = {person.id for person in self.classes.roster(assessment.class_id)}
        if student.id not in roster:
            raise ValidationError(f"{student.full_name} is not enrolled in this class.")
        return student

    def _upsert_submission(
        self, assessment: Assessment, student: Student, scanned_by: uuid.UUID
    ) -> Submission:
        existing = self.submissions.for_student(assessment.id, student.id)
        if existing is not None:
            return existing
        return self.submissions.add(
            Submission(
                assessment_id=assessment.id,
                student_id=student.id,
                status=SubmissionStatus.review,
                scanned_by=scanned_by,
            )
        )

    def _require_assessment(self, assessment_id: uuid.UUID) -> Assessment:
        assessment = self.assessments.get(assessment_id)
        if assessment is None:
            raise NotFoundError("That assessment does not exist.")
        if assessment.status is AssessmentStatus.completed:
            raise ConflictError("That assessment is closed.")
        return assessment
