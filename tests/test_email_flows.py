"""Registration, confirmation and password reset, end to end.

These used to stub the sign-in provider, because the token was its to issue and
only delivery was ours. Both halves are ours now, so these run against the real
`MongoAuthGateway` and an in-process database - which means they exercise the
code that actually mints and checks the codes rather than a double that agrees
with it by construction.

What they pin: that an account cannot sign in before it is confirmed, that the
reset endpoint still cannot be used to discover who holds an account, that a
code is single-use and guess-limited, and that no token reaches a log.
"""

from __future__ import annotations

import uuid

import pytest

from api.core.exceptions import AuthenticationError, ConflictError
from api.models import documents as D
from api.models.documents import PURPOSE_RESET, PURPOSE_SIGNUP
from api.models.enums import UserRole
from api.repositories.auth_repository import (
    MAX_CODE_ATTEMPTS,
    EmailTokenRepository,
    SessionRepository,
)
from api.repositories.profile_repository import (
    CoordinatorAssignmentRepository,
    ProfileRepository,
)
from api.repositories.school_repository import LearningAreaRepository, SchoolRepository
from api.services.auth_service import AuthService, MongoAuthGateway
from api.services.mail_service import (
    NullMailer,
    confirmation_message,
    password_reset_message,
)

PASSWORD = "a-long-enough-pass-1!"
EMAIL = "maria@deped.gov.ph"


@pytest.fixture
def service(db) -> AuthService:
    """A real service over a real gateway, with mail captured not sent."""
    profiles = ProfileRepository(db)
    return AuthService(
        gateway=MongoAuthGateway(
            profiles=profiles,
            sessions=SessionRepository(db),
            email_tokens=EmailTokenRepository(db),
        ),
        profiles=profiles,
        schools=SchoolRepository(db),
        learning_areas=LearningAreaRepository(db),
        assignments=CoordinatorAssignmentRepository(db),
        mailer=NullMailer(),
    )


def register(service: AuthService, **overrides) -> D.Profile:
    payload = dict(
        last_name="Dela Cruz",
        first_name="Maria",
        middle_initial="S",
        suffix=None,
        username="mdelacruz",
        # Mixed case and padding on purpose: both are normalised.
        email="  Maria@Deped.gov.PH  ",
        password=PASSWORD,
        role=UserRole.subject_teacher,
        school_id=None,
        learning_area_id=None,
    )
    payload.update(overrides)
    return service.register(**payload)


def mailed_code(service: AuthService) -> str:
    """The code out of the one message that was sent."""
    import re

    sent = service.mailer.sent[-1]
    match = re.search(r"\b(\d{6})\b", sent.text)
    assert match, f"no six-digit code in the email:\n{sent.text}"
    return match.group(1)


# --------------------------------------------------------------------------- #
# Registration                                                                 #
# --------------------------------------------------------------------------- #
class TestRegistration:
    def test_the_account_starts_unconfirmed(self, service):
        """Confirming the address is what proves it belongs to them, and it is
        the address every report is sent to."""
        profile = register(service)
        assert profile.email_confirmed_at is None

    def test_the_address_is_normalised(self, service):
        profile = register(service)
        assert profile.email == EMAIL
        assert profile.username == "mdelacruz"

    def test_the_code_is_emailed(self, service):
        register(service)
        assert len(service.mailer.sent) == 1

        sent = service.mailer.sent[0]
        assert sent.to == EMAIL
        code = mailed_code(service)
        assert code in sent.text
        assert code in sent.html

    def test_the_email_also_carries_a_link(self, service):
        """For anyone who would rather tap than retype."""
        register(service)
        sent = service.mailer.sent[0]
        assert "/verify-code" in sent.text

    def test_the_password_is_hashed_not_stored(self, service, db):
        register(service)
        raw = db["users"].find_one({})
        assert PASSWORD not in str(raw)
        assert raw["password_hash"].startswith("$argon2")

    def test_the_role_asked_for_is_the_role_created(self, service):
        """Registration is open, and an administrator account stays one."""
        profile = register(
            service, role=UserRole.administrator, username="admin1", email="a@b.ph"
        )
        assert profile.role is UserRole.administrator

    def test_a_duplicate_address_is_refused(self, service):
        register(service)
        with pytest.raises(ConflictError):
            register(service, username="different")

    def test_a_duplicate_username_is_refused(self, service):
        register(service)
        with pytest.raises(ConflictError):
            register(service, email="other@deped.gov.ph")

    def test_nothing_is_mailed_when_the_account_could_not_be_written(self, service):
        """Otherwise someone holds a working code for an account that does not exist."""

        def failing(_profile):
            raise RuntimeError("database is down")

        service.profiles.add = failing

        with pytest.raises(RuntimeError):
            register(service)
        assert service.mailer.sent == []

    def test_there_is_no_separate_account_to_orphan(self, service, db):
        """The state that produced "already has an auth user but no profile".

        Registration is one document insert now, so a failure leaves nothing
        behind to reconcile - which is why the compensating delete is gone.
        """

        def failing(_profile):
            raise RuntimeError("database is down")

        service.profiles.add = failing
        with pytest.raises(RuntimeError):
            register(service)

        assert db["users"].count_documents({}) == 0
        assert db["email_tokens"].count_documents({}) == 0

    def test_the_first_administrator_skips_confirmation(self, service):
        """There is nobody to grant them access, and SMTP may not be set up yet."""
        profile = register(
            service,
            role=UserRole.administrator,
            require_confirmation=False,
        )
        assert profile.email_confirmed_at is not None
        assert service.mailer.sent == []

    def test_a_coordinator_must_be_scoped_to_a_subject(self, service):
        from api.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            register(service, role=UserRole.subject_coordinator)


