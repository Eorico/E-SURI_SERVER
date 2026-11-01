"""Shared fixtures.

The suite runs against `mongomock`, an in-process MongoDB. That is a deliberate
choice with a known limit: mongomock implements most of the query language but
not the server, so it does **not** enforce unique indexes, run TTL deletion, or
execute `$lookup` the way a real cluster does. What it does do is exercise every
line of the repository layer - the filters, the projections, the embedded-array
reads and writes, the id coercion - with no network and no cluster.

So the division is:

* **Here**, with mongomock: that the code issues the queries it means to, and
  that documents round-trip.
* **`scripts/doctor.py`**, against the real cluster: that the indexes exist and
  are unique, that the TTL is applied, and that the connection works at all.

Anything that needs the server to be real belongs in the second place, and the
tests that would be misleading here say so rather than pretending.
"""

from __future__ import annotations

import uuid
from datetime import date

import mongomock
import pytest

from api.core.config import settings
from api.models import documents as D
from api.models.enums import AssessmentType, UserRole

#: A signing key for the tests. Set here rather than in `.env` so the suite
#: never depends on a developer's local configuration, and never picks up a
#: real one.
TEST_JWT_SECRET = "test-signing-key-not-used-anywhere-real-0123456789"


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET", TEST_JWT_SECRET)


@pytest.fixture(autouse=True)
def empty_cache():
    """A fresh cache per test.

    The cache is process-global by design - one per worker - while each test
    gets its own database. Without this a cached read from one test would be
    served to the next, which looks exactly like a repository returning
    somebody else's data.
    """
    from api.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def db():
    """A fresh in-process database per test."""
    # `tz_aware=True` matches the real client - see `api/core/mongo.py`. Without
    # it the suite would read naive datetimes where production reads aware ones,
    # and the expiry checks would pass here and fail there.
    client = mongomock.MongoClient(tz_aware=True)
    return client["esuri_test"]


@pytest.fixture
def school(db) -> D.School:
    from api.repositories.school_repository import SchoolRepository

    return SchoolRepository(db).add(
        D.School(
            name="Paliparan III Senior High School",
            division="Dasmarinas City",
            cluster="Cluster VIII",
            school_year="2025-2026",
        )
    )


@pytest.fixture
def department(db, school) -> D.Department:
    from api.repositories.base import MongoRepository

    class Departments(MongoRepository[D.Department]):
        model = D.Department

    return Departments(db).add(
        D.Department(school_id=school.id, code="SCIENCE", name="Science")
    )


@pytest.fixture
def learning_area(db, school, department) -> D.LearningArea:
    from api.repositories.school_repository import LearningAreaRepository

    return LearningAreaRepository(db).add(
        D.LearningArea(
            school_id=school.id,
            code="GENSCI",
            name="General Science",
            sheet_name="Gen Sci",
            department_id=department.id,
        )
    )


@pytest.fixture
def section(db, school) -> D.Section:
    from api.repositories.school_repository import SectionRepository

    return SectionRepository(db).add(
        D.Section(
            school_id=school.id, name="ACAD ASSH 11-1", school_year="2025-2026", position=1
        )
    )


@pytest.fixture
def teacher(db, school) -> D.Profile:
    from api.repositories.profile_repository import ProfileRepository

    return ProfileRepository(db).add(
        D.Profile(
            role=UserRole.subject_teacher,
            last_name="Dela Cruz",
            first_name="Maria",
            middle_initial="S",
            username="mdelacruz",
            email="mdelacruz@example.ph",
            school_id=school.id,
        )
    )


@pytest.fixture
def klass(db, section, learning_area, teacher) -> D.Class:
    from api.repositories.class_repository import ClassRepository

    return ClassRepository(db).add(
        D.Class(
            section_id=section.id,
            learning_area_id=learning_area.id,
            teacher_id=teacher.id,
            class_code="ASSH111-GENSCI",
            school_year="2025-2026",
        )
    )


@pytest.fixture
def students(db, school) -> list[D.Student]:
    from api.repositories.student_repository import StudentRepository

    return StudentRepository(db).add_all(
        [
            D.Student(school_id=school.id, student_no=f"2025-{n:04d}", full_name=name)
            for n, name in enumerate(
                ["Abad, Juan", "Bautista, Rosa", "Castro, Mark"], start=1
            )
        ]
    )


@pytest.fixture
def enrolled(db, section, students) -> list[D.Enrollment]:
    from api.repositories.student_repository import EnrollmentRepository

    return EnrollmentRepository(db).add_all(
        [
            D.Enrollment(student_id=student.id, section_id=section.id)
            for student in students
        ]
    )


@pytest.fixture
def assessment(db, klass) -> D.Assessment:
    """A 60-item term exam with a full answer key embedded."""
    from api.repositories.assessment_repository import AssessmentRepository

    return AssessmentRepository(db).add(
        D.Assessment(
            title="Term Exam - General Science",
            type=AssessmentType.term,
            class_id=klass.id,
            item_count=60,
            test_code="GENSCI-TERM-01",
            date=date(2026, 3, 14),
            answer_keys=[
                D.AnswerKey(item_no=item, correct_option="ABCDE"[item % 5])
                for item in range(1, 61)
            ],
        )
    )


def make_responses(key: dict[int, str], correct_up_to: int) -> list[D.Response]:
    """Answers that are right for the first `correct_up_to` items."""
    return [
        D.Response(
            item_no=item,
            marked_option=option if item <= correct_up_to else "E",
            is_correct=item <= correct_up_to,
        )
        for item, option in sorted(key.items())
    ]


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
