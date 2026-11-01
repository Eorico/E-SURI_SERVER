"""MongoDB documents for IMECAR / E-SURI.

Shapes follow `docs/REQUIREMENTS.md`: EQWART is organised as learning area x
section, so a `Class` is the pairing of the two for one term.

Three decisions shape this file, and the rest follows from them.

**Attribute names are unchanged.** `dto/` builds every response straight off
these objects (`from_attributes=True`), and services read `.student.full_name`
and `.class_.section`. Keeping the names means the DTOs, all ten route modules
and the whole frontend needed no edit for this migration.

**What is only ever read with its parent is embedded.** Five tables became
sub-documents: a submission's 60 responses, an assessment's answer key, a
report's five least-mastered competencies, and the two role tables that hung
off a profile. Each was a join on a path that is walked constantly, and each is
bounded - 60 responses is nowhere near the 16MB document ceiling. Embedding
them is what removes the N+1s rather than papering over them: loading a
submission with its responses is now one read, and so is resolving a caller's
role, which used to cost three.

**What is queried on its own stays its own collection.** A section is listed,
searched and reported on without its school; a student exists apart from any
one enrolment. Those are referenced by id, and `$lookup` fills the
**hydration slots** below - fields like `Submission.student`, marked
`exclude=True` so they are never written back. They are how `.student.id` kept
working after the relationships went away.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.models.enums import (
    AdminType,
    AssessmentStatus,
    AssessmentType,
    CertificateStatus,
    CertificateType,
    EnrollmentStatus,
    InstructionalAssistance,
    InterventionMode,
    MappingSource,
    MasteryLevel,
    ReportStatus,
    ResponseFlag,
    SubmissionStatus,
    TemplateStatus,
    TermStatus,
    UserRole,
)


#: `Assessment` has a field called `date`, which shadows the imported type
#: inside that class body - with deferred annotations, Pydantic resolves
#: `date | None` against the class namespace and finds the field. The alias is
#: the same type under a name nothing shadows.
DateOnly = date


def utcnow() -> datetime:
    """Timezone-aware now, so stored times are never ambiguous."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Base                                                                         #
