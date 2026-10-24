"""Authentication and authorisation.

Supabase issues the JWT; this module only verifies it and resolves the caller's
profile. `RoleChecker` is a callable dependency, so a route declares the roles it
needs declaratively and no controller hand-rolls a role test (DRY, and new roles
need no change here — OCP).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from api.core.config import settings
from api.core.database import get_db
from api.core.exceptions import AuthenticationError, PermissionDeniedError
from api.core.logging import user_id_var
from api.models.enums import UserRole
from api.models.tables import Profile
from api.repositories.profile_repository import ProfileRepository

bearer_scheme = HTTPBearer(auto_error=False, description="Supabase access token")


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

    def is_at_least(self, role: UserRole) -> bool:
        return _RANK[self.role] >= _RANK[role]


#: Privilege ordering, used by `is_at_least` for "coordinator or above" checks.
_RANK: dict[UserRole, int] = {
    UserRole.subject_teacher: 0,
    UserRole.subject_coordinator: 1,
    UserRole.administrator: 2,
}


def decode_token(token: str) -> dict:
    """Verify signature, audience and expiry. Raises on anything unexpected."""
    if not settings.SUPABASE_JWT_SECRET:
        raise AuthenticationError("Authentication is not configured on this server.")
    try:
        return jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience=settings.SUPABASE_JWT_AUDIENCE,
            options={"require": ["exp", "sub"], "verify_exp": True, "verify_aud": True},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Your session has expired. Sign in again.") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid authentication token.") from exc


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
    db: Session = Depends(get_db),
) -> Principal:
    profile = ProfileRepository(db).get(user.id)
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
