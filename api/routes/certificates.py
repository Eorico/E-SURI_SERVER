"""Certificates: eligibility is visible to teachers, release is admin-only."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from api.core.security import Principal, require_admin, require_teacher
from api.deps import get_audit, get_certificate_service, get_class_service
from api.repositories.audit_repository import AuditRepository
from api.services.certificate_service import CertificateService
from api.services.class_service import ClassService
from dto.schemas import (
    CertificateEligibility,
    CertificateOut,
    ReleaseCertificatesRequest,
)

router = APIRouter(prefix="/classes/{class_id}/certificates", tags=["certificates"])


@router.get("/eligibility", response_model=list[CertificateEligibility])
def eligibility(
    class_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    service: CertificateService = Depends(get_certificate_service),
    classes: ClassService = Depends(get_class_service),
) -> list[CertificateEligibility]:
    """Who qualifies for Completion, and what the rest are still missing."""
    classes.get_for(principal, class_id)
    return [
        CertificateEligibility(
            student_id=entry.student.id,
            student_name=entry.student.full_name,
            eligible_type=entry.eligible_type,
            approved_types=sorted(entry.approved_types, key=lambda t: t.value),
            missing_types=sorted(entry.missing_types, key=lambda t: t.value),
        )
        for entry in service.eligibility_for_class(class_id)
    ]


@router.get("", response_model=list[CertificateOut])
def list_certificates(
    class_id: uuid.UUID,
    principal: Principal = Depends(require_teacher),
    service: CertificateService = Depends(get_certificate_service),
    classes: ClassService = Depends(get_class_service),
) -> list[CertificateOut]:
    classes.get_for(principal, class_id)
    return [CertificateOut.model_validate(c) for c in service.certificates.for_class(class_id)]


@router.post("/release", response_model=list[CertificateOut])
def release(
    class_id: uuid.UUID,
    payload: ReleaseCertificatesRequest,
    principal: Principal = Depends(require_admin),
    service: CertificateService = Depends(get_certificate_service),
    audit: AuditRepository = Depends(get_audit),
) -> list[CertificateOut]:
    """Administrator action, so it is recorded in the audit trail."""
    released = service.release_for_class(
        class_id=class_id, issued_by=principal.id, student_ids=payload.student_ids
    )
    audit.record(
        actor_id=principal.id,
        action="certificates.release",
        entity_type="class",
        entity_id=class_id,
        metadata={"count": len(released)},
    )
    return [CertificateOut.model_validate(c) for c in released]
