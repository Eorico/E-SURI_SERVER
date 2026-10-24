"""Scanning answer sheets and reviewing what the scanner flagged."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, UploadFile

from api.controllers.workspace_controller import ScanController
from api.core.config import settings
from api.core.exceptions import ValidationError
from api.core.security import Principal, require_teacher
from api.deps import (
    get_assessment_service,
    get_class_service,
    get_scan_service,
    get_submissions,
)
from api.repositories.submission_repository import SubmissionRepository
from api.services.assessment_service import AssessmentService
from api.services.class_service import ClassService
from api.services.scan_service import ScanService
from dto.schemas import ResolveFlagsRequest, ResponseOut, ScanResultOut, SubmissionOut

router = APIRouter(tags=["scanning"])


def controller(
    service: ScanService = Depends(get_scan_service),
    assessments: AssessmentService = Depends(get_assessment_service),
    classes: ClassService = Depends(get_class_service),
) -> ScanController:
    return ScanController(service, assessments, classes)


@router.post("/assessments/{assessment_id}/scan", response_model=ScanResultOut)
async def scan(
    assessment_id: uuid.UUID,
    image: UploadFile = File(..., description="Photo of the answer sheet"),
    principal: Principal = Depends(require_teacher),
    ctrl: ScanController = Depends(controller),
) -> ScanResultOut:
    """Read one sheet and score it immediately.

    The body cap is enforced by middleware; this reads at most one byte beyond
    the limit so a lying Content-Length cannot be used to exhaust memory.
    """
    data = await image.read(settings.MAX_UPLOAD_BYTES + 1)
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"That image is larger than {settings.MAX_UPLOAD_BYTES // (1024 * 1024)}MB."
        )
    return ctrl.scan(principal, assessment_id, data, image.content_type or "image/jpeg")


@router.get("/assessments/{assessment_id}/submissions", response_model=list[SubmissionOut])
def list_submissions(
    assessment_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    assessments: AssessmentService = Depends(get_assessment_service),
    classes: ClassService = Depends(get_class_service),
    submissions: SubmissionRepository = Depends(get_submissions),
) -> list[SubmissionOut]:
    assessment = assessments.get(assessment_id)
    classes.get_for(principal, assessment.class_id)
    return [SubmissionOut.model_validate(s) for s in submissions.for_assessment(assessment_id)]


@router.get("/submissions/{submission_id}/responses", response_model=list[ResponseOut])
def responses(
    submission_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    assessments: AssessmentService = Depends(get_assessment_service),
    classes: ClassService = Depends(get_class_service),
    submissions: SubmissionRepository = Depends(get_submissions),
) -> list[ResponseOut]:
    submission = submissions.with_responses(submission_id)
    if submission is None:
        raise ValidationError("That submission does not exist.")
    assessment = assessments.get(submission.assessment_id)
    classes.get_for(principal, assessment.class_id)
    return [ResponseOut.model_validate(r) for r in sorted(submission.responses, key=lambda r: r.item_no)]


@router.post("/submissions/{submission_id}/resolve", response_model=ScanResultOut)
def resolve(
    submission_id: uuid.UUID,
    payload: ResolveFlagsRequest,
    principal: Principal = Depends(require_teacher),
    ctrl: ScanController = Depends(controller),
) -> ScanResultOut:
    """Record the teacher's ruling on each flagged item and re-score."""
    return ctrl.resolve(principal, submission_id, payload)


@router.post("/submissions/{submission_id}/approve", response_model=SubmissionOut)
def approve(
    submission_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    ctrl: ScanController = Depends(controller),
) -> SubmissionOut:
    return ctrl.approve(principal, submission_id)
