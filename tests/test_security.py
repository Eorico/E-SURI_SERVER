"""The properties that make the system safe rather than merely working.

Each of these pins something that is invisible when it breaks. A timing oracle
leaks accounts while every message stays correct; a mass-assignment hole looks
like a working profile form; a missing header is a header nobody notices is
missing. None of them would show up in a test of what the endpoints return.
"""

from __future__ import annotations

import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from api.core.config import settings
from api.core.exceptions import AuthenticationError
from api.core.passwords import hash_password, verify_password, waste_a_verification
from api.models import documents as D
from api.models.enums import UserRole
from api.repositories.auth_repository import EmailTokenRepository, SessionRepository
from api.repositories.profile_repository import ProfileRepository
from api.services.auth_service import MongoAuthGateway

PASSWORD = "the-real-password-1"


@pytest.fixture
def gateway(db) -> MongoAuthGateway:
    return MongoAuthGateway(
        profiles=ProfileRepository(db),
        sessions=SessionRepository(db),
        email_tokens=EmailTokenRepository(db),
    )


@pytest.fixture
def account(db) -> D.Profile:
    return ProfileRepository(db).add(
        D.Profile(
            last_name="Dela Cruz",
            first_name="Maria",
            username="mdelacruz",
            email="real@example.ph",
            password_hash=hash_password(PASSWORD),
            email_confirmed_at=datetime.now(timezone.utc),
        )
    )


# --------------------------------------------------------------------------- #
# Account enumeration                                                         #
# --------------------------------------------------------------------------- #
class TestEnumeration:
    """Every rejected sign-in must be indistinguishable, by any means."""

    def test_the_messages_are_identical(self, gateway, account):
        messages = set()
        for email, password in [
            ("real@example.ph", "wrong-password-entirely"),
            ("nobody@example.ph", PASSWORD),
            ("nobody@example.ph", "wrong-password-entirely"),
        ]:
            with pytest.raises(AuthenticationError) as caught:
                gateway.sign_in(email, password)
            messages.add(str(caught.value))
        assert len(messages) == 1, f"different wording leaks existence: {messages}"

    def test_the_timing_is_indistinguishable(self, gateway, account):
        """The fix for a real leak, so it is measured rather than assumed.

        An unknown address used to return in 0.0 ms against 83 ms for a real
        one, because the password was only hashed when there was a hash to
        compare against. Identical wording is no defence against a stopwatch,
        so the work is now done either way.
        """

        def median_ms(email: str) -> float:
            samples = []
            for _ in range(6):
                started = time.perf_counter()
                with pytest.raises(AuthenticationError):
                    gateway.sign_in(email, "wrong-password-entirely")
                samples.append((time.perf_counter() - started) * 1000)
            return statistics.median(samples)

        known = median_ms("real@example.ph")
        unknown = median_ms("nobody@example.ph")

        assert known > 5, "the real path should be doing Argon2 work"
        slower, faster = max(known, unknown), max(min(known, unknown), 0.001)
        assert slower / faster < 2.0, (
            f"existing {known:.1f}ms vs unknown {unknown:.1f}ms - the clock "
            f"distinguishes them, so the identical messages achieve nothing"
        )

    def test_an_unknown_username_also_costs_the_same(self, gateway, account):
        """Resolving a username happens before any password check."""

        def timed(call) -> float:
            started = time.perf_counter()
            with pytest.raises(AuthenticationError):
                call()
            return (time.perf_counter() - started) * 1000

        from api.services.auth_service import AuthService

        unknown = timed(
            lambda: AuthService(
                gateway=gateway,
                profiles=gateway.profiles,
                schools=None,
                learning_areas=None,
                assignments=None,
            )._address_for(email=None, username="nobody")
        )
        assert unknown > 5, "an unknown username must not be refused for free"

    def test_waste_a_verification_actually_costs_something(self):
        """If it were optimised away the defence would be gone and silent."""
        started = time.perf_counter()
        waste_a_verification()
        elapsed = (time.perf_counter() - started) * 1000
        assert elapsed > 5, f"only took {elapsed:.2f}ms - that is not Argon2 work"


