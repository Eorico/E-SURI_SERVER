"""Item analysis and assessment statistics."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from api.controllers.workspace_controller import AnalysisController
from api.core.security import Principal, require_teacher
from api.deps import get_analysis_service, get_assessment_service, get_class_service
from api.services.analysis_service import AnalysisService
from api.services.assessment_service import AssessmentService
from api.services.class_service import ClassService
from dto.schemas import ItemAnalysisOut, ScoreSummaryOut

router = APIRouter(prefix="/assessments/{assessment_id}", tags=["analysis"])


def controller(
    service: AnalysisService = Depends(get_analysis_service),
    assessments: AssessmentService = Depends(get_assessment_service),
    classes: ClassService = Depends(get_class_service),
) -> AnalysisController:
    return AnalysisController(service, assessments, classes)


@router.get("/item-analysis", response_model=list[ItemAnalysisOut])
def item_analysis(
    assessment_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    ctrl: AnalysisController = Depends(controller),
) -> list[ItemAnalysisOut]:
    """Per-item correct count, percent, rank, discrimination and mastery."""
    return ctrl.items(principal, assessment_id)


@router.get("/statistics", response_model=ScoreSummaryOut)
def statistics(
    assessment_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    ctrl: AnalysisController = Depends(controller),
) -> ScoreSummaryOut:
    """Papers, min/max, mean, median, SD, MPS and the band counts."""
    return ctrl.summary(principal, assessment_id)


@router.get("/least-mastered", response_model=list[ItemAnalysisOut])
def least_mastered(
    assessment_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    ctrl: AnalysisController = Depends(controller),
) -> list[ItemAnalysisOut]:
    """The five weakest items, which the Coordinator maps to competencies."""
    return ctrl.least_mastered(principal, assessment_id)
