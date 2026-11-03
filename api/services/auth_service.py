"""Sign-in, registration, password recovery and profile binding.

This server owns authentication outright: passwords, tokens, confirmation
codes and sessions are all issued and checked here, and the browser holds no
credential but this API's address.

A profile *is* an account, which is what makes registration a single document
insert - atomic, with no second system to fall out of step with and nothing to
compensate for if a write fails halfway.

The credential work sits behind `AuthGateway` so the service is testable
without a database (DIP), and `AuthService` reads as a sequence of decisions
rather than as a sequence of calls.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from api.core.config import settings
from api.core.exceptions import (
    AuthenticationError,
    ConflictError,
    PermissionDeniedError,
    ValidationError,
)
from api.core.logging import get_logger
from api.core.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
    waste_a_verification,
)
from api.core import tokens
from api.models.documents import (
    PURPOSE_RESET,
    PURPOSE_SIGNUP,
    AdministratorProfile,
    Profile,
    Session,
)
from api.models.enums import AdminType, UserRole
from api.repositories.auth_repository import EmailTokenRepository, SessionRepository
from api.repositories.profile_repository import (
    CoordinatorAssignmentRepository,
    ProfileRepository,
)
from api.repositories.school_repository import (
    DepartmentRepository,
    LearningAreaRepository,
    SchoolRepository,
)
from api.services.mail_service import (
    Mailer,
    build_mailer,
    confirmation_message,
    password_reset_message,
)

logger = get_logger(__name__)

#: Every rejected sign-in says this, whatever was actually wrong with it. A
#: wrong password, an unknown address and an unknown username must be
#: indistinguishable, or the form becomes a way to discover who holds an
#: account at the school.
_REFUSED = "Those credentials were not accepted."


@dataclass(frozen=True)
class AuthUser:
    id: uuid.UUID
    email: str


@dataclass(frozen=True)
class AuthSession:
    """A session handed back to the caller.

    The access token is the same JWT `api.core.security` verifies on every
    subsequent request; its claims are `sub`, `email` and `session_id`.
    """

    access_token: str
    refresh_token: str
    expires_in: int
    user: AuthUser


@dataclass(frozen=True)
class NewAccount:
    """Credentials for an account that is about to be created.

    Returned before the profile exists, so that the profile can be written in
    one piece with its own password hash. This is what replaced "create the
    auth user, then create its profile".
    """

    id: uuid.UUID
    password_hash: str


@dataclass(frozen=True)
class PendingUser:
    """A registration awaiting confirmation, and the two ways to confirm it.

    Both carry the same token: a code to type into the form the person is
    already looking at, and a link for the one who would rather click. The code
    leads in the email because the form is right there.
    """

    user: AuthUser
    action_link: str
    email_otp: str


class AuthGateway(Protocol):
    """Everything that touches a credential, so `AuthService` touches none."""

    def prepare_account(self, email: str, password: str) -> NewAccount: ...
    def issue_signup_code(self, profile: Profile) -> PendingUser: ...
    def verify_signup_code(self, email: str, code: str) -> AuthSession: ...
    def issue_recovery(self, profile: Profile) -> str: ...
    def complete_recovery(self, token: str, new_password: str) -> Profile: ...
    def find_user_by_email(self, email: str) -> AuthUser | None: ...
    def delete_user(self, user_id: uuid.UUID) -> None: ...
    def set_password(self, profile: Profile, password: str) -> None: ...
    def confirm(self, profile: Profile) -> Profile: ...
    def sign_in(self, email: str, password: str) -> AuthSession: ...
    def refresh(self, refresh_token: str) -> AuthSession: ...
    def sign_out(self, access_token: str) -> None: ...


class MongoAuthGateway:
    """Issues and checks credentials against MongoDB.

    Holds its own repositories rather than borrowing the service's, so the
    service can be handed a stub gateway and tested with no database at all -
    which is how `tests/test_auth_routes.py` runs.
    """

    def __init__(
        self,
        profiles: ProfileRepository,
        sessions: SessionRepository,
        email_tokens: EmailTokenRepository,
    ):
        self.profiles = profiles
        self.sessions = sessions
        self.email_tokens = email_tokens

    # --- account creation -------------------------------------------------
    def prepare_account(self, email: str, password: str) -> NewAccount:
        """An id and a password hash for an account not yet written.

        No I/O: the caller inserts the profile, so there is no half-made
        account to clean up if that insert fails.
        """
        return NewAccount(id=uuid.uuid4(), password_hash=hash_password(password))

    def issue_signup_code(self, profile: Profile) -> PendingUser:
        """Mint and record a confirmation code for an existing profile."""
        if not profile.email:
            raise ValidationError("That account has no email address to confirm.")

        code = tokens.new_code()
        self.email_tokens.issue_signup(
            user_id=profile.id,
            email=profile.email,
            code=code,
            expires_at=tokens.signup_code_expiry(),
        )
        link = f"{settings.PUBLIC_APP_URL}/verify-code?email={profile.email}&code={code}"
        return PendingUser(
            user=AuthUser(id=profile.id, email=profile.email),
            action_link=link,
            email_otp=code,
        )

    def verify_signup_code(self, email: str, code: str) -> AuthSession:
        """Confirm an address and sign the person in, in one step.

        Every failure reads the same to the caller. Saying "that code expired"
        rather than "wrong code" would confirm the address is registered.
        """
        profile = self.profiles.by_email(email)
        if profile is None:
            logger.info("signup_verify_unknown_address")
            raise AuthenticationError("That code was not accepted.")

        token = self.email_tokens.newest_for(profile.id, PURPOSE_SIGNUP)
        if token is None or not token.is_live:
            raise AuthenticationError("That code has expired. Ask for a new one.")

        if not tokens.matches(code, token.code_hash):
            self.email_tokens.record_failure(token)
            raise AuthenticationError("That code was not accepted.")

        self.email_tokens.consume(token)
        self.confirm(profile)
        return self._open_session(profile)

    def confirm(self, profile: Profile) -> Profile:
        """Mark the address proven. Idempotent."""
        if profile.email_confirmed_at is None:
            return self.profiles.update(profile, email_confirmed_at=datetime.now(timezone.utc))
        return profile

    def find_user_by_email(self, email: str) -> AuthUser | None:
        profile = self.profiles.by_email(email)
        if profile is None or not profile.email:
            return None
        return AuthUser(id=profile.id, email=profile.email)

    def delete_user(self, user_id: uuid.UUID) -> None:
        """Remove an account and everything that could still authenticate it.

        The sessions and tokens go too. A profile deleted on its own would
        leave a live refresh token pointing at nothing - the mirror image of
        the orphaned auth user this migration got rid of.
        """
        self.sessions.delete_where(user_id=user_id)
        self.email_tokens.delete_where(user_id=user_id)
        profile = self.profiles.get(user_id)
        if profile is not None:
            self.profiles.delete(profile)

    # --- credentials ------------------------------------------------------
    def set_password(self, profile: Profile, password: str) -> None:
        """Replace the password and end every session it could still open.

        Changing a password has to take away the access someone already has:
        a refresh token issued under the old password would otherwise keep
        working for a fortnight.
        """
        # A new password clears the lock: whoever can read the mailbox has
        # proven more than a password attempt would, and leaving them locked
        # out after a successful reset would be absurd.
        self.profiles.update(
            profile,
            password_hash=hash_password(password),
            failed_sign_ins=0,
            locked_until=None,
        )
        revoked = self.sessions.revoke_all_for(profile.id)
        logger.info(
            "password_changed",
            extra={"user_id": str(profile.id), "sessions_revoked": revoked},
        )

    def sign_in(self, email: str, password: str) -> AuthSession:
        profile = self.profiles.by_email(email)
        if profile is None:
            # Spend the time a real check would take before refusing. The
            # messages are identical by design, and this is what stops a
            # stopwatch telling the two apart anyway.
            waste_a_verification()
            logger.info("sign_in_unknown_address")
            raise AuthenticationError(_REFUSED)

        if profile.is_locked:
            # Refused with the same words as everything else: "this account is
            # locked" would confirm the account exists, which is the thing the
            # wording exists to avoid.
            waste_a_verification()
            logger.warning(
                "sign_in_while_locked",
                extra={"user_id": str(profile.id), "until": profile.locked_until.isoformat()},
            )
            raise AuthenticationError(_REFUSED)

        if not verify_password(password, profile.password_hash):
            self._count_failure(profile)
            raise AuthenticationError(_REFUSED)

        if profile.email_confirmed_at is None:
            # A distinct message, and deliberately so: the password was right,
            # so this tells the owner of the account something they need to
            # know and tells an attacker nothing they did not already prove.
            raise AuthenticationError(
                "Confirm your email address first - check your inbox for the code."
            )

        # The one moment the plaintext is in hand, so the one moment the hash
        # can be rebuilt if the cost settings have since been raised.
        if needs_rehash(profile.password_hash or ""):
            self.profiles.update(profile, password_hash=hash_password(password))

        # One write: the failure counter has to be cleared here or a teacher
        # who mistypes nine times and then succeeds stays one slip from a lock
        # for ever.
        self.profiles.update(
            profile,
            last_sign_in_at=datetime.now(timezone.utc),
            failed_sign_ins=0,
            locked_until=None,
        )
        return self._open_session(profile)

    def _count_failure(self, profile: Profile) -> None:
        """Record a wrong password, and lock the account past the threshold."""
        attempts = profile.failed_sign_ins + 1
        changes: dict = {"failed_sign_ins": attempts}

        if attempts >= settings.MAX_FAILED_SIGN_INS:
            changes["locked_until"] = datetime.now(timezone.utc) + timedelta(
                seconds=settings.SIGN_IN_LOCKOUT_SECONDS
            )
            logger.warning(
                "sign_in_account_locked",
                extra={"user_id": str(profile.id), "attempts": attempts},
            )
        else:
            logger.info(
                "sign_in_wrong_password",
                extra={"user_id": str(profile.id), "attempts": attempts},
            )
        self.profiles.update(profile, **changes)

    def refresh(self, refresh_token: str) -> AuthSession:
        """Renew a session, replacing the refresh token as it goes.

        The old token is revoked and a new one issued. Rotation means a stolen
        refresh token is good for one use at most, and the legitimate client's
        next attempt fails loudly instead of the two sharing a session
        indefinitely.
        """
        session = self.sessions.by_token(tokens.digest(refresh_token))
        if session is None or not session.is_live:
            raise AuthenticationError("Your session has expired. Sign in again.")

        profile = self.profiles.get(session.user_id)
        if profile is None or not profile.is_active:
            self.sessions.revoke(session)
            raise AuthenticationError("Your session has expired. Sign in again.")

        self.sessions.revoke(session)
        return self._open_session(profile)

    def sign_out(self, access_token: str) -> None:
        """End the session the token names. Never raises.

        Signing out must not fail: a caller who is told it did not work has no
        way to try harder, and the honest outcome of "this token is unusable"
        is the one they wanted anyway.
        """
        session_id = tokens.session_id_of(access_token)
        if session_id is None:
            logger.info("sign_out_unreadable_token")
            return
        self.sessions.revoke_by_id(session_id)

    # --- password recovery ------------------------------------------------
    def issue_recovery(self, profile: Profile) -> str:
        """Record a reset token and return the link that carries it."""
        if not profile.email:
            raise ValidationError("That account has no email address.")

        token = tokens.new_reset_token()
        self.email_tokens.issue_reset(
            user_id=profile.id,
            email=profile.email,
            token=token,
            expires_at=tokens.password_reset_expiry(),
        )
        return f"{settings.PUBLIC_APP_URL}/reset-password?token={token}"

    def complete_recovery(self, token: str, new_password: str) -> Profile:
        """Set a new password against a reset token, spending the token."""
        record = self.email_tokens.by_token_hash(tokens.digest(token), PURPOSE_RESET)
        if record is None or not record.is_live:
            raise AuthenticationError(
                "That reset link has expired. Ask for a new one."
            )

        profile = self.profiles.get(record.user_id)
        if profile is None:
            raise AuthenticationError("That reset link is no longer valid.")

        self.email_tokens.consume(record)
        self.set_password(profile, new_password)
        # Someone who can prove they read the mailbox has proven the address,
        # which is the same thing the signup code proves.
        return self.confirm(profile)

    # --- helpers ----------------------------------------------------------
    def _open_session(self, profile: Profile) -> AuthSession:
        """Record a refresh token and mint the access token beside it."""
        refresh_token = tokens.new_refresh_token()
        session = self.sessions.add(
            Session(
                user_id=profile.id,
                token_hash=tokens.digest(refresh_token),
                expires_at=tokens.refresh_expiry(),
            )
        )
        access_token, expires_in = tokens.issue_access_token(
            user_id=profile.id, email=profile.email, session_id=session.id
        )
        return AuthSession(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            user=AuthUser(id=profile.id, email=profile.email or ""),
        )


class AuthService:
    def __init__(
        self,
        gateway: AuthGateway,
        profiles: ProfileRepository,
        schools: SchoolRepository,
        learning_areas: LearningAreaRepository,
        assignments: CoordinatorAssignmentRepository,
        mailer: Mailer | None = None,
        departments: DepartmentRepository | None = None,
    ):
        self.gateway = gateway
        self.profiles = profiles
        self.schools = schools
        self.learning_areas = learning_areas
        self.assignments = assignments
        self.departments = departments
        self.mailer = mailer or build_mailer()

    def register(
        self,
        *,
        last_name: str,
        first_name: str,
        middle_initial: str | None,
        suffix: str | None,
        username: str,
        email: str,
        password: str,
        role: UserRole,
        school_id: uuid.UUID | None,
        learning_area_id: uuid.UUID | None,
        admin_type: AdminType | None = None,
        department_id: uuid.UUID | None = None,
        require_confirmation: bool = True,
    ) -> Profile:
        """Create the account, then mail the code that activates it.

        The account is created with the role that was requested.
        """
        email = email.strip().lower()
        username = username.strip().lower()

        if self.profiles.by_email(email) is not None:
            raise ConflictError("An account already uses that email address.")
        if self.profiles.by_username(username) is not None:
            raise ConflictError("That username is taken.")

        # The role asked for is the role created. Registration is open, so an
        # administrator account can be made by anyone who reaches the form -
        # see `docs/REQUIREMENTS.md` if that needs narrowing to an invite or a
        # school email domain later.
        effective_role = role
        if learning_area_id is not None and self.learning_areas.get(learning_area_id) is None:
            raise ValidationError("That learning area does not exist.")

        # Only a coordinator may hold a learning area, and must hold one.
        if effective_role is UserRole.subject_coordinator and learning_area_id is None:
            raise ValidationError("A coordinator must be scoped to a learning area.")

        # Which kind of administrator, and what they oversee. Checked here as
        # well as in the DTO because this is reachable from the bootstrap
        # script, which does not come through one.
        role_details = self._administrator_details(effective_role, admin_type, department_id)

        school_id = school_id or (school.id if (school := self.schools.current()) else None)

        credentials = self.gateway.prepare_account(email, password)
        profile = self.profiles.add(
            Profile(
                id=credentials.id,
                role=effective_role,
                last_name=last_name,
                first_name=first_name,
                middle_initial=middle_initial,
                suffix=suffix,
                username=username,
                email=email,
                school_id=school_id,
                password_hash=credentials.password_hash,
                administrator_profile=role_details,
                # `require_confirmation=False` is for the first administrator,
                # created on the server by whoever is setting the system up:
                # there is nobody to grant them access and SMTP may not be
                # configured yet, so a code they could never receive would lock
                # the system before it opened.
                email_confirmed_at=None if require_confirmation else datetime.now(timezone.utc),
            )
        )

        if effective_role is UserRole.subject_coordinator:
            self._set_learning_area(profile, learning_area_id)

        # Nobody signs in until they have entered the code, which is what
        # proves the address belongs to them - it is the address every report
        # is sent to.
        if require_confirmation:
            pending = self.gateway.issue_signup_code(profile)
            self.mailer.send(
                confirmation_message(
                    to=email,
                    name=first_name,
                    code=pending.email_otp,
                    link=pending.action_link,
                )
            )
            logger.info("registration_confirmation_sent", extra={"user_id": str(profile.id)})
        return profile

    def verify_signup_code(self, *, email: str, code: str) -> tuple[AuthSession, Profile]:
        """Finish a registration: confirm the address and sign the person in."""
        session = self.gateway.verify_signup_code(email.strip().lower(), code.strip())
        profile = self._active_profile(session.user.id)
        logger.info("signup_verified", extra={"user_id": str(profile.id)})
        return session, profile

    def resend_signup_code(self, *, email: str) -> None:
        """Mail a fresh code, superseding any still outstanding.

        Silent about whether the address is registered, for the same reason
        `request_password_reset` is.
        """
        profile = self.profiles.by_email(email.strip().lower())
        if profile is None or profile.email_confirmed_at is not None:
            logger.info("resend_code_not_applicable")
            return

        pending = self.gateway.issue_signup_code(profile)
        try:
            self.mailer.send(
                confirmation_message(
                    to=profile.email or "",
                    name=profile.first_name,
                    code=pending.email_otp,
                    link=pending.action_link,
                )
            )
        except Exception:
            logger.exception("resend_code_send_failed")

    def sign_in(
        self,
        *,
        password: str,
        email: str | None = None,
        username: str | None = None,
    ) -> tuple[AuthSession, Profile]:
        """Exchange credentials for a session, and refuse a disabled account.

        The school's own notion of an active member of staff is checked on top
        of the password: a deactivated account gets no session even though its
        password is still valid.

        A username is resolved to its address first, because accounts are keyed
        by email. An unknown username reads exactly like a wrong password, so
        the form cannot be used to find out which names are taken.
        """
        address = self._address_for(email=email, username=username)
        session = self.gateway.sign_in(address, password)
        profile = self._active_profile(session.user.id)
        logger.info("sign_in", extra={"user_id": str(profile.id), "role": profile.role.value})
        return session, profile

    def _address_for(self, *, email: str | None, username: str | None) -> str:
        if email:
            return email.strip().lower()

        if not username:
            raise ValidationError("Enter an email address or a username.")

        profile = self.profiles.by_username(username.strip().lower())
        if profile is None or not profile.email:
            # Same reasoning as an unknown address: refuse in the time a real
            # attempt would take, so the clock reveals nothing either.
            waste_a_verification()
            logger.info("sign_in_unknown_username")
            raise AuthenticationError(_REFUSED)

        return profile.email

    def refresh(self, *, refresh_token: str) -> tuple[AuthSession, Profile]:
        """Renew a session, re-checking the profile so access can be revoked."""
        session = self.gateway.refresh(refresh_token)
        return session, self._active_profile(session.user.id)

    def sign_out(self, *, access_token: str) -> None:
        self.gateway.sign_out(access_token)

    def _active_profile(self, user_id: uuid.UUID) -> Profile:
        profile = self.profiles.get(user_id)
        if profile is None:
            raise AuthenticationError("That account is not set up for this school.")
        if not profile.is_active:
            raise PermissionDeniedError("That account has been deactivated. Ask an administrator.")
        return profile

    def request_password_reset(self, email: str) -> None:
        """Always succeeds from the caller's point of view.

        Confirming whether an address is registered would let anyone enumerate
        the school's accounts.
        """
        target = email.strip().lower()
        profile = self.profiles.by_email(target)
        if profile is None:
            logger.info("password_reset_unknown_address")
            return

        link = self.gateway.issue_recovery(profile)
        try:
            self.mailer.send(password_reset_message(to=target, link=link))
        except Exception:
            # A mail failure is not the caller's problem to solve, and telling
            # them would leak that the address exists.
            logger.exception("password_reset_send_failed")

    def complete_password_reset(self, *, token: str, password: str) -> Profile:
        """Set a new password from a reset link.

        The reset page used to be unable to finish the job it looked like it
        finished, because this step lived outside the system. It does not now.
        """
        profile = self.gateway.complete_recovery(token.strip(), password)
        logger.info("password_reset_completed", extra={"user_id": str(profile.id)})
        return profile

    def assign_role(
        self,
        *,
        user_id: uuid.UUID,
        role: UserRole,
        learning_area_id: uuid.UUID | None = None,
    ) -> Profile:
        """Administrator action: promote or scope a colleague."""
        profile = self.profiles.get(user_id)
        if profile is None:
            raise ValidationError("That account does not exist.")

        if role is UserRole.subject_coordinator:
            area_id = learning_area_id or profile.learning_area_id
            # Role first: the embedded assignment is refused on a profile that
            # is not a coordinator, so the role has to change before it is set.
            self.profiles.update(profile, role=role)
            self._set_learning_area(profile, area_id)
        else:
            self.assignments.clear_for(profile)
            self.profiles.update(profile, role=role)

        return profile

    def _administrator_details(
        self,
        role: UserRole,
        admin_type: AdminType | None,
        department_id: uuid.UUID | None,
    ) -> AdministratorProfile | None:
        """Build the embedded administrator record, or refuse the combination."""
        if role is not UserRole.administrator:
            if admin_type is not None:
                raise ValidationError(
                    "Only an administrator account has an administrator type."
                )
            return None

        if admin_type is None:
            raise ValidationError("Choose which kind of administrator this account is.")

        if admin_type is AdminType.master_teacher:
            if department_id is None:
                raise ValidationError("A master teacher oversees one department; choose it.")
            # The department has to exist, or their reports would filter on an
            # id that matches nothing and come back permanently empty.
            if self.departments is not None and self.departments.get(department_id) is None:
                raise ValidationError("That department does not exist.")
        elif department_id is not None:
            raise ValidationError(
                "Only a master teacher is scoped to a department - an assessment "
                "coordinator and a school head see the whole school."
            )

        return AdministratorProfile(admin_type=admin_type, department_id=department_id)

    def _set_learning_area(self, profile: Profile, learning_area_id: uuid.UUID | None) -> None:
        """A coordinator's area is embedded beside the profile, never on it."""
        if learning_area_id is None:
            raise ValidationError("A coordinator must be scoped to a learning area.")
        if self.learning_areas.get(learning_area_id) is None:
            raise ValidationError("That learning area does not exist.")
        self.assignments.assign(profile, learning_area_id)

    def set_active(self, user_id: uuid.UUID, is_active: bool) -> Profile:
        profile = self.profiles.get(user_id)
        if profile is None:
            raise ValidationError("That account does not exist.")
        return self.profiles.update(profile, is_active=is_active)
