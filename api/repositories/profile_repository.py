"""Profiles — the application-side mirror of a Supabase auth user."""

from __future__ import annotations

import uuid

from api.models.enums import UserRole
from api.models.tables import Profile
from api.repositories.base import SqlRepository


class ProfileRepository(SqlRepository[Profile]):
    model = Profile

    def by_email(self, email: str) -> Profile | None:
        return self.first(email=email.lower())

    def by_username(self, username: str) -> Profile | None:
        return self.first(username=username.lower())

    def by_role(self, role: UserRole) -> list[Profile]:
        return self.list(role=role)

    def teachers_in_learning_area(self, learning_area_id: uuid.UUID) -> list[Profile]:
        return self.list(learning_area_id=learning_area_id)
