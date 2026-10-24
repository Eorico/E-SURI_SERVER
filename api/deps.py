"""Dependency wiring.

Every service is built here from repositories, so routes depend on an
abstraction and never construct their own collaborators (DIP). Swapping a
component — an in-memory object store for tests, a different scanner — means
changing one provider.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends
from sqlalchemy.orm import Session

from api.core.config import settings
from api.core.database import get_db
from api.repositories.analysis_repository import (
    CertificateRepository,
    EqwartReportRepository,
    ItemAnalysisRepository,
    LeastMasteredRepository,
)
from api.repositories.assessment_repository import (
    AnswerKeyRepository,
    AnswerSheetRepository,
    AssessmentRepository,
)
from api.repositories.audit_repository import AuditRepository
from api.repositories.class_repository import ClassRepository
from api.repositories.profile_repository import ProfileRepository
from api.repositories.school_repository import (
    LearningAreaRepository,
    SchoolRepository,
    SectionRepository,
)
from api.repositories.student_repository import EnrollmentRepository, StudentRepository
from api.repositories.template_repository import ReportTemplateRepository
from api.repositories.submission_repository import ResponseRepository, SubmissionRepository
from api.services.analysis_service import AnalysisService
from api.services.assessment_service import AssessmentService
from api.services.auth_service import AuthService, SupabaseAuthGateway
from api.services.certificate_service import CertificateService
from api.services.class_service import ClassService
from api.services.eqwart_service import EqwartService
from api.services.ai.template_mapper import OpenAIMapperGateway, TemplateMapperService
from api.services.scan_service import ScanService
from api.services.template_service import TemplateService
from api.services.storage_service import (
    InMemoryObjectStore,
    StorageService,
    SupabaseObjectStore,
)
from utils.omr.scanner import build_scanner


# --------------------------------------------------------------------------- #
# Process-wide singletons                                                      #
# --------------------------------------------------------------------------- #
@lru_cache
def get_scanner():
    """One scanner per process; it holds no per-request state."""
    return build_scanner()


@lru_cache
def get_storage() -> StorageService:
    """Supabase when configured, otherwise an in-memory store for local runs."""
    configured = bool(settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY)
    store = SupabaseObjectStore() if configured else InMemoryObjectStore()
    return StorageService(store)


@lru_cache
def get_template_mapper() -> TemplateMapperService:
    """AI mapping when a key is configured; the heuristic analyser otherwise."""
    return TemplateMapperService(gateway=OpenAIMapperGateway())


# --------------------------------------------------------------------------- #
# Services                                                                     #
# --------------------------------------------------------------------------- #
def get_class_service(db: Session = Depends(get_db)) -> ClassService:
    return ClassService(
        classes=ClassRepository(db),
        sections=SectionRepository(db),
        learning_areas=LearningAreaRepository(db),
        students=StudentRepository(db),
        enrollments=EnrollmentRepository(db),
    )


def get_assessment_service(db: Session = Depends(get_db)) -> AssessmentService:
    return AssessmentService(
        assessments=AssessmentRepository(db),
        answer_keys=AnswerKeyRepository(db),
        classes=ClassRepository(db),
    )


def get_analysis_service(db: Session = Depends(get_db)) -> AnalysisService:
    return AnalysisService(
        assessments=AssessmentRepository(db),
        submissions=SubmissionRepository(db),
        answer_keys=AnswerKeyRepository(db),
        item_analysis=ItemAnalysisRepository(db),
    )


def get_scan_service(db: Session = Depends(get_db)) -> ScanService:
    return ScanService(
        assessments=AssessmentRepository(db),
        assessment_service=get_assessment_service(db),
        submissions=SubmissionRepository(db),
        responses=ResponseRepository(db),
        answer_keys=AnswerKeyRepository(db),
        students=StudentRepository(db),
        classes=ClassRepository(db),
        scanner=get_scanner(),
        storage=get_storage(),
    )


def get_certificate_service(db: Session = Depends(get_db)) -> CertificateService:
    return CertificateService(
        submissions=SubmissionRepository(db),
        certificates=CertificateRepository(db),
        classes=ClassRepository(db),
    )


def get_auth_service(db: Session = Depends(get_db)) -> AuthService:
    return AuthService(
        gateway=SupabaseAuthGateway(),
        profiles=ProfileRepository(db),
        schools=SchoolRepository(db),
        learning_areas=LearningAreaRepository(db),
    )


def get_eqwart_service(db: Session = Depends(get_db)) -> EqwartService:
    return EqwartService(
        reports=EqwartReportRepository(db),
        least_mastered=LeastMasteredRepository(db),
        learning_areas=LearningAreaRepository(db),
        schools=SchoolRepository(db),
        assessments=AssessmentRepository(db),
        submissions=SubmissionRepository(db),
        item_analysis=ItemAnalysisRepository(db),
        analysis=get_analysis_service(db),
        templates=get_template_service(db),
    )


# --------------------------------------------------------------------------- #
# Repositories used directly by read-only endpoints                            #
# --------------------------------------------------------------------------- #
def get_template_service(db: Session = Depends(get_db)) -> TemplateService:
    return TemplateService(
        templates=ReportTemplateRepository(db),
        mapper=get_template_mapper(),
        storage=get_storage(),
    )


def get_profiles(db: Session = Depends(get_db)) -> ProfileRepository:
    return ProfileRepository(db)


def get_schools(db: Session = Depends(get_db)) -> SchoolRepository:
    return SchoolRepository(db)


def get_learning_areas(db: Session = Depends(get_db)) -> LearningAreaRepository:
    return LearningAreaRepository(db)


def get_sections(db: Session = Depends(get_db)) -> SectionRepository:
    return SectionRepository(db)


def get_students(db: Session = Depends(get_db)) -> StudentRepository:
    return StudentRepository(db)


def get_submissions(db: Session = Depends(get_db)) -> SubmissionRepository:
    return SubmissionRepository(db)


def get_answer_sheets(db: Session = Depends(get_db)) -> AnswerSheetRepository:
    return AnswerSheetRepository(db)


def get_audit(db: Session = Depends(get_db)) -> AuditRepository:
    return AuditRepository(db)
