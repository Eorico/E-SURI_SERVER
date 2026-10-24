"""Request and response DTOs for every endpoint.

Validation lives in the field constraints, so a malformed or oversized payload
is rejected before any service or query sees it.
"""

from __future__ import annotations

import re
import uuid
# `date` is aliased because two DTOs expose a field of that name, which
# would otherwise shadow the type in their own annotations.
from datetime import date as date_type, datetime

from pydantic import EmailStr, Field, field_validator

from api.models.enums import (
    AssessmentStatus,
    AssessmentType,
    CertificateStatus,
    CertificateType,
    InstructionalAssistance,
    InterventionActivity,
    InterventionMode,
    MappingSource,
    MasteryLevel,
    ReportStatus,
    SubmissionStatus,
    TemplateStatus,
    UserRole,
)
from dto.common import DTO, ORMDto

#: Usernames and codes are restricted so they can never carry markup or SQL.
SAFE_SLUG = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_NAME = re.compile(r"^[\w\s.,'()\-ÑñÁÉÍÓÚáéíóú]+$", re.UNICODE)


# --------------------------------------------------------------------------- #
# Auth                                                                         #
# --------------------------------------------------------------------------- #
class RegisterRequest(DTO):
    full_name: str = Field(min_length=2, max_length=160)
    username: str = Field(min_length=3, max_length=64)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    role: UserRole = UserRole.subject_teacher
    school_id: uuid.UUID | None = None
    learning_area_id: uuid.UUID | None = None

    @field_validator("username")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SAFE_SLUG.match(value):
            raise ValueError("Use letters, numbers, dot, dash or underscore only")
        return value.lower()

    @field_validator("full_name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not SAFE_NAME.match(value):
            raise ValueError("That name contains characters we cannot accept")
        return value

    @field_validator("password")
    @classmethod
    def _strength(cls, value: str) -> str:
        if not any(ch.isdigit() for ch in value):
            raise ValueError("Include at least one number")
        if not any(not ch.isalnum() for ch in value):
            raise ValueError("Include at least one symbol")
        return value


class LoginRequest(DTO):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(DTO):
    refresh_token: str = Field(min_length=10, max_length=2048)


class PasswordResetRequest(DTO):
    email: EmailStr


class ProfileOut(ORMDto):
    id: uuid.UUID
    role: UserRole
    full_name: str | None = None
    username: str | None = None
    email: str | None = None
    phone: str | None = None
    is_active: bool
    school_id: uuid.UUID | None = None
    learning_area_id: uuid.UUID | None = None


class ProfileUpdate(DTO):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    phone: str | None = Field(default=None, max_length=32)


class SessionOut(ORMDto):
    """What the browser stores after signing in or refreshing.

    The tokens come from Supabase but reach the client through this API, which
    is the only party holding Supabase credentials. The profile rides along so
    the app can render the right workspace without a second round trip.
    """

    profile: ProfileOut
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


# --------------------------------------------------------------------------- #
# Organisation                                                                 #
# --------------------------------------------------------------------------- #
class SchoolOut(ORMDto):
    id: uuid.UUID
    name: str
    division: str | None = None
    cluster: str | None = None
    school_year: str


class LearningAreaOut(ORMDto):
    id: uuid.UUID
    code: str
    name: str
    sheet_name: str
    grade_level: int


class SectionOut(ORMDto):
    id: uuid.UUID
    name: str
    grade_level: int
    school_year: str
    position: int


class StudentOut(ORMDto):
    id: uuid.UUID
    student_no: str
    full_name: str


class ClassCreate(DTO):
    section_id: uuid.UUID
    learning_area_id: uuid.UUID
    school_year: str = Field(min_length=4, max_length=16)


class ClassOut(ORMDto):
    id: uuid.UUID
    class_code: str
    school_year: str
    teacher_id: uuid.UUID
    section: SectionOut
    learning_area: LearningAreaOut
    created_at: datetime


class EnrollRequest(DTO):
    student_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


# --------------------------------------------------------------------------- #
# Assessments                                                                  #
# --------------------------------------------------------------------------- #
class AssessmentCreate(DTO):
    class_id: uuid.UUID
    type: AssessmentType
    title: str | None = Field(default=None, max_length=160)
    date: date_type | None = None


class AssessmentOut(ORMDto):
    id: uuid.UUID
    title: str
    type: AssessmentType
    class_id: uuid.UUID
    item_count: int
    test_code: str
    options: str
    date: date_type | None = None
    status: AssessmentStatus
    key_locked_at: datetime | None = None
    created_at: datetime


class AnswerKeyItem(DTO):
    item_no: int = Field(ge=1, le=100)
    correct_option: str = Field(min_length=1, max_length=1)
    competency_id: uuid.UUID | None = None


class AnswerKeyUpsert(DTO):
    answers: list[AnswerKeyItem] = Field(min_length=1, max_length=100)


class AnswerKeyOut(ORMDto):
    item_no: int
    correct_option: str
    points: int
    competency_id: uuid.UUID | None = None


class SheetRequest(DTO):
    student_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)