# --------------------------------------------------------------------------- #
# Brute force                                                                 #
# --------------------------------------------------------------------------- #
class TestBruteForce:
    def test_an_account_locks_itself_after_enough_failures(self, db, gateway, account):
        """The rate limiter keys on the client address, so it does not see one
        account being ground at from many of them."""
        for _ in range(settings.MAX_FAILED_SIGN_INS):
            with pytest.raises(AuthenticationError):
                gateway.sign_in("real@example.ph", "wrong-password-entirely")

        locked = ProfileRepository(db).get(account.id)
        assert locked.failed_sign_ins >= settings.MAX_FAILED_SIGN_INS
        assert locked.is_locked

    def test_the_right_password_is_refused_while_locked(self, gateway, account):
        for _ in range(settings.MAX_FAILED_SIGN_INS):
            with pytest.raises(AuthenticationError):
                gateway.sign_in("real@example.ph", "wrong-password-entirely")

        with pytest.raises(AuthenticationError):
            gateway.sign_in("real@example.ph", PASSWORD)

    def test_being_locked_is_not_announced(self, gateway, account):
        """"Your account is locked" would be the enumeration oracle again."""
        with pytest.raises(AuthenticationError) as before:
            gateway.sign_in("real@example.ph", "wrong-password-entirely")

        for _ in range(settings.MAX_FAILED_SIGN_INS):
            with pytest.raises(AuthenticationError):
                gateway.sign_in("real@example.ph", "wrong-password-entirely")

        with pytest.raises(AuthenticationError) as after:
            gateway.sign_in("real@example.ph", "wrong-password-entirely")

        assert str(before.value) == str(after.value)

    def test_succeeding_clears_the_counter(self, db, gateway, account):
        """Otherwise somebody who mistypes nine times stays one slip from a
        lock for ever."""
        for _ in range(settings.MAX_FAILED_SIGN_INS - 1):
            with pytest.raises(AuthenticationError):
                gateway.sign_in("real@example.ph", "wrong-password-entirely")

        gateway.sign_in("real@example.ph", PASSWORD)
        assert ProfileRepository(db).get(account.id).failed_sign_ins == 0

    def test_a_lock_lapses(self, db, gateway, account):
        profiles = ProfileRepository(db)
        profiles.update(
            account,
            failed_sign_ins=settings.MAX_FAILED_SIGN_INS,
            locked_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        assert not profiles.get(account.id).is_locked
        gateway.sign_in("real@example.ph", PASSWORD)

    def test_resetting_the_password_clears_the_lock(self, db, gateway, account):
        """Reading the mailbox proves more than a password attempt does."""
        for _ in range(settings.MAX_FAILED_SIGN_INS):
            with pytest.raises(AuthenticationError):
                gateway.sign_in("real@example.ph", "wrong-password-entirely")
        assert ProfileRepository(db).get(account.id).is_locked

        gateway.set_password(ProfileRepository(db).get(account.id), "a-new-password-2!")
        refreshed = ProfileRepository(db).get(account.id)
        assert not refreshed.is_locked
        assert refreshed.failed_sign_ins == 0


# --------------------------------------------------------------------------- #
# Passwords                                                                   #
# --------------------------------------------------------------------------- #
class TestPasswords:
    def test_a_hash_reveals_nothing_about_the_password(self):
        stored = hash_password(PASSWORD)
        assert PASSWORD not in stored
        assert stored.startswith("$argon2id$")

    def test_the_same_password_hashes_differently_each_time(self):
        """A per-hash salt, so two people with one password are not visibly alike."""
        assert hash_password(PASSWORD) != hash_password(PASSWORD)

    def test_an_account_with_no_hash_cannot_be_signed_into(self):
        """And is not distinguishable from a wrong password."""
        assert verify_password(PASSWORD, None) is False
        assert verify_password(PASSWORD, "") is False

    def test_a_corrupt_hash_is_refused_rather_than_raising(self):
        assert verify_password(PASSWORD, "not-a-hash-at-all") is False

    def test_a_short_password_is_refused_at_the_hashing_layer(self):
        """The length rule lives here as well as in the DTO, so no path into
        the system - a script, a seed - can store one the form would reject."""
        from api.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            hash_password("short")


# --------------------------------------------------------------------------- #
# Privilege                                                                   #
# --------------------------------------------------------------------------- #
class TestPrivilege:
    def test_the_self_service_form_cannot_change_a_role(self):
        """`PATCH /auth/me` splats this DTO over the caller's own profile, so
        anything it accepts is something a teacher can set on themselves."""
        from dto.schemas import ProfileUpdate

        allowed = set(ProfileUpdate.model_fields)
        forbidden = {
            "role",
            "is_active",
            "password_hash",
            "email",
            "username",
            "school_id",
            "administrator_profile",
            "coordinator_assignment",
            "email_confirmed_at",
            "failed_sign_ins",
            "locked_until",
        }
        assert not (allowed & forbidden), f"a user could set {allowed & forbidden}"

    def test_an_unknown_field_is_refused_rather_than_ignored(self):
        """Forbidding extras is what makes the list above exhaustive."""
        import pydantic

        from dto.schemas import ProfileUpdate

        with pytest.raises(pydantic.ValidationError):
            ProfileUpdate(first_name="Maria", role="administrator")

    def test_a_profile_response_never_carries_a_credential(self):
        from dto.schemas import ProfileOut

        published = set(ProfileOut.model_fields)
        assert "password_hash" not in published
        assert "locked_until" not in published
        assert "failed_sign_ins" not in published


# --------------------------------------------------------------------------- #
# Response hardening                                                          #
# --------------------------------------------------------------------------- #
class TestResponseHeaders:
    def test_the_api_forbids_everything_it_can(self):
        from api.http.middleware.security_headers import API_CSP, BASE_HEADERS

        assert "default-src 'none'" in API_CSP
        assert "frame-ancestors 'none'" in API_CSP
        assert BASE_HEADERS["X-Content-Type-Options"] == "nosniff"
        assert BASE_HEADERS["X-Frame-Options"] == "DENY"
        assert BASE_HEADERS["Referrer-Policy"] == "no-referrer"

    def test_authenticated_json_is_never_cached_by_a_browser(self):
        """Server-side caching is one thing; letting a shared proxy hold one
        teacher's data and hand it to the next is another."""
        from api.http.middleware.security_headers import BASE_HEADERS

        assert BASE_HEADERS["Cache-Control"] == "no-store"


# --------------------------------------------------------------------------- #
# Rate limiting                                                               #
# --------------------------------------------------------------------------- #
class TestRateLimitExemptions:
    def test_liveness_is_free_because_it_touches_nothing(self):
        from api.http.middleware.rate_limit import is_exempt

        assert is_exempt("/api/v1/health")

    def test_readiness_is_metered_because_it_reaches_the_database(self):
        """It was exempt by prefix, which left an unauthenticated endpoint
        anyone could use to make the cluster do work."""
        from api.http.middleware.rate_limit import is_exempt

        assert not is_exempt("/api/v1/health/ready")

    def test_no_auth_route_is_exempt(self):
        from api.http.middleware.rate_limit import is_exempt

        for path in (
            "/api/v1/auth/login",
            "/api/v1/auth/register",
            "/api/v1/auth/verify-code",
            "/api/v1/auth/password-reset",
            "/api/v1/auth/resend-code",
        ):
            assert not is_exempt(path), path

    def test_sign_in_has_the_tightest_budget(self):
        assert settings.RATE_LIMIT_AUTH <= 10
        assert settings.RATE_LIMIT_AUTH < settings.RATE_LIMIT_SESSION
        assert settings.RATE_LIMIT_AUTH < settings.RATE_LIMIT_READ


# --------------------------------------------------------------------------- #
# Secrets                                                                     #
# --------------------------------------------------------------------------- #
def test_no_secret_has_a_usable_default():
    """A default signing key would mean every deployment that forgot to set one
    shared it - and anyone could mint a token for any account."""
    assert settings.JWT_SECRET != ""  # set by the test fixture
    from api.core.config import Settings

    assert Settings.model_fields["JWT_SECRET"].default == ""
    assert Settings.model_fields["SMTP_PASSWORD"].default == ""
    assert Settings.model_fields["OPENAI_API_KEY"].default == ""


def test_production_refuses_a_weak_signing_key():
    import pydantic

    from api.core.config import Settings

    with pytest.raises(pydantic.ValidationError):
        Settings(
            ENVIRONMENT="production",
            MONGODB_URI="mongodb+srv://u:p@real.mongodb.net/",
            JWT_SECRET="too-short",
        )


def test_production_refuses_a_local_cluster():
    import pydantic

    from api.core.config import Settings

    with pytest.raises(pydantic.ValidationError):
        Settings(
            ENVIRONMENT="production",
            MONGODB_URI="mongodb://localhost:27017",
            JWT_SECRET="x" * 48,
        )
