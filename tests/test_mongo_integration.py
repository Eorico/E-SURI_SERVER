"""The parts that hold the deployment together.

That the index definitions cover every rule the system relies on the database
to enforce, that the seed agrees with the workbook it has to fill, that storage
behaves, and that `doctor.py` knows about all of it.

The boundary of what can be checked here is in `conftest.py`: mongomock does
not enforce unique indexes or run TTL deletion, so this asserts that the
definitions are *declared*, and `scripts/doctor.py` asserts they are *applied*.
Both halves are needed - a declared index nobody ran protects nothing.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from openpyxl import load_workbook

from api.core.config import settings
from api.core.indexes import SCHEMA, ensure_indexes, index_report
from api.models.documents import COLLECTIONS
from db import seed_data

BACKEND = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Indexes: the constraints the SQL schema used to declare                      #
# --------------------------------------------------------------------------- #
def indexed() -> dict[str, set[str]]:
    return {spec.collection: {ix.document["name"] for ix in spec.indexes} for spec in SCHEMA}


#: Every uniqueness rule the system depends on the database to enforce.
#:
#: Written out rather than derived from `SCHEMA`, because deriving it would
#: only make the test agree with whatever the code currently says - the point
#: is to notice when one goes missing. Nothing else re-checks these: two
#: accounts on one email, two assessments of the same type for one class, two
#: submissions for one learner.
REQUIRED_UNIQUE_INDEXES: tuple[tuple[str, str], ...] = (
    ("departments", "uq_departments_school_code"),
    ("terms", "uq_terms_school_year_no"),
    ("learning_areas", "uq_learning_areas_school_code"),
    ("sections", "uq_sections_school_name_year"),
    ("users", "uq_users_username"),
    ("users", "uq_users_email"),
    ("students", "uq_students_school_student_no"),
    ("enrollments", "uq_enrollments_student_section"),
    ("classes", "uq_classes_section_area_year"),
    ("classes", "uq_classes_class_code"),
    ("competencies", "uq_competencies_area_code"),
    ("assessments", "uq_assessments_class_type"),
    ("assessments", "uq_assessments_test_code"),
    ("answer_sheets", "uq_answer_sheets_assessment_student"),
    ("submissions", "uq_submissions_assessment_student"),
    ("item_analysis", "uq_item_analysis_assessment_item"),
    ("eqwart_reports", "uq_eqwart_area_type_year"),
    ("report_templates", "uq_templates_year_grade_status"),
    ("certificates", "uq_certificates_student_class"),
)


@pytest.mark.parametrize("collection, name", REQUIRED_UNIQUE_INDEXES)
def test_every_uniqueness_rule_has_an_index(collection, name):
    assert name in indexed().get(
        collection, set()
    ), f"{name} is not declared; nothing would stop a duplicate"


@pytest.mark.parametrize("collection, name", REQUIRED_UNIQUE_INDEXES)
def test_each_of_them_is_actually_declared_unique(collection, name):
    """An index with the right name but no `unique: true` protects nothing."""
    for spec in SCHEMA:
        if spec.collection != collection:
            continue
        for index in spec.indexes:
            if index.document["name"] == name:
                assert index.document.get("unique") is True
                return
    pytest.fail(f"{collection}.{name} not found")


def test_the_two_optional_unique_fields_use_a_partial_index():
    """`username` and `email` are both optional and both unique.

    A plain unique index would admit exactly one document without the field,
    because MongoDB indexes a missing value as null and null collides with
    null. Every teacher registered without a username would then be refused.
    """
    for spec in SCHEMA:
        if spec.collection != "users":
            continue
        for index in spec.indexes:
            if index.document["name"] in ("uq_users_username", "uq_users_email"):
                assert "partialFilterExpression" in index.document, (
                    f"{index.document['name']} must be partial or it admits only "
                    f"one document without the field"
                )


def test_the_two_growing_collections_expire_their_own_rows():
    """Sessions and mailed codes are written on every sign-in and registration.

    Without a TTL index they grow without bound, and nothing else in the system
    deletes them.
    """
    for collection in ("sessions", "email_tokens"):
        has_ttl = any(
            "expireAfterSeconds" in index.document
            for spec in SCHEMA
            if spec.collection == collection
            for index in spec.indexes
        )
        assert has_ttl, f"{collection} has no TTL index"


def test_the_index_spec_names_only_real_collections():
    """A typo would create indexes on a collection nothing ever reads."""
    known = set(COLLECTIONS.values())
    for spec in SCHEMA:
        assert spec.collection in known, f"{spec.collection!r} is not a known collection"


def test_the_fields_screens_filter_on_are_indexed():
    """Without one of these the query becomes a collection scan."""
    for collection, column in [
        ("classes", "ix_classes_teacher"),
        ("classes", "ix_classes_area"),
        ("classes", "ix_classes_term"),
        ("enrollments", "ix_enrollments_section"),
        ("submissions", "ix_submissions_student"),
        ("learning_areas", "ix_learning_areas_department"),
    ]:
        assert column in indexed()[collection]


def test_applying_the_indexes_is_idempotent(db):
    """`sync_indexes.py` runs on every deploy, so twice must equal once."""
    first = ensure_indexes(db)
    second = ensure_indexes(db)
    assert {k: sorted(v) for k, v in first.items()} == {
        k: sorted(v) for k, v in second.items()
    }


def test_the_doctor_reports_on_every_declared_index(db):
    """An index the doctor does not know about can go missing unnoticed."""
    ensure_indexes(db)
    reported = {(collection, name) for collection, name, _ in index_report(db)}
    declared = {
        (spec.collection, index.document["name"])
        for spec in SCHEMA
        for index in spec.indexes
    }
    assert reported == declared


# --------------------------------------------------------------------------- #
# The seed, against the workbook it has to agree with                          #
# --------------------------------------------------------------------------- #
def test_the_seed_sheet_names_match_the_workbook_tabs():
    """`sheet_name` must equal the worksheet tab or the exporter cannot find it.

    This is why the spellings look inconsistent - "Gen Science" but "E.Comm".
    They are transcribed from the DepEd template, and a tidied-up version would
    silently stop that subject being written.
    """
    workbook = load_workbook(BACKEND / settings.EQWART_TEMPLATE_PATH, read_only=True)
    try:
        tabs = set(workbook.sheetnames)
    finally:
        workbook.close()

    for code, _, sheet_name, _ in seed_data.LEARNING_AREAS:
        assert sheet_name in tabs, (
            f"{code} seeds sheet_name {sheet_name!r}, which is not a tab in "
            f"{settings.EQWART_TEMPLATE_PATH}. Tabs: {sorted(tabs)}"
        )


def test_the_seed_has_no_duplicate_codes():
    """Each would be refused by its unique index, so the seed would half-apply."""
    for label, codes in [
        ("departments", [code for code, _ in seed_data.DEPARTMENTS]),
        ("learning areas", [code for code, _, _, _ in seed_data.LEARNING_AREAS]),
        ("sections", list(seed_data.SECTIONS)),
    ]:
        assert len(codes) == len(set(codes)), f"duplicate {label} in the seed"


def test_the_sections_fit_the_sheets_columns():
    """The EQWART sheet has columns B..V for sections - twenty-one of them."""
    assert len(seed_data.SECTIONS) <= 21


def test_seeding_is_rerunnable(db):
    """Re-running is how a renamed subject gets applied, so it must not duplicate."""
    from scripts.seed import upsert

    key = {"school_id": "s", "code": "GENSCI"}
    first = upsert(db["learning_areas"], key, {**key, "name": "General Science"})
    second = upsert(db["learning_areas"], key, {**key, "name": "Gen. Science"})

    assert first == second, "the id must survive, since everything else references it"
    assert db["learning_areas"].count_documents({}) == 1
    assert db["learning_areas"].find_one(key)["name"] == "Gen. Science"


# --------------------------------------------------------------------------- #
# Storage                                                                      #
# --------------------------------------------------------------------------- #
def test_a_signed_url_points_at_this_api_and_carries_a_token():
    """GridFS has no URL of its own, so the API serves the file itself."""
    from api.services.storage_service import GridFsObjectStore

    url = GridFsObjectStore().signed_url(settings.ANSWER_SHEET_BUCKET, "a/b.jpg", 600)
    assert url.startswith(settings.PUBLIC_API_URL.rstrip("/"))
    assert f"{settings.API_PREFIX}/files/" in url


def test_a_file_token_names_one_file_and_nothing_else():
    from api.core.tokens import issue_file_token, read_file_token

    token = issue_file_token(bucket="answer-sheets", path="a/b.jpg", expires_in=600)
    assert read_file_token(token) == ("answer-sheets", "a/b.jpg")


def test_a_file_token_is_not_a_session():
    """A leaked image link must not be usable as a bearer credential."""
    from api.core.exceptions import AuthenticationError
    from api.core.tokens import decode_access_token, issue_file_token

    token = issue_file_token(bucket="answer-sheets", path="a/b.jpg", expires_in=600)
    with pytest.raises(AuthenticationError):
        decode_access_token(token)


def test_an_expired_file_token_is_refused():
    from api.core.exceptions import AuthenticationError
    from api.core.tokens import issue_file_token, read_file_token

    with pytest.raises(AuthenticationError):
        read_file_token(issue_file_token(bucket="b", path="p", expires_in=-10))


def test_a_missing_file_raises_rather_than_returning_empty():
    """An empty result would travel all the way to a blank image."""
    from api.core.exceptions import NotFoundError
    from api.services.storage_service import InMemoryObjectStore

    with pytest.raises(NotFoundError):
        InMemoryObjectStore().open("answer-sheets", "nothing/here.jpg")


def test_bucket_names_are_the_configured_ones():
    assert settings.ANSWER_SHEET_BUCKET == "answer-sheets"
    assert settings.CERTIFICATE_BUCKET == "certificates"
    assert settings.REPORT_TEMPLATE_BUCKET == "report-templates"


def test_a_sheet_is_recompressed_before_it_is_stored():
    """GridFS shares the cluster's storage with every record, so size matters."""
    import cv2
    import numpy as np

    from api.services.storage_service import recompress_sheet

    sheet = np.full((3508, 2480, 3), 250, dtype=np.uint8)
    for row in range(60):
        for col in range(5):
            cv2.circle(sheet, (400 + col * 90, 300 + row * 52), 18, (40, 40, 40), 2)
    original = cv2.imencode(".jpg", sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 92])[
        1
    ].tobytes()

    stored, mime = recompress_sheet(original)
    assert len(stored) < len(original) / 2
    assert mime == "image/jpeg"

    decoded = cv2.imdecode(np.frombuffer(stored, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert decoded is not None, "the retained sheet has to stay viewable"
    assert max(decoded.shape) <= settings.SHEET_MAX_EDGE_PX


def test_recompression_never_grows_a_file():
    """A small or already-compressed upload can encode larger than it arrived."""
    from api.services.storage_service import recompress_sheet

    tiny = b"\xff\xd8\xff" + b"\x00" * 40
    stored, _ = recompress_sheet(tiny)
    assert len(stored) <= len(tiny)


def test_an_unreadable_upload_is_still_stored():
    """Losing the sheet would lose the evidence behind a score."""
    from api.services.storage_service import recompress_sheet

    rubbish = b"\xff\xd8\xff" + b"not really a jpeg" * 20
    stored, _ = recompress_sheet(rubbish)
    assert stored == rubbish


# --------------------------------------------------------------------------- #
# What must not come back                                                      #
# --------------------------------------------------------------------------- #
def test_no_module_reaches_for_a_retired_dependency():
    """These packages are uninstalled, so importing one is a module that cannot run.

    They are named literally because that is the only way to forbid them. A
    stray `from sqlalchemy import ...` pasted in from an old snippet would
    otherwise fail at runtime, on whichever code path happened to reach it
    first.
    """
    offenders = []
    for folder in ("api", "dto", "utils", "scripts"):
        for path in (BACKEND / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if not (stripped.startswith("import ") or stripped.startswith("from ")):
                    continue
                if "supabase" in stripped or "sqlalchemy" in stripped:
                    offenders.append(f"{path.relative_to(BACKEND)}: {stripped}")
    assert not offenders, "importing a retired dependency:\n" + "\n".join(offenders)


def test_no_sql_is_left_in_the_tree():
    """The schema is `api/models/documents.py` and the indexes beside it.

    A stray `.sql` file would be a second description of the schema that
    nothing applies and nothing checks, and the next person to read it would
    have no way to tell it was dead.
    """
    stray = [
        path.relative_to(BACKEND)
        for path in BACKEND.rglob("*.sql")
        if "venv" not in path.parts
    ]
    assert not stray, f"unexpected SQL in the tree: {stray}"


def test_the_accounts_collection_is_called_users():
    """And the class that maps to it is still `Profile`, on purpose.

    The document carries the password hash and the confirmation state, so it
    *is* the account and `users` is what the database should call it. The class
    keeps its name because `Principal` distinguishes `user` - the identity a
    token proves - from `profile`, the staff record read from the database, and
    a request needs both. The mapping is declared once, in `COLLECTIONS`.

    Asserted so that neither half gets "tidied" into the other later.
    """
    from api.models.documents import COLLECTIONS, Profile

    assert COLLECTIONS[Profile] == "users"
    assert "profiles" not in COLLECTIONS.values()


def test_nothing_still_points_at_a_profile_id():
    """A `users` collection reached by `profile_id` is the inconsistency the
    rename exists to remove, so the referencing fields moved with it."""
    from api.models.documents import EmailToken, Session

    for model in (Session, EmailToken):
        assert "user_id" in model.model_fields, model.__name__
        assert "profile_id" not in model.model_fields, model.__name__


def test_every_index_on_the_accounts_collection_is_named_for_it():
    """An index called `uq_profiles_email` on `users` would be a puzzle for
    whoever next reads a slow query's plan."""
    for spec in SCHEMA:
        for index in spec.indexes:
            name = index.document["name"]
            assert "profile" not in name, f"{spec.collection}.{name} still says profile"