# --------------------------------------------------------------------------- #
# Scanning                                                                     #
# --------------------------------------------------------------------------- #
class ResponseOut(ORMDto):
    item_no: int
    marked_option: str | None = None
    detected_option: str | None = None
    is_correct: bool
    flag: str


class SubmissionOut(ORMDto):
    id: uuid.UUID
    assessment_id: uuid.UUID
    student_id: uuid.UUID
    score: int | None = None
    status: SubmissionStatus
    flagged_count: int
    scanned_at: datetime | None = None


class ScanResultOut(ORMDto):
    submission: SubmissionOut
    student: StudentOut
    score: int
    possible: int
    percent: float
    flagged_items: list[int]
    responses: list[ResponseOut]


class ResolveFlagsRequest(DTO):
    #: item number -> chosen option, or null to keep it blank.
    decisions: dict[int, str | None] = Field(min_length=1, max_length=100)

    @field_validator("decisions")
    @classmethod
    def _options(cls, value: dict[int, str | None]) -> dict[int, str | None]:
        for item_no, choice in value.items():
            if item_no < 1 or item_no > 100:
                raise ValueError(f"Item {item_no} is out of range")
            if choice is not None and (len(choice) != 1 or not choice.isalpha()):
                raise ValueError(f"Item {item_no}: expected a single letter or null")
        return value


# --------------------------------------------------------------------------- #
# Analysis                                                                     #
# --------------------------------------------------------------------------- #
class ItemAnalysisOut(ORMDto):
    item_no: int
    correct_count: int
    attempted_count: int
    percent_correct: float | None = None
    rank: int | None = None
    discrimination_index: float | None = None
    mastery_level: MasteryLevel | None = None
    instructional_assistance: InstructionalAssistance | None = None
    distribution: dict[str, int] | None = None


class ScoreSummaryOut(ORMDto):
    paper_count: int
    item_count: int
    possible_points: int
    minimum: int
    maximum: int
    mean: float
    median: float
    stdev: float
    mps: float
    at_or_above_75: int
    below_75: int
    band_counts: dict[str, int]


# --------------------------------------------------------------------------- #
# EQWART                                                                       #
# --------------------------------------------------------------------------- #
class LeastMasteredIn(DTO):
    position: int = Field(ge=1, le=5)
    competency_text: str = Field(min_length=3, max_length=1000)
    competency_id: uuid.UUID | None = None


class LeastMasteredOut(ORMDto):
    position: int
    rank_value: int | None = None
    item_no: int | None = None
    competency_text: str | None = None
    competency_id: uuid.UUID | None = None


class InterventionIn(DTO):
    activities: list[InterventionActivity] = Field(min_length=1, max_length=6)
    mode: InterventionMode
    other: str | None = Field(default=None, max_length=500)


class EqwartReportOut(ORMDto):
    id: uuid.UUID
    learning_area_id: uuid.UUID
    assessment_type: AssessmentType
    school_year: str
    status: ReportStatus
    key_teacher_name: str | None = None
    reviewed_by_name: str | None = None
    approved_by_name: str | None = None
    intervention_activities: list[str] | None = None
    intervention_mode: InterventionMode | None = None
    intervention_other: str | None = None
    least_mastered: list[LeastMasteredOut] = []


class SignatoriesIn(DTO):
    key_teacher_name: str | None = Field(default=None, max_length=160)
    reviewed_by_name: str | None = Field(default=None, max_length=160)
    approved_by_name: str | None = Field(default=None, max_length=160)


# --------------------------------------------------------------------------- #
# Certificates                                                                 #
# --------------------------------------------------------------------------- #
class CertificateOut(ORMDto):
    id: uuid.UUID
    student_id: uuid.UUID
    class_id: uuid.UUID
    type: CertificateType
    status: CertificateStatus
    issued_at: datetime | None = None


class CertificateEligibility(ORMDto):
    student_id: uuid.UUID
    student_name: str
    eligible_type: CertificateType
    approved_types: list[AssessmentType]
    missing_types: list[AssessmentType]


class ReleaseCertificatesRequest(DTO):
    student_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)


# --------------------------------------------------------------------------- #
# Report templates (administrator)                                             #
# --------------------------------------------------------------------------- #
class ReportTemplateOut(ORMDto):
    id: uuid.UUID
    name: str
    original_filename: str
    school_year: str
    grade_level: int
    status: TemplateStatus
    mapping_source: MappingSource
    mapping_confidence: float
    dry_run_passed: bool
    mapping_notes: list[str] | None = None
    activated_at: datetime | None = None
    created_at: datetime


class ReportTemplateDetail(ReportTemplateOut):
    #: The full detected mapping, for review before activation.
    mapping: dict | None = None


class TemplateDryRunOut(ORMDto):
    passed: bool
    problems: list[str]
    sheets_checked: int


class MappingOverrideIn(DTO):
    mapping: dict