# --------------------------------------------------------------------------- #
# Confirming                                                                   #
# --------------------------------------------------------------------------- #
class TestConfirmation:
    def test_the_right_code_confirms_and_signs_them_in(self, service):
        register(service)
        session, profile = service.verify_signup_code(
            email=EMAIL, code=mailed_code(service)
        )

        assert profile.email_confirmed_at is not None
        assert session.access_token
        assert session.refresh_token

    def test_a_code_works_only_once(self, service):
        register(service)
        code = mailed_code(service)
        service.verify_signup_code(email=EMAIL, code=code)

        with pytest.raises(AuthenticationError):
            service.verify_signup_code(email=EMAIL, code=code)

    def test_a_wrong_code_is_refused(self, service):
        register(service)
        wrong = "000000" if mailed_code(service) != "000000" else "111111"
        with pytest.raises(AuthenticationError):
            service.verify_signup_code(email=EMAIL, code=wrong)

    def test_guessing_is_limited(self, service, db):
        """Six digits is one of a million, which is minutes of free guessing.

        Spending the token after a few wrong tries is what makes a short,
        typeable code safe; the rate limiter would not, because it is per
        caller and an attacker can change callers.
        """
        register(service)
        right = mailed_code(service)
        wrong = "000000" if right != "000000" else "111111"

        for _ in range(MAX_CODE_ATTEMPTS):
            with pytest.raises(AuthenticationError):
                service.verify_signup_code(email=EMAIL, code=wrong)

        # Even the correct code is now dead, so a fresh one must be requested.
        with pytest.raises(AuthenticationError):
            service.verify_signup_code(email=EMAIL, code=right)

    def test_an_unknown_address_reads_like_a_wrong_code(self, service):
        """Saying which one was wrong would confirm the address is registered."""
        with pytest.raises(AuthenticationError):
            service.verify_signup_code(email="stranger@example.com", code="123456")

    def test_resending_supersedes_the_previous_code(self, service):
        register(service)
        first = mailed_code(service)

        service.resend_signup_code(email=EMAIL)
        second = mailed_code(service)

        assert first != second, "a resend must mint a new code"
        with pytest.raises(AuthenticationError):
            service.verify_signup_code(email=EMAIL, code=first)
        service.verify_signup_code(email=EMAIL, code=second)

    def test_resending_to_an_unknown_address_says_nothing(self, service):
        service.resend_signup_code(email="stranger@example.com")
        assert service.mailer.sent == []

    def test_resending_for_a_confirmed_account_says_nothing(self, service):
        register(service)
        service.verify_signup_code(email=EMAIL, code=mailed_code(service))
        service.mailer.sent.clear()

        service.resend_signup_code(email=EMAIL)
        assert service.mailer.sent == []


