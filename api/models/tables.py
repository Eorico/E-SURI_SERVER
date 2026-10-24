"""SQLAlchemy ORM models for IMECAR.

DRY: PKMixin and TimestampMixin provide the id and audit columns once; every
table reuses them instead of re-declaring the same columns. Shapes follow
`docs/REQUIREMENTS.md` — EQWART is organised as learning area × section, so a
`Class` is the pairing of the two for one school year.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.core.database import Base
from api.models.enums import (
    AssessmentStatus,
    MappingSource,
    TemplateStatus,
    AssessmentType,
    CertificateStatus,
    CertificateType,
    EnrollmentStatus,
    InstructionalAssistance,
    InterventionMode,
    MasteryLevel,
    ReportStatus,
    ResponseFlag,
    SubmissionStatus,
    UserRole,
)


def _enum(python_enum, name: str):
    """Store the enum *value* (not the member name) under a stable type name."""
    return SAEnum(
        python_enum,
        name=name,
        native_enum=False,
        length=40,
        values_callable=lambda e: [member.value for member in e],
        validate_strings=True,
    )


# --------------------------------------------------------------------------- #
# Mixins (DRY)                                                                 #
# --------------------------------------------------------------------------- #
class PKMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# Organisation                                                                 #
# --------------------------------------------------------------------------- #
class School(PKMixin, TimestampMixin, Base):
    __tablename__ = "schools"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    division: Mapped[str | None] = mapped_column(String(120))
    cluster: Mapped[str | None] = mapped_column(String(120))
    school_year: Mapped[str] = mapped_column(String(16), nullable=False)

    learning_areas: Mapped[list["LearningArea"]] = relationship(back_populates="school")
    sections: Mapped[list["Section"]] = relationship(back_populates="school")


class LearningArea(PKMixin, TimestampMixin, Base):
    """One subject, and one sheet in the EQWART workbook."""

    __tablename__ = "learning_areas"
    __table_args__ = (UniqueConstraint("school_id", "code", name="uq_learning_areas_school_code"),)

    school_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("schools.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Worksheet tab in the EQWART template, e.g. "Gen Math".
    sheet_name: Mapped[str] = mapped_column(String(64), nullable=False)
    grade_level: Mapped[int] = mapped_column(Integer, default=11, nullable=False)

    school: Mapped[School] = relationship(back_populates="learning_areas")
    competencies: Mapped[list["Competency"]] = relationship(back_populates="learning_area")


class Section(PKMixin, TimestampMixin, Base):
    """A class group of learners, e.g. "ACAD ASSH 11-1"."""

    __tablename__ = "sections"
    __table_args__ = (
        UniqueConstraint("school_id", "name", "school_year", name="uq_sections_school_name_year"),
    )

    school_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("schools.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    grade_level: Mapped[int] = mapped_column(Integer, default=11, nullable=False)
    school_year: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Display order in the EQWART sheet (column B..V).
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    school: Mapped[School] = relationship(back_populates="sections")
    enrollments: Mapped[list["Enrollment"]] = relationship(back_populates="section")


class Profile(PKMixin, TimestampMixin, Base):
    """Mirrors auth.users; id == the Supabase user id."""

    __tablename__ = "profiles"

    role: Mapped[UserRole] = mapped_column(
        _enum(UserRole, "user_role"), default=UserRole.subject_teacher, nullable=False
    )
    full_name: Mapped[str | None] = mapped_column(String(160))
    username: Mapped[str | None] = mapped_column(String(64), unique=True)
    email: Mapped[str | None] = mapped_column(String(254), unique=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    school_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("schools.id", ondelete="SET NULL"))
    #: Coordinators own one learning area; teachers may be scoped to one too.
    learning_area_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learning_areas.id", ondelete="SET NULL")
    )

    classes: Mapped[list["Class"]] = relationship(back_populates="teacher")


class Student(PKMixin, TimestampMixin, Base):
    __tablename__ = "students"
    __table_args__ = (
        UniqueConstraint("school_id", "student_no", name="uq_students_school_student_no"),
    )

    school_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("schools.id", ondelete="CASCADE"))
    student_no: Mapped[str] = mapped_column(String(32), nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)

    enrollments: Mapped[list["Enrollment"]] = relationship(back_populates="student")


class Enrollment(PKMixin, TimestampMixin, Base):
    """A learner belongs to one section, and through it to every class it takes."""

    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("student_id", "section_id", name="uq_enrollments_student_section"),
    )

    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"))
    section_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sections.id", ondelete="CASCADE"))
    status: Mapped[EnrollmentStatus] = mapped_column(
        _enum(EnrollmentStatus, "enrollment_status"), default=EnrollmentStatus.active, nullable=False
    )

    student: Mapped[Student] = relationship(back_populates="enrollments")
    section: Mapped[Section] = relationship(back_populates="enrollments")


class Class(PKMixin, TimestampMixin, Base):
    """One learning area taught to one section for a school year."""

    __tablename__ = "classes"
    __table_args__ = (
        UniqueConstraint(
            "section_id", "learning_area_id", "school_year", name="uq_classes_section_area_year"
        ),
    )

    section_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sections.id", ondelete="CASCADE"))
    learning_area_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learning_areas.id", ondelete="CASCADE")
    )
    teacher_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="RESTRICT"))
    class_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    school_year: Mapped[str] = mapped_column(String(16), nullable=False)

    section: Mapped[Section] = relationship()
    learning_area: Mapped[LearningArea] = relationship()
    teacher: Mapped[Profile] = relationship(back_populates="classes")
    assessments: Mapped[list["Assessment"]] = relationship(back_populates="class_")


# --------------------------------------------------------------------------- #
# Assessment                                                                   #
# --------------------------------------------------------------------------- #
class Competency(PKMixin, TimestampMixin, Base):
    __tablename__ = "competencies"
    __table_args__ = (UniqueConstraint("learning_area_id", "code", name="uq_competencies_area_code"),)

    learning_area_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learning_areas.id", ondelete="CASCADE")
    )
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    learning_area: Mapped[LearningArea] = relationship(back_populates="competencies")


class Assessment(PKMixin, TimestampMixin, Base):
    __tablename__ = "assessments"
    __table_args__ = (
        UniqueConstraint("class_id", "type", name="uq_assessments_class_type"),
        CheckConstraint("item_count > 0", name="item_count_positive"),
        Index("ix_assessments_class_status", "class_id", "status"),
    )

    title: Mapped[str] = mapped_column(String(160), nullable=False)
    type: Mapped[AssessmentType] = mapped_column(_enum(AssessmentType, "assessment_type"), nullable=False)
    class_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("classes.id", ondelete="CASCADE"))
    #: Fixed by type: 30 for summative, 60 for pre/post/term.
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Printed on the sheet and read back when scanning.
    test_code: Mapped[str] = mapped_column(String(24), unique=True, nullable=False)
    options: Mapped[str] = mapped_column(String(10), default="ABCDE", nullable=False)
    date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[AssessmentStatus] = mapped_column(
        _enum(AssessmentStatus, "assessment_status"), default=AssessmentStatus.draft, nullable=False
    )
    #: Set the first time a sheet is scanned; the key is immutable from then on.
    key_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))

    class_: Mapped[Class] = relationship(back_populates="assessments")
    answer_keys: Mapped[list["AnswerKey"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )
    submissions: Mapped[list["Submission"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )

    @property
    def possible_points(self) -> int:
        """One point per item."""
        return self.item_count


class AnswerKey(PKMixin, Base):
    __tablename__ = "answer_keys"
    __table_args__ = (
        UniqueConstraint("assessment_id", "item_no", name="uq_answer_keys_assessment_item"),
        CheckConstraint("item_no >= 1", name="item_no_positive"),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"))
    item_no: Mapped[int] = mapped_column(Integer, nullable=False)
    correct_option: Mapped[str] = mapped_column(String(1), nullable=False)
    points: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    competency_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("competencies.id", ondelete="SET NULL")
    )

    assessment: Mapped[Assessment] = relationship(back_populates="answer_keys")


class AnswerSheet(PKMixin, TimestampMixin, Base):
    """A generated, personalised sheet ready to print."""

    __tablename__ = "answer_sheets"
    __table_args__ = (
        UniqueConstraint("assessment_id", "student_id", name="uq_answer_sheets_assessment_student"),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"))
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"))
    sheet_code: Mapped[str] = mapped_column(String(32), nullable=False)
    pdf_path: Mapped[str | None] = mapped_column(String(512))


# --------------------------------------------------------------------------- #
# Scanning                                                                     #
# --------------------------------------------------------------------------- #
class Submission(PKMixin, TimestampMixin, Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("assessment_id", "student_id", name="uq_submissions_assessment_student"),
        Index("ix_submissions_assessment_status", "assessment_id", "status"),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"))
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"))
    #: Storage paths, never public URLs — the API signs them on read.
    image_path: Mapped[str | None] = mapped_column(String(512))
    name_strip_path: Mapped[str | None] = mapped_column(String(512))
    score: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[SubmissionStatus] = mapped_column(
        _enum(SubmissionStatus, "submission_status"), default=SubmissionStatus.missing, nullable=False
    )
    flagged_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scanned_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))

    assessment: Mapped[Assessment] = relationship(back_populates="submissions")
    student: Mapped[Student] = relationship()
    responses: Mapped[list["Response"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )


class Response(PKMixin, Base):
    __tablename__ = "responses"
    __table_args__ = (
        UniqueConstraint("submission_id", "item_no", name="uq_responses_submission_item"),
    )

    submission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"))
    item_no: Mapped[int] = mapped_column(Integer, nullable=False)
    #: None when blank; several letters when the learner shaded more than one.
    marked_option: Mapped[str | None] = mapped_column(String(10))
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    flag: Mapped[ResponseFlag] = mapped_column(
        _enum(ResponseFlag, "response_flag"), default=ResponseFlag.none, nullable=False
    )
    #: Kept when a teacher overrides the scan, so the original stays auditable.
    detected_option: Mapped[str | None] = mapped_column(String(10))

    submission: Mapped[Submission] = relationship(back_populates="responses")


# --------------------------------------------------------------------------- #
# Analysis and reporting                                                       #
# --------------------------------------------------------------------------- #
class ItemAnalysis(PKMixin, TimestampMixin, Base):
    __tablename__ = "item_analysis"
    __table_args__ = (
        UniqueConstraint("assessment_id", "item_no", name="uq_item_analysis_assessment_item"),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"))
    item_no: Mapped[int] = mapped_column(Integer, nullable=False)
    correct_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    percent_correct: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int | None] = mapped_column(Integer)
    discrimination_index: Mapped[float | None] = mapped_column(Float)
    mastery_level: Mapped[MasteryLevel | None] = mapped_column(_enum(MasteryLevel, "mastery_level"))
    instructional_assistance: Mapped[InstructionalAssistance | None] = mapped_column(
        _enum(InstructionalAssistance, "instructional_assistance")
    )
    #: {"A": 12, "B": 3, "_": 1, "CD": 1} — option counts including blank/multiple.
    distribution: Mapped[dict | None] = mapped_column(JSON)


class EqwartReport(PKMixin, TimestampMixin, Base):
    """One EQWART sheet: a learning area for one assessment type and school year."""

    __tablename__ = "eqwart_reports"
    __table_args__ = (
        UniqueConstraint(
            "learning_area_id", "assessment_type", "school_year", name="uq_eqwart_area_type_year"
        ),
    )

    learning_area_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learning_areas.id", ondelete="CASCADE")
    )
    assessment_type: Mapped[AssessmentType] = mapped_column(
        _enum(AssessmentType, "assessment_type"), nullable=False
    )
    school_year: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[ReportStatus] = mapped_column(
        _enum(ReportStatus, "report_status"), default=ReportStatus.draft, nullable=False
    )

    #: Signatories, mirroring rows 101-112 of the sheet.
    key_teacher_name: Mapped[str | None] = mapped_column(String(160))
    reviewed_by_name: Mapped[str | None] = mapped_column(String(160))
    approved_by_name: Mapped[str | None] = mapped_column(String(160))

    #: Planned interventions (several) and the single mode, rows 89-97.
    intervention_activities: Mapped[list | None] = mapped_column(JSON)
    intervention_other: Mapped[str | None] = mapped_column(Text)
    intervention_mode: Mapped[InterventionMode | None] = mapped_column(
        _enum(InterventionMode, "intervention_mode")
    )

    learning_area: Mapped[LearningArea] = relationship()
    least_mastered: Mapped[list["LeastMastered"]] = relationship(
        back_populates="report", cascade="all, delete-orphan", order_by="LeastMastered.position"
    )


class LeastMastered(PKMixin, TimestampMixin, Base):
    """One of the five least-mastered competencies a Coordinator encodes."""

    __tablename__ = "least_mastered"
    __table_args__ = (
        UniqueConstraint("report_id", "position", name="uq_least_mastered_report_position"),
        CheckConstraint("position BETWEEN 1 AND 5", name="position_in_range"),
    )

    report_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eqwart_reports.id", ondelete="CASCADE"))
    #: 1..5 — "Least Mastered Competency #1" through "#5".
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    rank_value: Mapped[int | None] = mapped_column(Integer)
    item_no: Mapped[int | None] = mapped_column(Integer)
    competency_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("competencies.id", ondelete="SET NULL")
    )
    #: Free text, since the template asks for "competency and course code".
    competency_text: Mapped[str | None] = mapped_column(Text)

    report: Mapped[EqwartReport] = relationship(back_populates="least_mastered")


class ReportTemplate(PKMixin, TimestampMixin, Base):
    """A DepEd workbook an administrator uploaded, plus how to fill it.

    `mapping` is the JSON form of a `TemplateMapping`: where section names,
    examinee counts, item frequencies, raw scores, competencies, interventions
    and signatories belong. Because the exporter reads it rather than fixed cell
    addresses, a reissued template needs a new row here and no code change.
    """

    __tablename__ = "report_templates"
    __table_args__ = (
        UniqueConstraint("school_year", "grade_level", "status", name="uq_templates_year_grade_status"),
        Index("ix_report_templates_status", "status"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    school_year: Mapped[str] = mapped_column(String(16), nullable=False)
    grade_level: Mapped[int] = mapped_column(Integer, default=11, nullable=False)

    status: Mapped[TemplateStatus] = mapped_column(
        _enum(TemplateStatus, "template_status"), default=TemplateStatus.draft, nullable=False
    )
    mapping: Mapped[dict | None] = mapped_column(JSON)
    mapping_source: Mapped[MappingSource] = mapped_column(
        _enum(MappingSource, "mapping_source"), default=MappingSource.heuristic, nullable=False
    )
    mapping_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Problems found while mapping or dry-running, shown to the administrator.
    mapping_notes: Mapped[list | None] = mapped_column(JSON)
    #: True once a synthetic fill proved the mapping actually writes correctly.
    dry_run_passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))


class Certificate(PKMixin, TimestampMixin, Base):
    __tablename__ = "certificates"
    __table_args__ = (
        UniqueConstraint("student_id", "class_id", name="uq_certificates_student_class"),
    )

    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"))
    class_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("classes.id", ondelete="CASCADE"))
    type: Mapped[CertificateType] = mapped_column(_enum(CertificateType, "certificate_type"), nullable=False)
    status: Mapped[CertificateStatus] = mapped_column(
        _enum(CertificateStatus, "certificate_status"), default=CertificateStatus.pending, nullable=False
    )
    issued_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_path: Mapped[str | None] = mapped_column(String(512))

    student: Mapped[Student] = relationship()
    class_: Mapped[Class] = relationship()


class AuditLog(PKMixin, Base):
    """Append-only trail for the actions that change results or release documents."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_actor_created", "actor_id", "created_at"),)

    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
