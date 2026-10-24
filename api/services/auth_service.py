"""Sign-in, registration, password recovery and profile binding.

The API owns the Supabase credentials. The browser holds no Supabase URL and no
Supabase key — it knows this API's address and nothing else — so every exchange
with Supabase Auth, sign-in and refresh included, happens here. Supabase still
issues and signs the tokens; this service is the only thing allowed to ask it
to, which also means the server, not the client, decides a caller's role.

The Supabase calls sit behind `AuthGateway` so the service is testable without
the SDK or a network (DIP).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from api.core.exceptions import (
    AuthenticationError,
    ConflictError,
    PermissionDeniedError,
    UpstreamError,
    ValidationError,
)
from api.core.logging import get_logger
from api.models.enums import UserRole
from api.models.tables import Profile
from api.repositories.profile_repository import ProfileRepository
from api.repositories.school_repository import LearningAreaRepository, SchoolRepository

logger = get_logger(__name__)


@dataclass(frozen=True)
class AuthUser:
    id: uuid.UUID
    email: str


@dataclass(frozen=True)
class AuthSession:
    """A Supabase session, relayed to the caller.

    The access token is the same JWT `api.core.security` verifies on every
    subsequent request, so nothing downstream changes because the token now
    arrives through this API rather than straight from Supabase.
    """

    access_token: str
    refresh_token: str
    expires_in: int
    user: AuthUser


class AuthGateway(Protocol):
    def create_user(self, email: str, password: str) -> AuthUser: ...
    def delete_user(self, user_id: uuid.UUID) -> None: ...
    def send_password_reset(self, email: str) -> None: ...
    def sign_in(self, email: str, password: str) -> AuthSession: ...
    def refresh(self, refresh_token: str) -> AuthSession: ...
    def sign_out(self, access_token: str) -> None: ...


class SupabaseAuthGateway:
    """Talks to Supabase Auth on the browser's behalf.

    Two clients, deliberately: administrative work (creating and deleting users)
    uses the service-role key, while the sign-in and refresh grants use the anon
    key, so a signed-in user's token carries only their own privileges.
    """

    def __init__(self, client_factory=None, public_client_factory=None):
        self._client_factory = client_factory
        self._public_client_factory = public_client_factory

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory()
        from api.core.supabase import get_supabase  # deferred: optional at import

        return get_supabase()

    def _public_client(self):
        if self._public_client_factory is not None:
            return self._public_client_factory()
        from api.core.supabase import get_supabase_public  # deferred: optional at import

        return get_supabase_public()

    @staticmethod
    def _session_from(response) -> AuthSession:
        """Read the SDK's response into our own type.

        Kept in one place so a change in the SDK's shape is a single edit, and
        so nothing above this layer handles a Supabase object directly.
        """
        session = getattr(response, "session", None)
        user = getattr(response, "user", None) or getattr(session, "user", None)
        access = getattr(session, "access_token", None)
        refresh = getattr(session, "refresh_token", None)
        identifier = getattr(user, "id", None)

        if not access or not refresh or identifier is None:
            raise AuthenticationError("Those credentials were not accepted.")

        return AuthSession(
            access_token=access,
            refresh_token=refresh,
            expires_in=int(getattr(session, "expires_in", 3600) or 3600),
            user=AuthUser(id=uuid.UUID(str(identifier)), email=getattr(user, "email", "") or ""),
        )

    def sign_in(self, email: str, password: str) -> AuthSession:
        try:
            response = self._public_client().auth.sign_in_with_password(
                {"email": email, "password": password}
            )
        except Exception as exc:  # pragma: no cover - network dependent
            # Any failure reads the same to the caller: a wrong password and an
            # unknown address must not be distinguishable.
            logger.info("supabase_sign_in_rejected")
            raise AuthenticationError("Those credentials were not accepted.") from exc
        return self._session_from(response)

    def refresh(self, refresh_token: str) -> AuthSession:
        try:
            response = self._public_client().auth.refresh_session(refresh_token)
        except Exception as exc:  # pragma: no cover - network dependent
            logger.info("supabase_refresh_rejected")
            raise AuthenticationError("That session has expired. Sign in again.") from exc
        return self._session_from(response)

    def sign_out(self, access_token: str) -> None:
        try:
            self._public_client().auth.admin.sign_out(access_token)
        except Exception:  # pragma: no cover - best effort
            # The client discards the token regardless, so a failure here only
            # means the refresh token outlives the sign-out on Supabase's side.
            logger.info("supabase_sign_out_failed")

    def create_user(self, email: str, password: str) -> AuthUser:
        try:
            created = self._client().auth.admin.create_user(
                {"email": email, "password": password, "email_confirm": False}
            )
        except Exception as exc:  # pragma: no cover - network dependent
            logger.exception("supabase_create_user_failed")
            raise UpstreamError("Could not create the account right now.") from exc

        user = getattr(created, "user", created)
        identifier = getattr(user, "id", None)
        if identifier is None:
            raise UpstreamError("Supabase did not return a user id.")
        return AuthUser(id=uuid.UUID(str(identifier)), email=email)

    def delete_user(self, user_id: uuid.UUID) -> None:
        try:
            self._client().auth.admin.delete_user(str(user_id))
        except Exception:  # pragma: no cover - best effort cleanup
            logger.exception("supabase_delete_user_failed", extra={"user_id": str(user_id)})

    def send_password_reset(self, email: str) -> None:
        try:
            self._client().auth.reset_password_email(email)
        except Exception:  # pragma: no cover - network dependent
            # Swallowed on purpose: the caller must not learn whether the
            # address exists, and a mail failure is not the user's problem.
            logger.exception("supabase_password_reset_failed")


class AuthService:
    def __init__(
        self,
        gateway: AuthGateway,
        profiles: ProfileRepository,
        schools: SchoolRepository,
        learning_areas: LearningAreaRepository,
    ):
        self.gateway = gateway
        self.profiles = profiles
        self.schools = schools
        self.learning_areas = learning_areas

    def register(
        self,
        *,
        full_name: str,
        username: str,
        email: str,
        password: str,
        role: UserRole,
        school_id: uuid.UUID | None,
        learning_area_id: uuid.UUID | None,
        allow_privileged: bool = False,
    ) -> Profile:
        """Create the auth user, then its profile.

        Only an administrator may hand out a privileged role; a self-service
        registration is always a subject teacher, whatever the payload asked for.
        """
        email = email.strip().lower()
        username = username.strip().lower()

        if self.profiles.by_email(email) is not None:
            raise ConflictError("An account already uses that email address.")
        if self.profiles.by_username(username) is not None:
            raise ConflictError("That username is taken.")

        effective_role = role if allow_privileged else UserRole.subject_teacher
        if learning_area_id is not None and self.learning_areas.get(learning_area_id) is None:
            raise ValidationError("That learning area does not exist.")

        school_id = school_id or (school.id if (school := self.schools.current()) else None)

        user = self.gateway.create_user(email, password)
        try:
            return self.profiles.add(
                Profile(
                    id=user.id,
                    role=effective_role,
                    full_name=full_name,
                    username=username,
                    email=email,
                    school_id=school_id,
                    learning_area_id=learning_area_id,
                )
            )
        except Exception:
            # Never leave an auth user without a profile — it could not sign in.
            self.gateway.delete_user(user.id)
            raise

    def sign_in(self, *, email: str, password: str) -> tuple[AuthSession, Profile]:
        """Exchange credentials for a session, and refuse a disabled account.

        Supabase knows nothing about the school's own notion of an active
        member of staff, so the profile check happens here: a deactivated
        account gets no session even though its password is still valid.
        """
        session = self.gateway.sign_in(email.strip().lower(), password)
        profile = self._active_profile(session.user.id)
        logger.info("sign_in", extra={"user_id": str(profile.id), "role": profile.role.value})
        return session, profile

    def refresh(self, *, refresh_token: str) -> tuple[AuthSession, Profile]:
        """Renew a session, re-checking the profile so access can be revoked."""
        session = self.gateway.refresh(refresh_token)
        return session, self._active_profile(session.user.id)

    def sign_out(self, *, access_token: str) -> None:
        self.gateway.sign_out(access_token)

    def _active_profile(self, user_id: uuid.UUID) -> Profile:
        profile = self.profiles.get(user_id)
        if profile is None:
            # Authenticated with Supabase but unknown here: the registration
            # never completed, so there is no role to act with.
            raise AuthenticationError("That account is not set up for this school.")
        if not profile.is_active:
            raise PermissionDeniedError("That account has been deactivated. Ask an administrator.")
        return profile

    def request_password_reset(self, email: str) -> None:
        """Always succeeds from the caller's point of view.

        Confirming whether an address is registered would let anyone enumerate
        the school's accounts.
        """
        self.gateway.send_password_reset(email.strip().lower())

    def assign_role(
        self,
        *,
        profile_id: uuid.UUID,
        role: UserRole,
        learning_area_id: uuid.UUID | None = None,
    ) -> Profile:
        """Administrator action: promote or scope a colleague."""
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise ValidationError("That account does not exist.")
        if role is UserRole.subject_coordinator and learning_area_id is None:
            raise ValidationError("A coordinator must be scoped to a learning area.")
        return self.profiles.update(
            profile, role=role, learning_area_id=learning_area_id or profile.learning_area_id
        )

    def set_active(self, profile_id: uuid.UUID, is_active: bool) -> Profile:
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise ValidationError("That account does not exist.")
        return self.profiles.update(profile, is_active=is_active)
