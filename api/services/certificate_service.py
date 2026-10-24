"""Certificate eligibility and release.

A learner earns a **Completion** certificate only when every required assessment
has an approved submission; otherwise **Participation**. Only an administrator
releases them, which the route enforces.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from api.core.exceptions import ConflictError, NotFoundError
from api.models.enums import (
    REQUIRED_FOR_COMPLETION,
    AssessmentType,
    CertificateStatus,
    CertificateType,
)
from api.models.tables import Certificate, Student
from api.repositories.analysis_repository import CertificateRepository
from api.repositories.class_repository import ClassRepository
from api.repositories.submission_repository import SubmissionRepository


@dataclass(frozen=True)
class Eligibility:
    student: Student
    eligible_type: CertificateType
    approved_types: set[AssessmentType]

    @property
    def missing_types(self) -> set[AssessmentType]:
        return set(REQUIRED_FOR_COMPLETION) - self.approved_types


class CertificateService:
    def __init__(
        self,
        submissions: SubmissionRepository,
        certificates: CertificateRepository,
        classes: ClassRepository,
        renderer=None,
        storage=None,
    ):
        self.submissions = submissions
        self.certificates = certificates
        self.classes = classes
        self.renderer = renderer
        self.storage = storage

    # --- eligibility ------------------------------------------------------
    @staticmethod
    def decide_type(approved_types: set[AssessmentType]) -> CertificateType:
        """Completion needs all five; anything less is Participation."""
        complete = REQUIRED_FOR_COMPLETION <= approved_types
        return CertificateType.completion if complete else CertificateType.participation

    def eligibility_for_class(self, class_id: uuid.UUID) -> list[Eligibility]:
        if self.classes.get(class_id) is None:
            raise NotFoundError("That class does not exist.")

        return [
            Eligibility(
                student=student,
                approved_types=(
                    approved := self.submissions.approved_types_for_student(student.id, class_id)
                ),
                eligible_type=self.decide_type(approved),
            )
            for student in self.classes.roster(class_id)
        ]

    # --- release ----------------------------------------------------------
    def release_for_class(
        self,
        *,
        class_id: uuid.UUID,
        issued_by: uuid.UUID,
        student_ids: list[uuid.UUID] | None = None,
    ) -> list[Certificate]:
        """Issue or refresh a certificate for each eligible learner."""
        wanted = set(student_ids) if student_ids else None
        released: list[Certificate] = []

        for entry in self.eligibility_for_class(class_id):
            if wanted is not None and entry.student.id not in wanted:
                continue
            released.append(self._release_one(entry, class_id, issued_by))
        return released

    def _release_one(
        self, entry: Eligibility, class_id: uuid.UUID, issued_by: uuid.UUID
    ) -> Certificate:
        existing = self.certificates.for_student(entry.student.id, class_id)
        now = datetime.now(timezone.utc)

        if existing is None:
            certificate = self.certificates.add(
                Certificate(
                    student_id=entry.student.id,
                    class_id=class_id,
                    type=entry.eligible_type,
                    status=CertificateStatus.released,
                    issued_by=issued_by,
                    issued_at=now,
                )
            )
        else:
            certificate = self.certificates.update(
                existing,
                type=entry.eligible_type,
                status=CertificateStatus.released,
                issued_by=issued_by,
                issued_at=now,
            )

        if self.renderer is not None and self.storage is not None:
            pdf = self.renderer.render(
                student_name=entry.student.full_name,
                cert_type=entry.eligible_type.value,
                issued_on=now.date().isoformat(),
            )
            path = self.storage.upload_certificate(
                class_id=class_id, certificate_id=certificate.id, data=pdf
            )
            certificate = self.certificates.update(certificate, pdf_path=path)

        return certificate

    def revoke(self, certificate_id: uuid.UUID) -> Certificate:
        certificate = self.certificates.get(certificate_id)
        if certificate is None:
            raise NotFoundError("That certificate does not exist.")
        if certificate.status is CertificateStatus.revoked:
            raise ConflictError("That certificate is already revoked.")
        return self.certificates.update(certificate, status=CertificateStatus.revoked)
