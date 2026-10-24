"""Report templates — administrator only.

DepEd reissues the item-analysis workbook most years. An administrator uploads
the new one here; the server reads its structure, works out where every value
belongs, and proves the mapping by filling a copy with synthetic data before it
can go live. Nothing but the administrator role can reach any of this.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile, status

from api.core.config import settings
from api.core.exceptions import ValidationError
from api.core.security import Principal, require_admin
from api.deps import get_audit, get_template_service
from api.repositories.audit_repository import AuditRepository
from api.services.template_service import TemplateService
from dto.schemas import (
    MappingOverrideIn,
    ReportTemplateDetail,
    ReportTemplateOut,
    TemplateDryRunOut,
)
from utils.omr.template_mapping import TemplateMapping

router = APIRouter(prefix="/admin/report-templates", tags=["report templates"])


@router.get("", response_model=list[ReportTemplateOut])
def list_templates(
    _: Principal = Depends(require_admin),
    service: TemplateService = Depends(get_template_service),
) -> list[ReportTemplateOut]:
    return [ReportTemplateOut.model_validate(t) for t in service.list()]


@router.get("/{template_id}", response_model=ReportTemplateDetail)
def get_template(
    template_id: uuid.UUID,
    _: Principal = Depends(require_admin),
    service: TemplateService = Depends(get_template_service),
) -> ReportTemplateDetail:
    """The full mapping, so an administrator can review what was detected."""
    return ReportTemplateDetail.model_validate(service.get(template_id))


@router.post("", response_model=ReportTemplateDetail, status_code=status.HTTP_201_CREATED)
async def upload_template(
    file: UploadFile = File(..., description="The DepEd .xlsx or .xlsm workbook"),
    school_year: str = Form(..., min_length=4, max_length=16),
    grade_level: int = Form(11, ge=7, le=12),
    name: str | None = Form(None, max_length=160),
    principal: Principal = Depends(require_admin),
    service: TemplateService = Depends(get_template_service),
    audit: AuditRepository = Depends(get_audit),
) -> ReportTemplateDetail:
    """Upload a new template.

    The workbook is mapped and dry-run immediately; the result is a draft, and
    activating it is a separate, deliberate step.
    """
    data = await file.read(settings.MAX_TEMPLATE_BYTES + 1)
    if len(data) > settings.MAX_TEMPLATE_BYTES:
        raise ValidationError(
            f"That workbook is larger than {settings.MAX_TEMPLATE_BYTES // (1024 * 1024)}MB."
        )

    template = service.upload(
        data=data,
        filename=file.filename or "template.xlsx",
        content_type=file.content_type,
        school_year=school_year,
        grade_level=grade_level,
        name=name,
        uploaded_by=principal.id,
    )
    audit.record(
        actor_id=principal.id,
        action="template.upload",
        entity_type="report_template",
        entity_id=template.id,
        metadata={
            "source": template.mapping_source.value,
            "confidence": template.mapping_confidence,
            "dry_run_passed": template.dry_run_passed,
        },
    )
    return ReportTemplateDetail.model_validate(template)


@router.post("/{template_id}/activate", response_model=ReportTemplateOut)
def activate(
    template_id: uuid.UUID,
    principal: Principal = Depends(require_admin),
    service: TemplateService = Depends(get_template_service),
    audit: AuditRepository = Depends(get_audit),
) -> ReportTemplateOut:
    """Make this the template every export uses. Refused unless it dry-ran clean."""
    template = service.activate(template_id, principal.id)
    audit.record(
        actor_id=principal.id,
        action="template.activate",
        entity_type="report_template",
        entity_id=template.id,
    )
    return ReportTemplateOut.model_validate(template)


@router.post("/{template_id}/archive", response_model=ReportTemplateOut)
def archive(
    template_id: uuid.UUID,
    _: Principal = Depends(require_admin),
    service: TemplateService = Depends(get_template_service),
) -> ReportTemplateOut:
    return ReportTemplateOut.model_validate(service.archive(template_id))


@router.post("/{template_id}/remap", response_model=ReportTemplateDetail)
def remap(
    template_id: uuid.UUID,
    _: Principal = Depends(require_admin),
    service: TemplateService = Depends(get_template_service),
) -> ReportTemplateDetail:
    """Re-read the stored workbook — useful after adding an OpenAI key."""
    return ReportTemplateDetail.model_validate(service.remap(template_id))


@router.put("/{template_id}/mapping", response_model=ReportTemplateDetail)
def override_mapping(
    template_id: uuid.UUID,
    payload: MappingOverrideIn,
    principal: Principal = Depends(require_admin),
    service: TemplateService = Depends(get_template_service),
    audit: AuditRepository = Depends(get_audit),
) -> ReportTemplateDetail:
    """Correct a detected mapping by hand. It still has to pass the dry run."""
    mapping = TemplateMapping.from_dict(payload.mapping)
    template = service.override_mapping(template_id, mapping)
    audit.record(
        actor_id=principal.id,
        action="template.override_mapping",
        entity_type="report_template",
        entity_id=template.id,
    )
    return ReportTemplateDetail.model_validate(template)


@router.post("/{template_id}/dry-run", response_model=TemplateDryRunOut)
def dry_run(
    template_id: uuid.UUID,
    _: Principal = Depends(require_admin),
    service: TemplateService = Depends(get_template_service),
) -> TemplateDryRunOut:
    """Re-prove a stored mapping without changing anything."""
    data, mapping = service.workbook_and_mapping(template_id)
    result = service.dry_run(data, mapping)
    return TemplateDryRunOut(
        passed=result.passed, problems=result.problems, sheets_checked=result.sheets_checked
    )
