"""Profiles — a member of staff, and the account they sign in with."""

from __future__ import annotations

import uuid

from pymongo import ASCENDING

from api.core.cache import PRINCIPAL_TTL_SECONDS, cached
from api.models.documents import CoordinatorAssignment, Profile
from api.models.enums import UserRole
from api.repositories.base import MongoRepository, as_uuid


class ProfileRepository(MongoRepository[Profile]):
    model = Profile
    #: A hydrated class carries a copy of its teacher's profile.
    depends_on = ("classes",)
    #: Surname first, which is how every DepEd staff list is ordered.
    default_sort = (("last_name", ASCENDING), ("first_name", ASCENDING))

    @cached(ttl=PRINCIPAL_TTL_SECONDS)
    def with_role_details(self, user_id: uuid.UUID) -> Profile | None:
        """The caller's profile and the details that decide what they reach.

        Cached, and this is the entry that matters most: it is read on every
        authenticated request, so the hit removes one round trip from each one.

        Its TTL is the shortest of any (`PRINCIPAL_TTL_SECONDS`) because the
        document carries `role` and `is_active`. A write through
        `ProfileRepository` clears it immediately; the TTL is what bounds how
        long a *different* worker process can go on serving the old answer.
        Deactivating an account therefore takes effect at once in the worker
        that did it and within seconds everywhere else.

        Kept as its own method because `api.core.security.get_principal` calls
        it on every authenticated request, and the name says what that path
        needs. It is now a plain primary-key read: the role details are
        embedded, so there is nothing left to join.

        It is one read because the role details are embedded. Holding them in
        separate collections would make it three - the profile, then the
        administrator record, then the coordinator record - on every single
        authenticated request, which is the worst place in the system to pay
        for a join.
        """
        return self.get(user_id)

    def by_email(self, email: str) -> Profile | None:
        return self.first(email=email.lower())

    def by_username(self, username: str) -> Profile | None:
        return self.first(username=username.lower())

    def by_role(self, role: UserRole) -> list[Profile]:
        return self.list(role=role)

    def teachers_in_learning_area(self, learning_area_id: uuid.UUID) -> list[Profile]:
        """Teachers who hold a class in this learning area.

        A teacher's learning areas come from the classes they teach, not from a
        field on their profile — one teacher may well carry two subjects. So
        the class ids are resolved first (on `ix_classes_area`) and the
        profiles fetched by primary key, which is two indexed reads in place of
        the old join.
        """
        teaching = self.other("classes").find(
            {"learning_area_id": learning_area_id}, projection={"teacher_id": 1}
        )
        teacher_ids = {as_uuid(document["teacher_id"]) for document in teaching}
        if not teacher_ids:
            return []
        cursor = self.collection.find({"_id": {"$in": list(teacher_ids)}}).sort(
            list(self.default_sort)
        )
        return self._many(cursor)

    def in_department(self, department_id: uuid.UUID) -> list[Profile]:
        """The administrators scoped to one department — its master teachers."""
        return self.list(**{"administrator_profile.department_id": department_id})

    def coordinator_for(self, learning_area_id: uuid.UUID) -> Profile | None:
        """The coordinator responsible for a learning area, if one is assigned.

        One query against the embedded assignment: `ix_users_role` narrows it
        and the document carries the area, so there is nothing to join.
        """
        return self.first(
            role=UserRole.subject_coordinator,
            **{"coordinator_assignment.learning_area_id": learning_area_id},
        )


class CoordinatorAssignmentRepository:
    """The one learning area a subject coordinator is responsible for.

    Still its own repository, because the services call it as one and the rule
    it enforces is its own: one area per coordinator, and never on anybody
    else. What changed is where the data sits — embedded in the profile rather
    than in a table keyed to `(id, role)` — so assigning an area is now a
    single-document update and cannot leave a profile and its assignment
    disagreeing about the role.
    """

    def __init__(self, db):
        self.db = db
        self.profiles = ProfileRepository(db)

    def for_user(self, user_id: uuid.UUID) -> CoordinatorAssignment | None:
        profile = self.profiles.get(user_id)
        return profile.coordinator_assignment if profile else None

    def assign(self, profile: Profile, learning_area_id: uuid.UUID) -> CoordinatorAssignment:
        """Create or move the assignment. One area per coordinator."""
        if profile.role is not UserRole.subject_coordinator:
            raise ValueError("Only a subject coordinator can be assigned a learning area.")

        assignment = CoordinatorAssignment(learning_area_id=learning_area_id)
        self.profiles.update(profile, coordinator_assignment=assignment)
        return assignment

    def clear_for(self, profile: Profile) -> None:
        """Remove the assignment when someone stops being a coordinator."""
        self.profiles.update(profile, coordinator_assignment=None)