# --------------------------------------------------------------------------- #
# Signing in                                                                   #
# --------------------------------------------------------------------------- #
class TestSignIn:
    def test_an_unconfirmed_account_is_told_to_check_the_inbox(self, service):
        """"Credentials not accepted" would send them round a loop they cannot
        get out of, because the password is in fact correct."""
        register(service)

        with pytest.raises(AuthenticationError) as caught:
            service.sign_in(email=EMAIL, password=PASSWORD)
        assert "inbox" in str(caught.value).lower()

    def test_a_confirmed_account_signs_in(self, service):
        register(service)
        service.verify_signup_code(email=EMAIL, code=mailed_code(service))

        session, profile = service.sign_in(email=EMAIL, password=PASSWORD)
        assert session.access_token
        assert profile.email == EMAIL

    def test_an_administrator_signs_in_by_username(self, service):
        register(service, role=UserRole.administrator, require_confirmation=False)
        session, profile = service.sign_in(username="mdelacruz", password=PASSWORD)
        assert profile.role is UserRole.administrator
        assert session.access_token

    def test_a_wrong_password_reads_the_same_as_an_unknown_address(self, service):
        """Both must be indistinguishable, or the form finds out who has an account."""
        register(service, require_confirmation=False)

        with pytest.raises(AuthenticationError) as wrong_password:
            service.sign_in(email=EMAIL, password="not-the-password")
        with pytest.raises(AuthenticationError) as unknown:
            service.sign_in(email="stranger@example.com", password=PASSWORD)

        assert str(wrong_password.value) == str(unknown.value)

    def test_an_unknown_username_reads_the_same_way_too(self, service):
        register(service, require_confirmation=False)

        with pytest.raises(AuthenticationError) as unknown:
            service.sign_in(username="nobody", password=PASSWORD)
        with pytest.raises(AuthenticationError) as wrong_password:
            service.sign_in(email=EMAIL, password="not-the-password")

        assert str(unknown.value) == str(wrong_password.value)

    def test_a_deactivated_account_gets_no_session(self, service):
        """The school's own notion of active staff, on top of the password."""
        from api.core.exceptions import PermissionDeniedError

        profile = register(service, require_confirmation=False)
        service.set_active(profile.id, False)

        with pytest.raises(PermissionDeniedError):
            service.sign_in(email=EMAIL, password=PASSWORD)


# --------------------------------------------------------------------------- #
# Sessions                                                                     #
# --------------------------------------------------------------------------- #
class TestSessions:
    def test_refreshing_rotates_the_token(self, service):
        """A stolen refresh token is then good for one use at most."""
        register(service, require_confirmation=False)
        first, _ = service.sign_in(email=EMAIL, password=PASSWORD)

        second, _ = service.refresh(refresh_token=first.refresh_token)
        assert second.refresh_token != first.refresh_token

        with pytest.raises(AuthenticationError):
            service.refresh(refresh_token=first.refresh_token)

    def test_signing_out_really_ends_the_session(self, service):
        """The point of recording refresh tokens rather than leaving them stateless."""
        register(service, require_confirmation=False)
        session, _ = service.sign_in(email=EMAIL, password=PASSWORD)

        service.sign_out(access_token=session.access_token)

        with pytest.raises(AuthenticationError):
            service.refresh(refresh_token=session.refresh_token)

    def test_signing_out_twice_is_not_an_error(self, service):
        register(service, require_confirmation=False)
        session, _ = service.sign_in(email=EMAIL, password=PASSWORD)
        service.sign_out(access_token=session.access_token)
        service.sign_out(access_token=session.access_token)

    def test_signing_out_with_a_token_we_did_not_sign_does_nothing(self, service):
        """Revoking on an unsigned token would be a way to sign other people out."""
        service.sign_out(access_token="not.a.token")

    def test_a_refresh_token_is_not_stored_in_the_clear(self, service, db):
        register(service, require_confirmation=False)
        session, _ = service.sign_in(email=EMAIL, password=PASSWORD)

        stored = db["sessions"].find_one({})
        assert session.refresh_token not in str(stored)
        assert len(stored["token_hash"]) == 64


