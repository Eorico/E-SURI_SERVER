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
from api.repositories.base import EmbeddedRepository, MongoRepository, Repository
from api.repositories.class_repository import ClassRepository
from api.repositories.profile_repository import (
    CoordinatorAssignmentRepository,
    ProfileRepository,
)
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
    "CoordinatorAssignmentRepository",
    "EmbeddedRepository",
    "EnrollmentRepository",
    "EqwartReportRepository",
    "ItemAnalysisRepository",
    "LearningAreaRepository",
    "LeastMasteredRepository",
    "MongoRepository",
    "ProfileRepository",
    "ReportTemplateRepository",
    "Repository",
    "ResponseRepository",
    "SchoolRepository",
    "SectionRepository",
    "StudentRepository",
    "SubmissionRepository",
]
