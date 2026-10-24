"""Repositories: the only layer that talks to the database."""

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
from api.repositories.base import Repository, SqlRepository
from api.repositories.class_repository import ClassRepository
from api.repositories.profile_repository import ProfileRepository
from api.repositories.school_repository import (
    LearningAreaRepository,
    SchoolRepository,
    SectionRepository,
)
from api.repositories.template_repository import ReportTemplateRepository
from api.repositories.student_repository import EnrollmentRepository, StudentRepository
from api.repositories.submission_repository import ResponseRepository, SubmissionRepository

__all__ = [
    "AnswerKeyRepository",
    "AnswerSheetRepository",
    "AssessmentRepository",
    "AuditRepository",
    "CertificateRepository",
    "ClassRepository",
    "EnrollmentRepository",
    "EqwartReportRepository",
    "ItemAnalysisRepository",
    "LearningAreaRepository",
    "LeastMasteredRepository",
    "ProfileRepository",
    "ReportTemplateRepository",
    "Repository",
    "ResponseRepository",
    "SchoolRepository",
    "SectionRepository",
    "SqlRepository",
    "StudentRepository",
    "SubmissionRepository",
]