# --------------------------------------------------------------------------- #
# Password reset                                                               #
# --------------------------------------------------------------------------- #
class TestPasswordReset:
    def test_a_reset_link_is_emailed(self, service):
        register(service, require_confirmation=False)
        service.mailer.sent.clear()

        service.request_password_reset(" Maria@Deped.gov.PH ")

        assert len(service.mailer.sent) == 1
        sent = service.mailer.sent[0]
        assert sent.to == EMAIL
        assert "/reset-password?token=" in sent.text

    def test_an_unknown_address_sends_nothing_and_says_nothing(self, service):
        """Reporting the difference would let anyone enumerate the school's staff."""
        service.request_password_reset("stranger@example.com")
        assert service.mailer.sent == []

    def test_a_mail_failure_does_not_surface_to_the_caller(self, service):
        register(service, require_confirmation=False)

        class Broken:
            sent: list = []

            def send(self, message):
                raise RuntimeError("smtp is down")

        service.mailer = Broken()
        # Must not raise: an error here would reveal that the address exists.
        service.request_password_reset(EMAIL)

    def test_the_link_sets_a_new_password(self, service):
        register(service, require_confirmation=False)
        service.mailer.sent.clear()
        service.request_password_reset(EMAIL)

        token = service.mailer.sent[0].text.split("token=")[1].split()[0].strip()
        service.complete_password_reset(token=token, password="a-brand-new-pass-2!")

        service.sign_in(email=EMAIL, password="a-brand-new-pass-2!")
        with pytest.raises(AuthenticationError):
            service.sign_in(email=EMAIL, password=PASSWORD)

    def test_the_link_works_only_once(self, service):
        register(service, require_confirmation=False)
        service.mailer.sent.clear()
        service.request_password_reset(EMAIL)
        token = service.mailer.sent[0].text.split("token=")[1].split()[0].strip()

        service.complete_password_reset(token=token, password="a-brand-new-pass-2!")
        with pytest.raises(AuthenticationError):
            service.complete_password_reset(token=token, password="another-one-3!")

    def test_an_invented_token_is_refused(self, service):
        with pytest.raises(AuthenticationError):
            service.complete_password_reset(token="made-up", password="whatever-pass-1!")

    def test_changing_the_password_ends_every_other_session(self, service):
        """Whoever knew the old password may hold a refresh token, and taking
        the password away has to take that with it."""
        register(service, require_confirmation=False)
        old_session, _ = service.sign_in(email=EMAIL, password=PASSWORD)

        service.mailer.sent.clear()
        service.request_password_reset(EMAIL)
        token = service.mailer.sent[0].text.split("token=")[1].split()[0].strip()
        service.complete_password_reset(token=token, password="a-brand-new-pass-2!")

        with pytest.raises(AuthenticationError):
            service.refresh(refresh_token=old_session.refresh_token)

    def test_completing_a_reset_also_confirms_the_address(self, service):
        """Reading the mailbox proves the address, same as the signup code does."""
        profile = register(service)
        assert profile.email_confirmed_at is None

        service.mailer.sent.clear()
        service.request_password_reset(EMAIL)
        token = service.mailer.sent[0].text.split("token=")[1].split()[0].strip()
        confirmed = service.complete_password_reset(
            token=token, password="a-brand-new-pass-2!"
        )
        assert confirmed.email_confirmed_at is not None


# --------------------------------------------------------------------------- #
# What the messages say                                                        #
# --------------------------------------------------------------------------- #
class TestMessages:
    def test_the_code_leads_and_the_link_follows(self):
        """The person is looking at the form, so the code is what they need."""
        message = confirmation_message(
            to=EMAIL, name="Maria", code="483921", link="https://app/verify"
        )
        assert message.text.index("483921") < message.text.index("https://app/verify")

    def test_a_reset_message_carries_only_the_link(self):
        message = password_reset_message(to=EMAIL, link="https://app/reset?token=x")
        assert "https://app/reset?token=x" in message.text
        assert message.to == EMAIL

    def test_no_token_is_written_to_a_log(self, service, caplog):
        """A link in a log file is a working credential sitting in a log file."""
        import logging

        with caplog.at_level(logging.DEBUG):
            register(service)
            code = mailed_code(service)
            service.request_password_reset(EMAIL)

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert code not in logged
        assert "token=" not in logged
        assert PASSWORD not in logged


# --------------------------------------------------------------------------- #
# Expiry                                                                       #
# --------------------------------------------------------------------------- #
def test_tokens_record_when_they_lapse(db, service):
    """A TTL index deletes them, so the field has to be there to index."""
    register(service)
    token = db["email_tokens"].find_one({"purpose": PURPOSE_SIGNUP})
    assert token["expires_at"] is not None

    service.request_password_reset(EMAIL)
    reset = db["email_tokens"].find_one({"purpose": PURPOSE_RESET})
    assert reset["expires_at"] is not None


def test_a_lapsed_code_is_refused(db, service, monkeypatch):
    from datetime import datetime, timedelta, timezone

    register(service)
    code = mailed_code(service)

    db["email_tokens"].update_one(
        {"purpose": PURPOSE_SIGNUP},
        {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}},
    )
    with pytest.raises(AuthenticationError):
        service.verify_signup_code(email=EMAIL, code=code)
