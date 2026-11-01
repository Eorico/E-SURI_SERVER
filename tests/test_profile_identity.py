"""How a member of staff is named and scoped.

DepEd records a name in parts and sorts its lists on the surname, so the parts
are what is stored and the display forms are assembled from them. The document
is the only place those rules live, so it is what these assert against.
"""

from __future__ import annotations

import uuid

import pytest

from api.models import documents as D
from api.models.enums import UserRole


def profile(**overrides) -> D.Profile:
    fields = {"last_name": "Dela Cruz", "first_name": "Maria"}
    fields.update(overrides)
    return D.Profile(**fields)


# --------------------------------------------------------------------------- #
# The name is stored in parts                                                  #
# --------------------------------------------------------------------------- #
def test_a_name_is_stored_in_parts_not_as_one_string():
    """`full_name` is derived. Storing it as well would let the two disagree."""
    stored = set(profile().to_bson())
    assert {"last_name", "first_name", "middle_initial", "suffix"} <= stored
    assert "full_name" not in stored
    assert "formal_name" not in stored


def test_only_the_two_real_parts_are_required():
    """Plenty of staff have no middle initial and most have no suffix."""
    assert profile().middle_initial is None
    assert profile().suffix is None

    with pytest.raises(Exception):
        D.Profile(first_name="Maria")
    with pytest.raises(Exception):
        D.Profile(last_name="Dela Cruz")


@pytest.mark.parametrize(
    "overrides, expected_full, expected_formal",
    [
        ({}, "Maria Dela Cruz", "Dela Cruz, Maria"),
        ({"middle_initial": "S"}, "Maria S. Dela Cruz", "Dela Cruz, Maria S."),
        # Already punctuated: it must not become "S.."
        ({"middle_initial": "S."}, "Maria S. Dela Cruz", "Dela Cruz, Maria S."),
        # A surname used as a middle name is not one letter and takes no stop.
        (
            {"middle_initial": "Bautista"},
            "Maria Bautista Dela Cruz",
            "Dela Cruz, Maria Bautista",
        ),
        (
            {"suffix": "Jr."},
            "Maria Dela Cruz, Jr.",
            "Dela Cruz Jr., Maria",
        ),
        (
            {"middle_initial": "S", "suffix": "III"},
            "Maria S. Dela Cruz, III",
            "Dela Cruz III, Maria S.",
        ),
    ],
)
def test_display_names_are_assembled_from_the_parts(
    overrides, expected_full, expected_formal
):
    record = profile(**overrides)
    assert record.full_name == expected_full
    assert record.formal_name == expected_formal


def test_a_compound_surname_survives_the_round_trip(db):
    """Two-word surnames are ordinary here, and must not be split or trimmed."""
    from api.repositories.profile_repository import ProfileRepository

    profiles = ProfileRepository(db)
    saved = profiles.add(
        profile(
            last_name="De los Santos",
            first_name="Juan Miguel",
            middle_initial="R",
            suffix="Jr.",
            username="jdelossantos",
            email="jdelossantos@example.ph",
        )
    )
    loaded = profiles.get(saved.id)
    assert loaded.last_name == "De los Santos"
    assert loaded.formal_name == "De los Santos Jr., Juan Miguel R."


# --------------------------------------------------------------------------- #
# A learning area belongs to the one role that has one                         #
# --------------------------------------------------------------------------- #
def test_a_learning_area_is_not_a_field_on_every_profile():
    """It was a nullable column that could never apply to a teacher.

    Now it is an embedded sub-document present only on a coordinator, so a
    teacher's record carries no field that could not mean anything for them.
    """
    assert "learning_area_id" not in profile().to_bson()
    assert profile().learning_area_id is None


def test_the_assignment_is_pinned_to_the_coordinator_role():
    """The rule the composite foreign key to `(id, role)` used to enforce."""
    area = D.CoordinatorAssignment(learning_area_id=uuid.uuid4())

    allowed = D.Profile(
        last_name="Santos",
        first_name="Jose",
        role=UserRole.subject_coordinator,
        coordinator_assignment=area,
    )
    assert allowed.learning_area_id == area.learning_area_id

    for role in (UserRole.subject_teacher, UserRole.administrator):
        with pytest.raises(Exception):
            D.Profile(
                last_name="Santos",
                first_name="Jose",
                role=role,
                coordinator_assignment=area,
            )


def test_the_area_is_still_readable_straight_off_the_profile():
    """Callers that only want the id should not have to know where it sits."""
    area_id = uuid.uuid4()
    record = D.Profile(
        last_name="Santos",
        first_name="Jose",
        role=UserRole.subject_coordinator,
        coordinator_assignment=D.CoordinatorAssignment(learning_area_id=area_id),
    )
    assert record.learning_area_id == area_id


# --------------------------------------------------------------------------- #
# The account itself                                                           #
# --------------------------------------------------------------------------- #
def test_the_profile_is_the_account():
    """There is no separate auth store, so the credential fields live here.

    This is what makes the orphaned-account state impossible: there is no
    second system for a profile to fall out of step with.
    """
    stored = set(profile().to_bson())
    assert "password_hash" in stored
    assert "email_confirmed_at" in stored


def test_a_password_hash_never_reaches_a_response():
    """The DTO decides what is public, and it has never listed the hash."""
    from dto.schemas import ProfileOut

    assert "password_hash" not in ProfileOut.model_fields