# --------------------------------------------------------------------------- #
class Doc(BaseModel):
    """A document with an id.

    The id is the BSON `_id`, so there is no second identifier and no extra
    index. It stays a `uuid.UUID` rather than becoming an `ObjectId`: the client
    reads these ids out of the API and the DTOs are typed on them, so changing
    the type would have reached every route signature.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        arbitrary_types_allowed=True,
        validate_assignment=False,
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, alias="_id")

    @model_validator(mode="before")
    @classmethod
    def _utc_on_naive_datetimes(cls, data: Any) -> Any:
        """Attach UTC to any naive datetime coming out of the database.

        `tz_aware=True` on the client already does this, so normally there is
        nothing to do. It is repeated here because a document read through a
        client built without that flag would otherwise carry naive times, and
        the failure surfaces as a `TypeError` deep inside an expiry check
        rather than anywhere near the connection that caused it.
        """
        return _utc_naive(data) if isinstance(data, dict) else data

    def to_bson(self) -> dict[str, Any]:
        """The document as PyMongo should store it.

        `by_alias` is what puts the id under `_id`; `exclude_none=False` keeps
        explicit nulls, because a field cleared by a teacher must overwrite the
        old value rather than silently keep it.
        """
        return _bson_safe(self.model_dump(by_alias=True, exclude_none=False))

    def to_changes(self) -> dict[str, Any]:
        """The document as a `$set` payload - everything but the id."""
        payload = self.to_bson()
        payload.pop("_id", None)
        return payload


class TimestampedDoc(Doc):
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


def _bson_safe(value: Any) -> Any:
    """Convert what BSON should not store as it stands.

    Two kinds need it.

    **`date`.** BSON has a datetime type and no date type, so a bare `date` -
    an assessment's date is one - raises on insert. It is widened to midnight
    UTC here and narrowed back by the validators below, so callers go on using
    `date` and never see the difference.

    **`uuid.UUID`.** Stored as its 36-character string rather than as BSON
    binary subtype 4. Two reasons, in order of how much they matter:

    * The test suite runs against `mongomock`, which refuses a custom
      `uuidRepresentation` outright - so binary ids would mean the entire data
      layer shipped with no automated test behind it. That is a worse trade
      than twenty bytes per document.
    * Ids are legible in the Atlas console. This system gets administered by
      hand - a profile deleted directly in the provider's UI is already part of
      its history - and `"a3f1..."` you can read and copy beats `BinData(4,...)`
      you cannot.

    The cost is 36 bytes against 16. On a cluster where one answer sheet is
    200KB, that is noise. Pydantic parses the string straight back into a
    `uuid.UUID` on read, so nothing above this layer sees a string.
    """
    if isinstance(value, dict):
        return {key: _bson_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bson_safe(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return value


def _utc_naive(value: Any) -> Any:
    """Recursively make every naive datetime UTC. See `Doc._utc_on_naive_datetimes`."""
    if isinstance(value, dict):
        return {key: _utc_naive(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_utc_naive(item) for item in value]
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _as_date(value: Any) -> Any:
    """Narrow a stored datetime back to the `date` the field declares."""
    return value.date() if isinstance(value, datetime) else value


def _unique_by(items: list, attribute: str, label: str) -> list:
    """Refuse a duplicate inside an embedded array.

    Each of these was a UNIQUE constraint while the array was a table:
    `uq_answer_keys_assessment_item`, `uq_responses_submission_item` and
    `uq_least_mastered_report_position`. No index can reach inside an array to
    replace them, so the rule lives on the document - and it is not cosmetic. A
    second response for item 7 would be counted twice in the score, and a
    duplicated answer key would make the right answer depend on which copy was
    read first.
    """
    seen = [getattr(item, attribute) for item in items]
    duplicates = {value for value in seen if seen.count(value) > 1}
    if duplicates:
        listed = ", ".join(str(value) for value in sorted(duplicates))
        raise ValueError(f"Duplicate {label}: {listed}.")
    return items


# --------------------------------------------------------------------------- #
# Organisation                                                                 #
# --------------------------------------------------------------------------- #
class School(TimestampedDoc):
    name: str
    division: str | None = None
    cluster: str | None = None
    school_year: str


class Department(TimestampedDoc):
    """A grouping of subjects, e.g. "Science" or "Math & PE".

    It exists for one reason: a master teacher sees their department and
    nothing else, so the department is what their reports are filtered by.
    """

    school_id: uuid.UUID
    code: str
    name: str


class Term(TimestampedDoc):
    """One assessment cycle: a term within a school year.

    A teacher's subjects and sections change every term, so a class belongs to
    a term rather than to a year - that is what stops last term's roster
    following them into this one.

    Only an assessment coordinator opens or closes one. Closing is what makes a
    cycle final: no more scanning into it.
    """

    school_id: uuid.UUID
    school_year: str
    term_no: int = Field(ge=1, le=4)
    status: TermStatus = TermStatus.open
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    closed_by: uuid.UUID | None = None

    @property
    def label(self) -> str:
        return f"Term {self.term_no} · {self.school_year}"


class LearningArea(TimestampedDoc):
    """One subject, and one sheet in the EQWART workbook."""

    school_id: uuid.UUID
    code: str
    name: str
    #: Worksheet tab in the EQWART template, e.g. "Gen Math".
    sheet_name: str
    grade_level: int = 11
    #: Which department this subject reports under. Optional because a school
    #: may add a subject before deciding where it sits.
    department_id: uuid.UUID | None = None

    #: Hydration slots.
    school: School | None = Field(default=None, exclude=True)
    department: Department | None = Field(default=None, exclude=True)


class Section(TimestampedDoc):
    """A class group of learners, e.g. "ACAD ASSH 11-1"."""

    school_id: uuid.UUID
    name: str
    grade_level: int = 11
    school_year: str
    #: Display order in the EQWART sheet (column B..V).
    position: int = 0


# --------------------------------------------------------------------------- #
# People                                                                       #
# --------------------------------------------------------------------------- #
class CoordinatorAssignment(BaseModel):
    """The learning area a subject coordinator is responsible for.

    Embedded rather than referenced, so a profile and its role details are one
    document and resolving a caller costs a single read.

    Only a coordinator may hold one. MongoDB has no constraint that can pin a
    sub-document to a role, so that is enforced on the document instead:
    `_assignment_matches_role` below refuses the combination outright.
    """

    model_config = ConfigDict(extra="ignore")

    _utc = model_validator(mode="before")(
        classmethod(lambda cls, data: _utc_naive(data) if isinstance(data, dict) else data)
    )

    #: Always `subject_coordinator`. Kept so a stored sub-document still states
    #: which role it belongs to, as the old composite key did.
    role: UserRole = UserRole.subject_coordinator
    learning_area_id: uuid.UUID
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AdministratorProfile(BaseModel):
    """Which kind of administrator someone is, and what they oversee.

    `department_id` is required for a master teacher and meaningless for the
    other two - an assessment coordinator and a school head both see the whole
    school, so there is nothing to scope them to. That pairing was a CHECK
    constraint; it is validated here instead.
    """

    model_config = ConfigDict(extra="ignore")

    _utc = model_validator(mode="before")(
        classmethod(lambda cls, data: _utc_naive(data) if isinstance(data, dict) else data)
    )

    #: Always `administrator`.
    role: UserRole = UserRole.administrator
    admin_type: AdminType
    department_id: uuid.UUID | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _department_matches_type(self):
        """A master teacher has a department; the other two never do.

        Checked on the model rather than on the field: a field validator does
        not run when the field is simply absent, so a master teacher with no
        department at all - the very case this exists to catch - went through.
        """
        if self.admin_type is AdminType.master_teacher and self.department_id is None:
            raise ValueError("A master teacher must be assigned a department.")
        if self.admin_type is not AdminType.master_teacher and self.department_id is not None:
            raise ValueError(
                "Only a master teacher is scoped to a department; an assessment "
                "coordinator and a school head see the whole school."
            )
        return self


class Profile(TimestampedDoc):
    """A member of staff, and the credentials they sign in with.

    This document *is* the account - there is no separate identity store, so
    `password_hash` and `email_confirmed_at` sit here beside the name. The hash
    is Argon2id and never leaves the server.

    That is also what makes an account impossible to orphan: creating one is a
    single insert, with no second system for it to fall out of step with.

    Role-specific details are embedded beside it - see the two classes above -
    so a teacher's document still carries no field that could not apply to them.
    """

    role: UserRole = UserRole.subject_teacher
    #: DepEd records a name in parts, and reports sort on the surname, so the
    #: parts are stored and `full_name` is assembled rather than the reverse.
    last_name: str
    first_name: str
    #: One letter for most, but some surnames-as-middle-names need a few.
    middle_initial: str | None = None
    #: DepEd's "name extension": Jr., Sr., III. Most staff have none.
    suffix: str | None = None
    username: str | None = None
    email: str | None = None
    phone: str | None = None
    is_active: bool = True
    school_id: uuid.UUID | None = None

    #: Argon2id. Excluded from `model_dump` by default would be wrong - it has
    #: to be stored - so it is the DTOs that leave it out, as they always did.
    password_hash: str | None = None
    #: Set when the person enters the code mailed to them at registration.
    email_confirmed_at: datetime | None = None
    last_sign_in_at: datetime | None = None
    #: Consecutive failures, reset by a successful sign-in. Together with
    #: `locked_until` this is the per-account brake the rate limiter cannot be:
    #: the limiter keys on the client address, so it does not see one account
    #: being ground at from many of them.
    failed_sign_ins: int = 0
    locked_until: datetime | None = None

    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > utcnow()

    coordinator_assignment: CoordinatorAssignment | None = None
    administrator_profile: AdministratorProfile | None = None

    @field_validator("administrator_profile")
    @classmethod
    def _role_details_match_role(cls, value, info):
        """Refuse administrator details on someone who is not an administrator.

        Nothing in the database can enforce this, so it is enforced here.
        Without it a profile could claim a department it has no right to, and
        the department is what decides whose reports a master teacher reads.
        """
        role = info.data.get("role")
        if value is not None and role is not None and role is not UserRole.administrator:
            raise ValueError("Administrator details belong only to an administrator.")
        return value

    @field_validator("coordinator_assignment")
    @classmethod
    def _assignment_matches_role(cls, value, info):
        role = info.data.get("role")
        if value is not None and role is not None and role is not UserRole.subject_coordinator:
            raise ValueError("A coordinator assignment belongs only to a subject coordinator.")
        return value

    @property
    def full_name(self) -> str:
        """Natural order, for greeting someone and for the UI."""
        parts = [self.first_name]
        if self.middle_initial:
            initial = self.middle_initial.strip().rstrip(".")
            parts.append(f"{initial}." if len(initial) == 1 else initial)
        parts.append(self.last_name)
        name = " ".join(part for part in parts if part)
        return f"{name}, {self.suffix}" if self.suffix else name

    @property
    def formal_name(self) -> str:
        """Surname first, which is how every DepEd list is ordered."""
        given = self.first_name
        if self.middle_initial:
            initial = self.middle_initial.strip().rstrip(".")
            given = f"{given} {initial}." if len(initial) == 1 else f"{given} {initial}"
        surname = f"{self.last_name} {self.suffix}" if self.suffix else self.last_name
        return f"{surname}, {given}"

    @property
    def admin_type(self) -> AdminType | None:
        """Which kind of administrator, or None for every other role."""
        return self.administrator_profile.admin_type if self.administrator_profile else None

    @property
    def department_id(self) -> uuid.UUID | None:
        """The department a master teacher oversees; None for everyone else."""
        return self.administrator_profile.department_id if self.administrator_profile else None

    @property
    def learning_area_id(self) -> uuid.UUID | None:
        """The area a coordinator owns, or None for every other role."""
        return self.coordinator_assignment.learning_area_id if self.coordinator_assignment else None


class Student(TimestampedDoc):
    school_id: uuid.UUID
    student_no: str
    full_name: str


class Enrollment(TimestampedDoc):
    """A learner belongs to one section, and through it to every class it takes.

    Its own collection, not embedded: a roster is read by section without the
    learner, and a learner's sections are read without the section.
    """

    student_id: uuid.UUID
    section_id: uuid.UUID
    status: EnrollmentStatus = EnrollmentStatus.active

    #: Hydration slots.
    student: Student | None = Field(default=None, exclude=True)
    section: Section | None = Field(default=None, exclude=True)


class Class(TimestampedDoc):
    """One learning area taught to one section for a term."""

    section_id: uuid.UUID
    learning_area_id: uuid.UUID
    teacher_id: uuid.UUID
    class_code: str
    school_year: str
    #: The cycle this class belongs to. A teacher's subjects and sections
    #: change each term, so the pairing is per term, not per year.
    term_id: uuid.UUID | None = None

    #: Hydration slots, filled by `ClassRepository.hydrate` with one batched
    #: query per relation however many classes are being loaded.
    section: Section | None = Field(default=None, exclude=True)
    learning_area: LearningArea | None = Field(default=None, exclude=True)
    teacher: Profile | None = Field(default=None, exclude=True)
    term: Term | None = Field(default=None, exclude=True)


# --------------------------------------------------------------------------- #
# Assessment                                                                   #
# --------------------------------------------------------------------------- #
class Competency(TimestampedDoc):
    learning_area_id: uuid.UUID
    code: str
    description: str


class AnswerKey(BaseModel):
    """One item's correct option, embedded in its assessment.

    Embedded because a key is never read without the assessment it belongs to,
    and is replaced wholesale when a teacher edits it. Sixty items is a tiny
    array; as a table it was sixty rows and a join on every scan.
    """

    model_config = ConfigDict(extra="ignore")

    item_no: int = Field(ge=1)
    correct_option: str
    points: int = 1
    competency_id: uuid.UUID | None = None

    #: Routing only, never stored - the parent document already says which
    #: assessment this belongs to. It is kept because the services construct
    #: these objects with it (AnswerKey(assessment_id=..., ...)) and the repository uses
    #: it to find the document to write into.
    assessment_id: uuid.UUID | None = Field(default=None, exclude=True)



class Assessment(TimestampedDoc):
    title: str
    type: AssessmentType
    class_id: uuid.UUID
    #: Fixed by type: 30 for summative, 60 for pre/post/term.
    item_count: int = Field(gt=0)
    #: Printed on the sheet and read back when scanning.
    test_code: str
    options: str = "ABCDE"
    date: DateOnly | None = None
    status: AssessmentStatus = AssessmentStatus.draft
    #: Set the first time a sheet is scanned; the key is immutable from then on.
    key_locked_at: datetime | None = None
    created_by: uuid.UUID | None = None

    answer_keys: list[AnswerKey] = Field(default_factory=list)

    #: Hydration slot.
    class_: Class | None = Field(default=None, exclude=True)

    _narrow_date = field_validator("date", mode="before")(_as_date)

    @field_validator("answer_keys")
    @classmethod
    def _one_key_per_item(cls, value):
        return _unique_by(value, "item_no", "answer key item number")

    @property
    def possible_points(self) -> int:
        """One point per item."""
        return self.item_count


class AnswerSheet(TimestampedDoc):
    """A generated, personalised sheet ready to print."""

    assessment_id: uuid.UUID
    student_id: uuid.UUID
    sheet_code: str
    pdf_path: str | None = None


# --------------------------------------------------------------------------- #
# Scanning                                                                     #
# --------------------------------------------------------------------------- #
class Response(BaseModel):
    """One item's answer, embedded in its submission.

    This is the embed that matters most. As a table it was one row per item per
    learner - 60 items across 70 examinees is 4,200 rows for a single section's
    exam - and every score, every item analysis and every report had to join
    them back together. They are only ever read as "this submission's answers",
    which is exactly what an embedded array is for.
    """

    model_config = ConfigDict(extra="ignore")

    item_no: int = Field(ge=1)
    #: None when blank; several letters when the learner shaded more than one.
    marked_option: str | None = None
    is_correct: bool = False
    flag: ResponseFlag = ResponseFlag.none
    #: Kept when a teacher overrides the scan, so the original stays auditable.
    detected_option: str | None = None

    #: Routing only, never stored - the parent document already says which
    #: submission this belongs to. It is kept because the services construct
    #: these objects with it (Response(submission_id=..., ...)) and the repository uses
    #: it to find the document to write into.
    submission_id: uuid.UUID | None = Field(default=None, exclude=True)



class Submission(TimestampedDoc):
    assessment_id: uuid.UUID
    student_id: uuid.UUID
    #: Storage paths, never public URLs - the API signs them on read.
    image_path: str | None = None
    name_strip_path: str | None = None
    score: int | None = None
    status: SubmissionStatus = SubmissionStatus.missing
    flagged_count: int = 0
    scanned_at: datetime | None = None
    scanned_by: uuid.UUID | None = None
    approved_at: datetime | None = None
    approved_by: uuid.UUID | None = None

    responses: list[Response] = Field(default_factory=list)

    #: Hydration slots.
    student: Student | None = Field(default=None, exclude=True)
    assessment: Assessment | None = Field(default=None, exclude=True)

    @field_validator("responses")
    @classmethod
    def _one_response_per_item(cls, value):
        return _unique_by(value, "item_no", "response item number")


# --------------------------------------------------------------------------- #
# Analysis and reporting                                                       #
# --------------------------------------------------------------------------- #
class ItemAnalysis(TimestampedDoc):
    """Per-item statistics for one assessment.

    Its own collection rather than embedded: it is recomputed as a set, ranked
    across items, and read by the exporter without the assessment.
    """

    assessment_id: uuid.UUID
    item_no: int
    correct_count: int = 0
    attempted_count: int = 0
    percent_correct: float | None = None
    rank: int | None = None
    discrimination_index: float | None = None
    mastery_level: MasteryLevel | None = None
    instructional_assistance: InstructionalAssistance | None = None
    #: {"A": 12, "B": 3, "_": 1, "CD": 1} - option counts including blank/multiple.
    distribution: dict | None = None


class LeastMastered(BaseModel):
    """One of the five least-mastered competencies a coordinator encodes.

    Embedded: there are at most five, they are always read with the report, and
    they are ordered by `position` rather than queried on their own.
    """

    model_config = ConfigDict(extra="ignore")

    #: 1..5 - "Least Mastered Competency #1" through "#5".
    position: int = Field(ge=1, le=5)
    rank_value: int | None = None
    item_no: int | None = None
    competency_id: uuid.UUID | None = None
    #: Free text, since the template asks for "competency and course code".
    competency_text: str | None = None

    #: Routing only, never stored - the parent document already says which
    #: report this belongs to. It is kept because the services construct
    #: these objects with it (LeastMastered(report_id=..., ...)) and the repository uses
    #: it to find the document to write into.
    report_id: uuid.UUID | None = Field(default=None, exclude=True)



class EqwartReport(TimestampedDoc):
    """One EQWART sheet: a learning area for one assessment type and school year."""

    learning_area_id: uuid.UUID
    assessment_type: AssessmentType
    school_year: str
    status: ReportStatus = ReportStatus.draft

    #: Signatories, mirroring rows 101-112 of the sheet.
    key_teacher_name: str | None = None
    reviewed_by_name: str | None = None
    approved_by_name: str | None = None

    #: Planned interventions (several) and the single mode, rows 89-97.
    intervention_activities: list | None = None
    intervention_other: str | None = None
    intervention_mode: InterventionMode | None = None

    least_mastered: list[LeastMastered] = Field(default_factory=list)

    #: Hydration slot.
    learning_area: LearningArea | None = Field(default=None, exclude=True)

    @field_validator("least_mastered")
    @classmethod
    def _one_entry_per_position(cls, value):
        return _unique_by(value, "position", "least-mastered position")


class ReportTemplate(TimestampedDoc):
    """A DepEd workbook an administrator uploaded, plus how to fill it.

    `mapping` is the JSON form of a `TemplateMapping`: where section names,
    examinee counts, item frequencies, raw scores, competencies, interventions
    and signatories belong. Because the exporter reads it rather than fixed cell
    addresses, a reissued template needs a new document here and no code change.
    """

    name: str
    original_filename: str
    storage_path: str
    checksum: str

    school_year: str
    grade_level: int = 11

    status: TemplateStatus = TemplateStatus.draft
    mapping: dict | None = None
    mapping_source: MappingSource = MappingSource.heuristic
    mapping_confidence: float = 0.0
    #: Problems found while mapping or dry-running, shown to the administrator.
    mapping_notes: list | None = None
    #: True once a synthetic fill proved the mapping actually writes correctly.
    dry_run_passed: bool = False

    uploaded_by: uuid.UUID | None = None
    activated_at: datetime | None = None
    activated_by: uuid.UUID | None = None


class Certificate(TimestampedDoc):
    student_id: uuid.UUID
    class_id: uuid.UUID
    type: CertificateType
    status: CertificateStatus = CertificateStatus.pending
    issued_by: uuid.UUID | None = None
    issued_at: datetime | None = None
    pdf_path: str | None = None

    #: Hydration slots.
    student: Student | None = Field(default=None, exclude=True)
    class_: Class | None = Field(default=None, exclude=True)


class AuditLog(Doc):
    """Append-only trail for the actions that change results or release documents."""

    actor_id: uuid.UUID | None = None
    action: str
    entity_type: str
    entity_id: uuid.UUID | None = None
    metadata_json: dict | None = None
    ip_address: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    #: Microseconds since the epoch, and what the trail is actually ordered by.
    #:
    #: `created_at` is what a person reads; this is what sorts. A BSON datetime
    #: holds only milliseconds, so several entries written inside one
    #: millisecond - approving a section's papers in a loop - share a
    #: `created_at` and tie. MongoDB returns ties in no defined order, so the
    #: trail would read differently each time it was opened. An append-only
    #: trail has to read the same way twice, which is what this guarantees.
    sequence: int = Field(default_factory=lambda: int(utcnow().timestamp() * 1_000_000))


# --------------------------------------------------------------------------- #
# Authentication                                                               #
#                                                                              #
# The state that proves who someone is between requests: which refresh tokens  #
# are still live, and which mailed codes are still good.                        #
# --------------------------------------------------------------------------- #
class Session(Doc):
    """One issued refresh token.

    Recorded rather than left stateless so that signing out actually ends the
    session. A stateless refresh token cannot be withdrawn: it stays valid
    until it lapses, which would have quietly weakened `POST /auth/logout`
    from "this session is over" to "please stop using it".

    `expires_at` carries a TTL index, so MongoDB deletes lapsed sessions
    without a cron job.
    """

    user_id: uuid.UUID
    #: SHA-256 of the token, not the token. A leaked database then yields
    #: nothing usable, the same reason passwords are hashed.
    token_hash: str
    issued_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    revoked_at: datetime | None = None
    user_agent: str | None = None
    ip_address: str | None = None

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None and self.expires_at > utcnow()


#: `EmailToken.purpose`. Constants rather than an enum: they never reach a
#: response or a form, so there is nothing for an enum to validate at the edge.
PURPOSE_SIGNUP = "signup"
PURPOSE_RESET = "password_reset"


class EmailToken(Doc):
    """A registration code or a password-reset token.

    One collection for both because they differ only in `purpose` and lifetime;
    two would have meant the same expiry, attempt-counting and single-use logic
    written twice.

    `attempts` is what stops a six-digit code being guessed: the code is short
    enough to brute force at a million tries, so the token is spent after a few
    wrong ones and a fresh one must be requested.
    """

    user_id: uuid.UUID
    email: str
    purpose: str
    #: SHA-256 of the code, for the same reason as `Session.token_hash`.
    code_hash: str
    expires_at: datetime
    consumed_at: datetime | None = None
    attempts: int = 0
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_live(self) -> bool:
        return self.consumed_at is None and self.expires_at > utcnow()


#: Collection name per document class. One place, so a repository cannot drift
#: from the index definitions in `api/core/indexes.py`.
#:
#: `Profile` maps to `users`, and that is deliberate rather than a leftover.
#: The document *is* the account - it carries the password hash and the
#: confirmation state - so `users` is what the database should call it. The
#: class keeps the name `Profile` because `Principal` already distinguishes
#: `user` (the identity a token proves) from `profile` (the staff record read
#: from the database), and a request genuinely needs both.
COLLECTIONS: dict[type[Doc], str] = {
    School: "schools",
    Department: "departments",
    Term: "terms",
    LearningArea: "learning_areas",
    Section: "sections",
    Profile: "users",
    Student: "students",
    Enrollment: "enrollments",
    Class: "classes",
    Competency: "competencies",
    Assessment: "assessments",
    AnswerSheet: "answer_sheets",
    Submission: "submissions",
    ItemAnalysis: "item_analysis",
    EqwartReport: "eqwart_reports",
    ReportTemplate: "report_templates",
    Certificate: "certificates",
    AuditLog: "audit_logs",
    Session: "sessions",
    EmailToken: "email_tokens",
}
