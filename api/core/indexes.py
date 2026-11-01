"""Index and uniqueness definitions - what the SQL migrations used to declare.

This is a *desired state*, not a migration history. Creating an index in
MongoDB is idempotent, so there is no version to record and no file that
becomes untouchable the moment it has run somewhere - `scripts/sync_indexes.py`
applies this and is safe to run as often as you like, including on every
deploy.

Three things a relational schema would enforce are not here, because MongoDB
has no equivalent to borrow:

* **Foreign keys.** There is no referential action to declare, so nothing
  cascades on delete. Where a parent going away must take its children with
  it, the service that owns the delete does it - and where the old schema said
  `ON DELETE SET NULL`, a dangling id simply reads as missing.
* **Row-level security.** There is none, and nothing replaces it:
  authorisation is the role dependencies in `api/core/security.py` and the
  checks in the services, which is where it has always actually been enforced.
* **Uniqueness inside an embedded array.** One answer key per item, one
  response per item, one least-mastered entry per position. No index can reach
  inside an array, so these are validated on the document - see `_unique_by` in
  `api/models/documents.py`.

A note on unique indexes over optional fields: `users.username` and
`users.email` are both optional and both unique. A plain unique index would
allow exactly one document without the field, because MongoDB indexes a missing
value as null and null collides with null - so every account registered without
a username after the first would be refused. Each therefore carries a
`partialFilterExpression` limiting the index to documents where the field is
actually a string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.database import Database

from api.core.logging import get_logger

logger = get_logger(__name__)

#: Only documents where the field is a real string take part in the index, so
#: any number of documents may omit it.
_PRESENT = {"$type": "string"}


@dataclass(frozen=True)
class CollectionIndexes:
    collection: str
    indexes: list[IndexModel] = field(default_factory=list)


def _ix(*keys: tuple[str, int], name: str, unique: bool = False, **extra: Any) -> IndexModel:
    return IndexModel(list(keys), name=name, unique=unique, **extra)


#: Every index the application needs. The names say what each one is for, so a
#: slow query's plan can be traced back to the rule it was meant to serve.
SCHEMA: tuple[CollectionIndexes, ...] = (
    CollectionIndexes("departments", [
        _ix(("school_id", ASCENDING), ("code", ASCENDING),
            name="uq_departments_school_code", unique=True),
    ]),
    CollectionIndexes("terms", [
        _ix(("school_id", ASCENDING), ("school_year", ASCENDING), ("term_no", ASCENDING),
            name="uq_terms_school_year_no", unique=True),
        # "Which cycle is open?" runs on every class and assessment screen.
        _ix(("school_id", ASCENDING), ("status", ASCENDING), name="ix_terms_school_status"),
    ]),
    CollectionIndexes("learning_areas", [
        _ix(("school_id", ASCENDING), ("code", ASCENDING),
            name="uq_learning_areas_school_code", unique=True),
        # A master teacher's reports filter by department.
        _ix(("department_id", ASCENDING), name="ix_learning_areas_department"),
    ]),
    CollectionIndexes("sections", [
        _ix(("school_id", ASCENDING), ("name", ASCENDING), ("school_year", ASCENDING),
            name="uq_sections_school_name_year", unique=True),
    ]),
    CollectionIndexes("users", [
        # Sign-in reads one of these two on every attempt.
        _ix(("username", ASCENDING), name="uq_users_username", unique=True,
            partialFilterExpression={"username": _PRESENT}),
        _ix(("email", ASCENDING), name="uq_users_email", unique=True,
            partialFilterExpression={"email": _PRESENT}),
        _ix(("role", ASCENDING), name="ix_users_role"),
        _ix(("school_id", ASCENDING), name="ix_users_school"),
        # Staff lists are ordered by surname, which is how DepEd lists read.
        _ix(("last_name", ASCENDING), ("first_name", ASCENDING), name="ix_users_name"),
        # A master teacher's own people, found without a scan.
        _ix(("administrator_profile.department_id", ASCENDING),
            name="ix_users_admin_department"),
    ]),
    CollectionIndexes("students", [
        _ix(("school_id", ASCENDING), ("student_no", ASCENDING),
            name="uq_students_school_student_no", unique=True),
    ]),
    CollectionIndexes("enrollments", [
        _ix(("student_id", ASCENDING), ("section_id", ASCENDING),
            name="uq_enrollments_student_section", unique=True),
        # "Who is in this section?" - the roster, read constantly.
        _ix(("section_id", ASCENDING), ("status", ASCENDING), name="ix_enrollments_section"),
    ]),
    CollectionIndexes("classes", [
        _ix(("section_id", ASCENDING), ("learning_area_id", ASCENDING), ("school_year", ASCENDING),
            name="uq_classes_section_area_year", unique=True),
        _ix(("class_code", ASCENDING), name="uq_classes_class_code", unique=True),
        _ix(("teacher_id", ASCENDING), name="ix_classes_teacher"),
        _ix(("learning_area_id", ASCENDING), name="ix_classes_area"),
        _ix(("term_id", ASCENDING), name="ix_classes_term"),
    ]),
    CollectionIndexes("competencies", [
        _ix(("learning_area_id", ASCENDING), ("code", ASCENDING),
            name="uq_competencies_area_code", unique=True),
    ]),
    CollectionIndexes("assessments", [
        _ix(("class_id", ASCENDING), ("type", ASCENDING),
            name="uq_assessments_class_type", unique=True),
        # Read back off the sheet when scanning, so it has to be fast and unique.
        _ix(("test_code", ASCENDING), name="uq_assessments_test_code", unique=True),
        _ix(("class_id", ASCENDING), ("status", ASCENDING), name="ix_assessments_class_status"),
    ]),
    CollectionIndexes("answer_sheets", [
        _ix(("assessment_id", ASCENDING), ("student_id", ASCENDING),
            name="uq_answer_sheets_assessment_student", unique=True),
    ]),
    CollectionIndexes("submissions", [
        _ix(("assessment_id", ASCENDING), ("student_id", ASCENDING),
            name="uq_submissions_assessment_student", unique=True),
        _ix(("assessment_id", ASCENDING), ("status", ASCENDING),
            name="ix_submissions_assessment_status"),
        # A learner's results across assessments, which the unique index above
        # cannot serve because it leads with the assessment.
        _ix(("student_id", ASCENDING), name="ix_submissions_student"),
    ]),
    CollectionIndexes("item_analysis", [
        _ix(("assessment_id", ASCENDING), ("item_no", ASCENDING),
            name="uq_item_analysis_assessment_item", unique=True),
    ]),
    CollectionIndexes("eqwart_reports", [
        _ix(("learning_area_id", ASCENDING), ("assessment_type", ASCENDING),
            ("school_year", ASCENDING), name="uq_eqwart_area_type_year", unique=True),
    ]),
    CollectionIndexes("report_templates", [
        _ix(("school_year", ASCENDING), ("grade_level", ASCENDING), ("status", ASCENDING),
            name="uq_templates_year_grade_status", unique=True),
        # "Which template is active?" is asked by every export.
        _ix(("status", ASCENDING), name="ix_report_templates_status"),
    ]),
    CollectionIndexes("certificates", [
        _ix(("student_id", ASCENDING), ("class_id", ASCENDING),
            name="uq_certificates_student_class", unique=True),
    ]),
    CollectionIndexes("audit_logs", [
        _ix(("actor_id", ASCENDING), ("sequence", DESCENDING),
            name="ix_audit_logs_actor_created"),
        _ix(("entity_type", ASCENDING), ("entity_id", ASCENDING), name="ix_audit_logs_entity"),
        # The trail's own order, which `recent()` reads straight off the index.
        _ix(("sequence", DESCENDING), name="ix_audit_logs_sequence"),
    ]),

    # --- Authentication ---------------------------------------------------
    # Both of these carry a TTL index, so MongoDB deletes what has lapsed on
    # its own. Without it the two fastest-growing collections in the system
    # would need a cron job nobody would remember to write.
    CollectionIndexes("sessions", [
        _ix(("token_hash", ASCENDING), name="uq_sessions_token_hash", unique=True),
        _ix(("user_id", ASCENDING), name="ix_sessions_user"),
        _ix(("expires_at", ASCENDING), name="ttl_sessions_expires", expireAfterSeconds=0),
    ]),
    CollectionIndexes("email_tokens", [
        _ix(("code_hash", ASCENDING), name="ix_email_tokens_code_hash"),
        _ix(("user_id", ASCENDING), ("purpose", ASCENDING), name="ix_email_tokens_user"),
        _ix(("expires_at", ASCENDING), name="ttl_email_tokens_expires", expireAfterSeconds=0),
    ]),
)


def ensure_indexes(db: Database) -> dict[str, list[str]]:
    """Apply `SCHEMA`. Idempotent, so it is safe on every deploy.

    `create_indexes` ignores an index that already exists with the same
    definition and raises if one exists with the *same name but different keys*
    - which is the useful failure, because it means the code and the cluster
    disagree and silently keeping the old one would be worse.
    """
    created: dict[str, list[str]] = {}
    for spec in SCHEMA:
        names = db[spec.collection].create_indexes(spec.indexes)
        created[spec.collection] = names
        logger.info(
            "indexes_ensured",
            extra={"collection": spec.collection, "count": len(names)},
        )
    return created


def index_report(db: Database) -> list[tuple[str, str, bool]]:
    """(collection, index name, present) for every index `SCHEMA` declares.

    Used by `scripts/doctor.py` to say what is missing rather than only that
    something is.
    """
    rows: list[tuple[str, str, bool]] = []
    for spec in SCHEMA:
        existing = set(db[spec.collection].index_information())
        for index in spec.indexes:
            name = index.document["name"]
            rows.append((spec.collection, name, name in existing))
    return rows
