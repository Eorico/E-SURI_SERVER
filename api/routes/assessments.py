"""Assessments, answer keys and printable answer sheets."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status

from api.controllers.workspace_controller import AssessmentController
from api.core.security import Principal, require_teacher
from api.deps import get_assessment_service, get_class_service, get_students
from api.repositories.student_repository import StudentRepository
from api.services.assessment_service import AssessmentService
from api.services.class_service import ClassService
from dto.schemas import (
    AnswerKeyOut,
    AnswerKeyUpsert,
    AssessmentCreate,
    AssessmentOut,
    SheetRequest,
)
from utils.omr.answer_sheet import AnswerSheetRenderer, SheetSubject
from utils.omr.sheet_layout import SheetLayout

router = APIRouter(tags=["assessments"])


def controller(
    service: AssessmentService = Depends(get_assessment_service),
    classes: ClassService = Depends(get_class_service),
) -> AssessmentController:
    return AssessmentController(service, classes)


@router.get("/classes/{class_id}/assessments", response_model=list[AssessmentOut])
def list_for_class(
    class_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    ctrl: AssessmentController = Depends(controller),
) -> list[AssessmentOut]:
    return ctrl.list_for_class(principal, class_id)


@router.post("/assessments", response_model=AssessmentOut, status_code=status.HTTP_201_CREATED)
def create(
    payload: AssessmentCreate,
    principal: Principal = Depends(require_teacher),
    ctrl: AssessmentController = Depends(controller),
) -> AssessmentOut:
    return ctrl.create(principal, payload)


@router.get("/assessments/{assessment_id}", response_model=AssessmentOut)
def get(
    assessment_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    ctrl: AssessmentController = Depends(controller),
) -> AssessmentOut:
    return ctrl.get(principal, assessment_id)


@router.get("/assessments/{assessment_id}/answer-key", response_model=list[AnswerKeyOut])
def get_answer_key(
    assessment_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    ctrl: AssessmentController = Depends(controller),
) -> list[AnswerKeyOut]:
    return ctrl.get_answer_key(principal, assessment_id)


@router.put("/assessments/{assessment_id}/answer-key", response_model=list[AnswerKeyOut])
def set_answer_key(
    assessment_id: uuid.UUID,
    payload: AnswerKeyUpsert,
    principal: Principal = Depends(require_teacher),
    ctrl: AssessmentController = Depends(controller),
) -> list[AnswerKeyOut]:
    return ctrl.set_answer_key(principal, assessment_id, payload)


@router.post(
    "/assessments/{assessment_id}/sheets",
    summary="Generate printable answer sheets",
    response_class=Response,
)
def sheets(
    assessment_id: uuid.UUID,
    payload: SheetRequest,
    principal: Principal = Depends(require_teacher),
    service: AssessmentService = Depends(get_assessment_service),
    classes: ClassService = Depends(get_class_service),
    students: StudentRepository = Depends(get_students),
) -> Response:
    """One personalised, pre-filled sheet per learner, as a single PDF."""
    assessment = service.get(assessment_id)
    record = classes.get_for(principal, assessment.class_id)

    roster = classes.roster(principal, assessment.class_id)
    if payload.student_ids:
        wanted = set(payload.student_ids)
        roster = [student for student in roster if student.id in wanted]

    layout = SheetLayout.for_item_count(assessment.item_count, assessment.options)
    subjects = [
        SheetSubject(
            student_no=student.student_no,
            student_name=student.full_name,
            section_name=record.section.name,
            assessment_title=assessment.title,
            test_code=assessment.test_code,
        )
        for student in roster
    ]
    pdf = AnswerSheetRenderer(layout).render_many(subjects)

    filename = f"{assessment.test_code}-answer-sheets.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
