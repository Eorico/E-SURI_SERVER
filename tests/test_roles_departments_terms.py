"""The three kinds of administrator, departments, and terms.

What the school actually looks like: an assessment coordinator who runs the
cycle, master teachers who each see one department, and school heads who read
the whole school without administering it. All three hold the `administrator`
role, so the role alone cannot tell them apart - these pin the parts that can.

The rules live on the documents and on `Principal`, so that is what these
assert against.
"""

from __future__ import annotations

import uuid

import pytest

from api.models import documents as D
from api.models.enums import AdminType, TermStatus, UserRole
from db import seed_data


# --------------------------------------------------------------------------- #
# The vocabulary                                                               #
# --------------------------------------------------------------------------- #
def test_administrator_splits_into_three_types():
    assert [kind.value for kind in AdminType] == [
        "assessment_coordinator",
        "master_teacher",
        "school_head",
    ]


def test_the_three_types_all_sit_under_one_role():
    """They are administrators first; the type narrows what they reach."""
    for kind in AdminType:
        details = D.AdministratorProfile(
            admin_type=kind,
            department_id=uuid.uuid4() if kind is AdminType.master_teacher else None,
        )
        assert details.role is UserRole.administrator


def test_administrator_details_cannot_be_attached_to_anyone_else():
    """The rule the composite foreign key to `(id, role)` used to enforce.

    It decides whose reports someone can read, so attaching it to a teacher
    would be a privilege escalation rather than a tidiness problem.
    """
    details = D.AdministratorProfile(admin_type=AdminType.school_head)
    for role in (UserRole.subject_teacher, UserRole.subject_coordinator):
        with pytest.raises(Exception):
            D.Profile(
                last_name="A", first_name="B", role=role, administrator_profile=details
            )


# --------------------------------------------------------------------------- #
# Departments                                                                  #
# --------------------------------------------------------------------------- #
def test_a_subject_belongs_to_a_department():
    """A master teacher's reports are filtered by it."""
    assert "department_id" in D.LearningArea.model_fields


def test_a_subject_may_wait_to_be_placed():
    """A school can add a subject before deciding where it sits."""
    area = D.LearningArea(
        school_id=uuid.uuid4(), code="NEW", name="New Subject", sheet_name="New"
    )
    assert area.department_id is None


def test_the_five_departments_are_seeded():
    codes = {code for code, _ in seed_data.DEPARTMENTS}
    assert codes == {"SOCSCI", "MATHPE", "TECHPRO", "SCIENCE", "COMMARTS"}


def test_every_seeded_subject_is_placed_in_a_real_department():
    """An unplaced subject is invisible to the master teacher who owns it."""
    codes = {code for code, _ in seed_data.DEPARTMENTS}
    for subject_code, _, _, department in seed_data.LEARNING_AREAS:
        assert department in codes, f"{subject_code} names an unknown department {department!r}"


def test_only_a_master_teacher_carries_a_department():
    """Was a CHECK constraint pairing the two columns.

    An assessment coordinator and a school head both see the whole school, so
    there is nothing to scope them to.
    """
    with pytest.raises(Exception):
        D.AdministratorProfile(admin_type=AdminType.master_teacher)

    for kind in (AdminType.assessment_coordinator, AdminType.school_head):
        with pytest.raises(Exception):
            D.AdministratorProfile(admin_type=kind, department_id=uuid.uuid4())


# --------------------------------------------------------------------------- #
# Terms                                                                        #
# --------------------------------------------------------------------------- #
def test_a_class_belongs_to_a_term_not_just_a_year():
    """What stops last term's roster following a teacher into this one."""
    assert "term_id" in D.Class.model_fields


def test_a_term_is_open_or_closed():
    assert [status.value for status in TermStatus] == ["open", "closed"]


def test_a_term_opens_open():
    term = D.Term(school_id=uuid.uuid4(), school_year="2026-2027", term_no=1)
    assert term.status is TermStatus.open


def test_a_term_number_cannot_be_nonsense():
    """Was `CHECK (term_no between 1 and 4)`."""
    for term_no in (0, 5, 7, -1):
        with pytest.raises(Exception):
            D.Term(school_id=uuid.uuid4(), school_year="2026-2027", term_no=term_no)


def test_a_term_reads_as_a_label():
    term = D.Term(school_id=uuid.uuid4(), school_year="2026-2027", term_no=2)
    assert term.label == "Term 2 · 2026-2027"


# --------------------------------------------------------------------------- #
# Who reaches what                                                             #
# --------------------------------------------------------------------------- #
def principal_for(admin_type: AdminType | None, department_id=None):
    from api.core.security import CurrentUser, Principal

    details = (
        D.AdministratorProfile(admin_type=admin_type, department_id=department_id)
        if admin_type
        else None
    )
    profile = D.Profile(
        last_name="Dela Cruz",
        first_name="Maria",
        role=UserRole.administrator if admin_type else UserRole.subject_teacher,
        is_active=True,
        administrator_profile=details,
    )
    return Principal(user=CurrentUser(uuid.uuid4(), "a@b.ph", None), profile=profile)


@pytest.mark.parametrize(
    "admin_type, school_wide",
    [
        (AdminType.assessment_coordinator, True),
        (AdminType.school_head, True),
        # The whole point of the department filter.
        (AdminType.master_teacher, False),
        (None, False),
    ],
)
def test_who_reads_the_whole_school(admin_type, school_wide):
    department = uuid.uuid4() if admin_type is AdminType.master_teacher else None
    assert principal_for(admin_type, department).sees_whole_school is school_wide


def test_a_teacher_has_no_admin_type():
    principal = principal_for(None)
    assert principal.admin_type is None
    assert principal.department_id is None


def test_a_master_teacher_exposes_their_department():
    department = uuid.uuid4()
    assert principal_for(AdminType.master_teacher, department).department_id == department


@pytest.mark.parametrize("admin_type", list(AdminType))
def test_every_type_is_recognised_as_an_administrator(admin_type):
    department = uuid.uuid4() if admin_type is AdminType.master_teacher else None
    principal = principal_for(admin_type, department)
    assert principal.role is UserRole.administrator
    assert principal.is_admin_type(admin_type)


def test_a_type_check_does_not_match_a_different_type():
    principal = principal_for(AdminType.school_head)
    assert not principal.is_admin_type(AdminType.assessment_coordinator)
