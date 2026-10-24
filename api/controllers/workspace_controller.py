"""Controllers for the teacher workspace.

Controllers do HTTP only: they take validated DTOs, call one service, and map
the result back to a response DTO. No business rule lives here.
"""

from __future__ import annotations

import uuid

from api.core.security import Principal
from api.models.enums import AssessmentType
from api.services.analysis_service import AnalysisService
from api.services.assessment_service import AssessmentService
from api.services.class_service import ClassService
from api.services.scan_service import ScanService, ScoredPaper
from dto.schemas import (
    AnswerKeyOut,
    AnswerKeyUpsert,
    AssessmentCreate,
    AssessmentOut,
    ClassCreate,
    ClassOut,
    EnrollRequest,
    ItemAnalysisOut,
    ResolveFlagsRequest,
    ResponseOut,
    ScanResultOut,
    ScoreSummaryOut,
    StudentOut,
    SubmissionOut,
)


class ClassController:
    def __init__(self, service: ClassService):
        self.service = service

    def list(self, principal: Principal) -> list[ClassOut]:
        return [ClassOut.model_validate(item) for item in self.service.visible_to(principal)]

    def get(self, principal: Principal, class_id: uuid.UUID) -> ClassOut:
        return ClassOut.model_validate(self.service.get_for(principal, class_id))

    def create(self, principal: Principal, payload: ClassCreate) -> ClassOut:
        record = self.service.create(
            principal,
            section_id=payload.section_id,
            learning_area_id=payload.learning_area_id,
            school_year=payload.school_year,
        )
        return ClassOut.model_validate(self.service.get_for(principal, record.id))

    def roster(self, principal: Principal, class_id: uuid.UUID) -> list[StudentOut]:
        return [StudentOut.model_validate(s) for s in self.service.roster(principal, class_id)]

    def enroll(
        self, principal: Principal, class_id: uuid.UUID, payload: EnrollRequest
    ) -> list[StudentOut]:
        self.service.enroll(principal, class_id, payload.student_ids)
        return self.roster(principal, class_id)


class AssessmentController:
    def __init__(self, service: AssessmentService, classes: ClassService):
        self.service = service
        self.classes = classes

    def list_for_class(self, principal: Principal, class_id: uuid.UUID) -> list[AssessmentOut]:
        self.classes.get_for(principal, class_id)
        return [AssessmentOut.model_validate(a) for a in self.service.list_for_class(class_id)]

    def get(self, principal: Principal, assessment_id: uuid.UUID) -> AssessmentOut:
        assessment = self.service.get(assessment_id)
        self.classes.get_for(principal, assessment.class_id)
        return AssessmentOut.model_validate(assessment)

    def create(self, principal: Principal, payload: AssessmentCreate) -> AssessmentOut:
        record = self.classes.get_for(principal, payload.class_id)
        self.classes.assert_can_edit(principal, record)
        assessment = self.service.create(
            class_id=payload.class_id,
            assessment_type=AssessmentType(payload.type),
            title=payload.title,
            date=payload.date,
            created_by=principal.id,
        )
        return AssessmentOut.model_validate(assessment)

    def get_answer_key(self, principal: Principal, assessment_id: uuid.UUID) -> list[AnswerKeyOut]:
        assessment = self.service.get(assessment_id)
        self.classes.get_for(principal, assessment.class_id)
        return [AnswerKeyOut.model_validate(k) for k in self.service.get_answer_key(assessment_id)]

    def set_answer_key(
        self, principal: Principal, assessment_id: uuid.UUID, payload: AnswerKeyUpsert
    ) -> list[AnswerKeyOut]:
        assessment = self.service.get(assessment_id)
        record = self.classes.get_for(principal, assessment.class_id)
        self.classes.assert_can_edit(principal, record)

        answers = {item.item_no: item.correct_option.upper() for item in payload.answers}
        return [
            AnswerKeyOut.model_validate(k)
            for k in self.service.set_answer_key(assessment_id, answers)
        ]


class ScanController:
    def __init__(self, service: ScanService, assessments: AssessmentService, classes: ClassService):
        self.service = service
        self.assessments = assessments
        self.classes = classes

    def scan(
        self,
        principal: Principal,
        assessment_id: uuid.UUID,
        data: bytes,
        content_type: str,
    ) -> ScanResultOut:
        assessment = self.assessments.get(assessment_id)
        record = self.classes.get_for(principal, assessment.class_id)
        self.classes.assert_can_edit(principal, record)

        result = self.service.process(
            assessment_id=assessment_id,
            image_bytes=data,
            scanned_by=principal.id,
            content_type=content_type,
        )
        return self._to_dto(result)

    def resolve(
        self, principal: Principal, submission_id: uuid.UUID, payload: ResolveFlagsRequest
    ) -> ScanResultOut:
        result = self.service.resolve_flagged(
            submission_id=submission_id,
            decisions={k: (v.upper() if v else None) for k, v in payload.decisions.items()},
            resolved_by=principal.id,
        )
        return self._to_dto(result)

    def approve(self, principal: Principal, submission_id: uuid.UUID) -> SubmissionOut:
        return SubmissionOut.model_validate(self.service.approve(submission_id, principal.id))

    @staticmethod
    def _to_dto(result: ScoredPaper) -> ScanResultOut:
        return ScanResultOut(
            submission=SubmissionOut.model_validate(result.submission),
            student=StudentOut.model_validate(result.student),
            score=result.score,
            possible=result.possible,
            percent=result.percent,
            flagged_items=result.flagged_items,
            responses=[ResponseOut.model_validate(r) for r in result.responses],
        )


class AnalysisController:
    def __init__(
        self, service: AnalysisService, assessments: AssessmentService, classes: ClassService
    ):
        self.service = service
        self.assessments = assessments
        self.classes = classes

    def items(self, principal: Principal, assessment_id: uuid.UUID) -> list[ItemAnalysisOut]:
        self._authorise(principal, assessment_id)
        rows = self.service.recompute(assessment_id)
        return [ItemAnalysisOut.model_validate(row) for row in rows]

    def summary(self, principal: Principal, assessment_id: uuid.UUID) -> ScoreSummaryOut:
        self._authorise(principal, assessment_id)
        summary = self.service.summary(assessment_id)
        return ScoreSummaryOut(
            paper_count=summary.paper_count,
            item_count=summary.item_count,
            possible_points=summary.possible_points,
            minimum=summary.minimum,
            maximum=summary.maximum,
            mean=summary.mean,
            median=summary.median,
            stdev=summary.stdev,
            mps=summary.mps,
            at_or_above_75=summary.at_or_above_75,
            below_75=summary.below_75,
            band_counts={level.value: count for level, count in summary.band_counts.items()},
        )

    def least_mastered(self, principal: Principal, assessment_id: uuid.UUID) -> list[ItemAnalysisOut]:
        self._authorise(principal, assessment_id)
        return [
            ItemAnalysisOut.model_validate(row)
            for row in self.service.least_mastered(assessment_id)
        ]

    def _authorise(self, principal: Principal, assessment_id: uuid.UUID) -> None:
        assessment = self.assessments.get(assessment_id)
        self.classes.get_for(principal, assessment.class_id)
