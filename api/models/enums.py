"""Domain enumerations and the constants tied to them.

Kept apart from `documents.py` so services and DTOs can import the vocabulary
without pulling in the document models or the database driver.
"""

from __future__ import annotations

import enum


class AdminType(str, enum.Enum):
    """What kind of administrator someone is.

    All three hold the `administrator` role and everything a subject teacher
    can do; they differ in how far they can see and what they may close.

    - `assessment_coordinator` runs the cycle: opens and closes a term, sees
      every report, releases certificates.
    - `master_teacher` sees their own department and nothing else - the filter
      that makes their analytics useful rather than school-wide noise.
    - `school_head` reads the overall EQWART and downloads it; they do not
      administer the cycle.
    """

    assessment_coordinator = "assessment_coordinator"
    master_teacher = "master_teacher"
    school_head = "school_head"


class TermStatus(str, enum.Enum):
    """A term is open for scanning and encoding, or closed to it."""

    open = "open"
    closed = "closed"


class UserRole(str, enum.Enum):
    """The three roles in the capstone document.

    `administrator` is split further by `AdminType`: an assessment
    coordinator, a master teacher and a school head all hold this role but
    see different amounts of the school.
    """

    subject_teacher = "subject_teacher"
    subject_coordinator = "subject_coordinator"
    administrator = "administrator"


class AssessmentType(str, enum.Enum):
    pre = "pre"
    st1 = "st1"
    st2 = "st2"
    term = "term"
    post = "post"


class AssessmentStatus(str, enum.Enum):
    draft = "draft"
    scheduled = "scheduled"
    in_progress = "in_progress"
    completed = "completed"


class SubmissionStatus(str, enum.Enum):
    missing = "missing"
    review = "review"
    approved = "approved"


class ResponseFlag(str, enum.Enum):
    none = "none"
    blank = "blank"
    multiple = "multiple"


class MasteryLevel(str, enum.Enum):
    not_proficient = "Not Proficient"
    low_proficient = "Low Proficient"
    nearly_proficient = "Nearly Proficient"
    proficient = "Proficient"
    highly_proficient = "Highly Proficient"


class InstructionalAssistance(str, enum.Enum):
    reteach = "Reteach"
    remediation = "Remediation"
    intervention = "Intervention"
    enhancement = "Enhancement"
    advancement = "Advancement"


class InterventionActivity(str, enum.Enum):
    """Planned interventions — the Coordinator may pick several."""

    reteaching = "Reteaching"
    alternative_strategies = "Using alternative instructional strategies"
    task_analysis = "Task analysis"
    additional_practice = "Additional practice"
    one_on_one_tutoring = "One-on-one tutoring"
    other = "Other"


class InterventionMode(str, enum.Enum):
    """Mode of intervention — exactly one."""

    add_on = "Add-on"
    pull_out = "Pull-out"
    built_in = "Built-in"


class CertificateType(str, enum.Enum):
    completion = "completion"
    participation = "participation"


class CertificateStatus(str, enum.Enum):
    pending = "pending"
    released = "released"
    revoked = "revoked"


class EnrollmentStatus(str, enum.Enum):
    active = "active"
    transferred = "transferred"
    dropped = "dropped"


class TemplateStatus(str, enum.Enum):
    """An uploaded report template moves draft -> active -> archived."""

    draft = "draft"
    active = "active"
    archived = "archived"
    rejected = "rejected"


class MappingSource(str, enum.Enum):
    ai = "ai"
    heuristic = "heuristic"
    manual = "manual"


class ReportStatus(str, enum.Enum):
    draft = "draft"
    submitted = "submitted"
    reviewed = "reviewed"
    approved = "approved"


# --------------------------------------------------------------------------- #
# Constants bound to the enums                                                  #
# --------------------------------------------------------------------------- #

#: Item count is fixed by assessment type and is never entered by hand.
ITEM_COUNT_BY_TYPE: dict[AssessmentType, int] = {
    AssessmentType.pre: 60,
    AssessmentType.st1: 30,
    AssessmentType.st2: 30,
    AssessmentType.term: 60,
    AssessmentType.post: 60,
}

ASSESSMENT_LABELS: dict[AssessmentType, str] = {
    AssessmentType.pre: "Pre-Test",
    AssessmentType.st1: "Summative Test 1",
    AssessmentType.st2: "Summative Test 2",
    AssessmentType.term: "End Term Examination",
    AssessmentType.post: "Post-Test",
}

#: The printed sheets carry five bubbles per item.
ANSWER_OPTIONS: tuple[str, ...] = ("A", "B", "C", "D", "E")

#: A Completion certificate needs an approved submission for all of these.
REQUIRED_FOR_COMPLETION: frozenset[AssessmentType] = frozenset(AssessmentType)

#: The EQWART sheet has columns B..V for sections.
MAX_SECTIONS_PER_LEARNING_AREA = 21

#: Table 2 of the EQWART sheet has rows for examinees 1..70.
MAX_EXAMINEES_PER_SECTION = 70

#: How many least-mastered competencies the Coordinator encodes.
LEAST_MASTERED_COUNT = 5
