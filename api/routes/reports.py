"""EQWART reports: the Coordinator encodes, the Administrator exports."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response

from api.core.security import Principal, require_admin, require_coordinator
from api.deps import get_eqwart_service
from api.models.enums import AssessmentType, ReportStatus
from api.services.eqwart_service import EqwartService
from dto.schemas import (
    EqwartReportOut,
    InterventionIn,
    LeastMasteredIn,
    SignatoriesIn,
)

router = APIRouter(prefix="/reports/eqwart", tags=["eqwart"])

XLSM_MEDIA_TYPE = "application/vnd.ms-excel.sheet.macroEnabled.12"


@router.post("", response_model=EqwartReportOut)
def open_report(
    learning_area_id: uuid.UUID,
    assessment_type: AssessmentType,
    school_year: str,
    _: Principal = Depends(require_coordinator),
    service: EqwartService = Depends(get_eqwart_service),
) -> EqwartReportOut:
    """Open — or reopen — the report for one learning area and cycle."""
    report = service.get_or_create(learning_area_id, assessment_type, school_year)
    return EqwartReportOut.model_validate(report)


@router.put("/{report_id}/least-mastered", response_model=EqwartReportOut)
def set_least_mastered(
    report_id: uuid.UUID,
    payload: LeastMasteredIn,
    _: Principal = Depends(require_coordinator),
    service: EqwartService = Depends(get_eqwart_service),
) -> EqwartReportOut:
    """Encode the competency behind one of the five weakest items."""
    service.set_least_mastered(
        report_id,
        position=payload.position,
        competency_text=payload.competency_text,
        competency_id=payload.competency_id,
    )
    return EqwartReportOut.model_validate(service.get_report(report_id))


@router.put("/{report_id}/intervention", response_model=EqwartReportOut)
def set_intervention(
    report_id: uuid.UUID,
    payload: InterventionIn,
    _: Principal = Depends(require_coordinator),
    service: EqwartService = Depends(get_eqwart_service),
) -> EqwartReportOut:
    report = service.set_intervention(
        report_id, activities=payload.activities, mode=payload.mode, other=payload.other
    )
    return EqwartReportOut.model_validate(report)


@router.put("/{report_id}/signatories", response_model=EqwartReportOut)
def set_signatories(
    report_id: uuid.UUID,
    payload: SignatoriesIn,
    _: Principal = Depends(require_coordinator),
    service: EqwartService = Depends(get_eqwart_service),
) -> EqwartReportOut:
    report = service.set_signatories(
        report_id,
        key_teacher_name=payload.key_teacher_name,
        reviewed_by_name=payload.reviewed_by_name,
        approved_by_name=payload.approved_by_name,
    )
    return EqwartReportOut.model_validate(report)


@router.post("/{report_id}/status", response_model=EqwartReportOut)
def advance_status(
    report_id: uuid.UUID,
    status: ReportStatus,
    _: Principal = Depends(require_coordinator),
    service: EqwartService = Depends(get_eqwart_service),
) -> EqwartReportOut:
    return EqwartReportOut.model_validate(service.advance_status(report_id, status))


@router.get("/export", response_class=Response, summary="Download the EQWART workbook")
def export_workbook(
    school_year: str,
    _: Principal = Depends(require_admin),
    service: EqwartService = Depends(get_eqwart_service),
) -> Response:
    """Fill the school's template and return it, formulas intact."""
    workbook = service.export_school_year(school_year)
    filename = f"EQWART-{school_year}.xlsm"
    return Response(
        content=workbook,
        media_type=XLSM_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
