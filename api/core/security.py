"""Authentication and authorisation.

This server issues the JWT (`api.core.tokens`); this module verifies it and
resolves the caller's profile.

`RoleChecker` is a callable dependency, so a route declares the roles it needs
declaratively and no controller hand-rolls a role test (DRY, and new roles need
no change here — OCP).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pymongo.database import Database

from api.core.config import settings
from api.core.exceptions import AuthenticationError, PermissionDeniedError
from api.core.logging import user_id_var
from api.core.mongo import get_db
from api.core.tokens import decode_access_token
from api.models.documents import Profile
from api.models.enums import AdminType, UserRole
from api.repositories.profile_repository import ProfileRepository

bearer_scheme = HTTPBearer(auto_error=False, description="E-SURI access token")


@dataclass(frozen=True)
class CurrentUser:
    """The verified identity from the token — no database access involved."""

    id: uuid.UUID
    email: str | None
    session_id: str | None


@dataclass(frozen=True)
class Principal:
    """A verified user together with the profile row that carries their role."""

    user: CurrentUser
    profile: Profile

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def role(self) -> UserRole:
        return self.profile.role

    @property
    def school_id(self) -> uuid.UUID | None:
        return self.profile.school_id

    @property
    def learning_area_id(self) -> uuid.UUID | None:
        return self.profile.learning_area_id

    @property
    def admin_type(self) -> AdminType | None:
        """Which kind of administrator, or None for every other role."""
        return self.profile.admin_type

    @property
    def department_id(self) -> uuid.UUID | None:
        """The department a master teacher oversees; None for everyone else."""
        return self.profile.department_id

    def is_admin_type(self, *types: AdminType) -> bool:
        return self.role is UserRole.administrator and self.admin_type in types

    @property
    def sees_whole_school(self) -> bool:
        """True for the two administrator types whose reports are school-wide.

        A master teacher is deliberately excluded: their analytics are their
        department's, which is the point of the filter.
        """
        return self.is_admin_type(AdminType.assessment_coordinator, AdminType.school_head)

    def is_at_least(self, role: UserRole) -> bool:
        return _RANK[self.role] >= _RANK[role]


#: Privilege ordering, used by `is_at_least` for "coordinator or above" checks.
_RANK: dict[UserRole, int] = {
    UserRole.subject_teacher: 0,
    UserRole.subject_coordinator: 1,
    UserRole.administrator: 2,
}


def decode_token(token: str) -> dict:
    """Verify signature, audience and expiry. Raises on anything unexpected.

    Kept as a thin alias over `api.core.tokens.decode_access_token`: the tests
    and the scripts call it by this name, and the verification rules belong
    beside the code that issues the tokens rather than being stated twice.
    """
    return decode_access_token(token)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> CurrentUser:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Sign in to continue.")

    claims = decode_token(credentials.credentials)
    try:
        user_id = uuid.UUID(str(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Invalid authentication token.") from exc

    user_id_var.set(str(user_id))
    return CurrentUser(
        id=user_id,
        email=claims.get("email"),
        session_id=claims.get("session_id"),
    )


def get_principal(
    user: CurrentUser = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> Principal:
    # One read: the role details are embedded, so there is nothing to join.
    profile = ProfileRepository(db).with_role_details(user.id)
    if profile is None:
        raise PermissionDeniedError("This account has no profile yet. Contact your administrator.")
    if not profile.is_active:
        raise PermissionDeniedError("This account has been deactivated.")
    return Principal(user=user, profile=profile)


class RoleChecker:
    """Dependency enforcing that the caller holds one of the allowed roles."""

    def __init__(self, *allowed: UserRole):
        self.allowed = frozenset(allowed)

    def __call__(self, principal: Principal = Depends(get_principal)) -> Principal:
        if self.allowed and principal.role not in self.allowed:
            raise PermissionDeniedError("Your role does not allow this action.")
        return principal


#: Ready-made dependencies, so routes read as the permission they need.
require_user = RoleChecker()
require_teacher = RoleChecker(
    UserRole.subject_teacher, UserRole.subject_coordinator, UserRole.administrator
)
require_coordinator = RoleChecker(UserRole.subject_coordinator, UserRole.administrator)
require_admin = RoleChecker(UserRole.administrator)


class AdminTypeChecker:
    """Narrows `administrator` to particular kinds of administrator.

    All three types hold the same role, so the role check alone cannot tell
    them apart - a school head reads reports but does not open a term, and a
    master teacher sees one department rather than the school.
    """

    def __init__(self, *allowed: AdminType, reason: str):
        self.allowed = frozenset(allowed)
        self.reason = reason

    def __call__(self, principal: Principal = Depends(require_admin)) -> Principal:
        if principal.admin_type not in self.allowed:
            raise PermissionDeniedError(self.reason)
        return principal


#: Opening and closing a cycle, and releasing certificates, belong to the one
#: person who runs the assessment calendar.
require_assessment_coordinator = AdminTypeChecker(
    AdminType.assessment_coordinator,
    reason="Only the assessment coordinator can do this.",
)

#: Reading the whole school's reports: the coordinator and the school head.
#: A master teacher reads their department through the department filter.
require_school_wide_reader = AdminTypeChecker(
    AdminType.assessment_coordinator,
    AdminType.school_head,
    reason="This report covers the whole school; your account is scoped to a department.",
)


def client_ip(request: Request) -> str:
    """Best-effort client address.

    `X-Forwarded-For` is only trusted when the app sits behind a proxy we
    configured; otherwise a caller could spoof it to dodge rate limits.
    """
    if settings.is_production:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
