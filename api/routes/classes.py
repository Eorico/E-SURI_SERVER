"""Classes, rosters and the reference data behind them."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from api.controllers.workspace_controller import ClassController
from api.core.security import Principal, require_teacher
from api.deps import get_class_service, get_learning_areas, get_schools, get_sections
from api.repositories.school_repository import (
    LearningAreaRepository,
    SchoolRepository,
    SectionRepository,
)
from api.services.class_service import ClassService
from dto.schemas import (
    ClassCreate,
    ClassOut,
    EnrollRequest,
    LearningAreaOut,
    SchoolOut,
    SectionOut,
    StudentOut,
)

router = APIRouter(tags=["classes"])


def controller(service: ClassService = Depends(get_class_service)) -> ClassController:
    return ClassController(service)


@router.get("/school", response_model=SchoolOut | None)
def school(
    _: Principal = Depends(require_teacher), schools: SchoolRepository = Depends(get_schools)
) -> SchoolOut | None:
    record = schools.current()
    return SchoolOut.model_validate(record) if record else None


@router.get("/learning-areas", response_model=list[LearningAreaOut])
def learning_areas(
    principal: Principal = Depends(require_teacher),
    areas: LearningAreaRepository = Depends(get_learning_areas),
) -> list[LearningAreaOut]:
    records = (
        areas.ordered(principal.school_id) if principal.school_id else areas.list()
    )
    return [LearningAreaOut.model_validate(area) for area in records]


@router.get("/sections", response_model=list[SectionOut])
def sections(
    school_year: str,
    principal: Principal = Depends(require_teacher),
    repo: SectionRepository = Depends(get_sections),
) -> list[SectionOut]:
    records = (
        repo.ordered(principal.school_id, school_year)
        if principal.school_id
        else repo.list(school_year=school_year)
    )
    return [SectionOut.model_validate(section) for section in records]


@router.get("/classes", response_model=list[ClassOut])
def list_classes(
    principal: Principal = Depends(require_teacher),
    ctrl: ClassController = Depends(controller),
) -> list[ClassOut]:
    return ctrl.list(principal)


@router.post("/classes", response_model=ClassOut, status_code=status.HTTP_201_CREATED)
def create_class(
    payload: ClassCreate,
    principal: Principal = Depends(require_teacher),
    ctrl: ClassController = Depends(controller),
) -> ClassOut:
    return ctrl.create(principal, payload)


@router.get("/classes/{class_id}", response_model=ClassOut)
def get_class(
    class_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    ctrl: ClassController = Depends(controller),
) -> ClassOut:
    return ctrl.get(principal, class_id)


@router.get("/classes/{class_id}/roster", response_model=list[StudentOut])
def roster(
    class_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    ctrl: ClassController = Depends(controller),
) -> list[StudentOut]:
    return ctrl.roster(principal, class_id)


@router.post("/classes/{class_id}/roster", response_model=list[StudentOut])
def enroll(
    class_id: uuid.UUID,
    payload: EnrollRequest,
    principal: Principal = Depends(require_teacher),
    ctrl: ClassController = Depends(controller),
) -> list[StudentOut]:
    return ctrl.enroll(principal, class_id, payload)
