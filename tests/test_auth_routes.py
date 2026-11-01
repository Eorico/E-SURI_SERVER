"""The sign-in flow the frontend depends on.

The browser holds no database credential, so these routes are the only way into
the system. What they must guarantee is covered here: a session is issued, the
school's own rules (unknown profile, deactivated account) are applied on top of
the credential check, the password never comes back out, and the attempt budget
is the five-minute one.

The gateway is stubbed, so these run with no database and no network. The real
gateway is exercised against an in-process MongoDB in `test_email_flows.py`,
and the whole stack over HTTP in `test_api_smoke.py`.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from api.core.exceptions import AuthenticationError
from api.core.rate_limit import limiter
from api.deps import get_auth_service
from api.main import app
from api.models.enums import UserRole
from api.services.auth_service import AuthService, AuthSession, AuthUser

PASSWORD = "correct-horse-battery"
USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


class StubProfile:
    """Only the attributes `ProfileOut` reads."""

    def __init__(self, *, is_active: bool = True) -> None:
        self.id = USER_ID
        self.role = UserRole.subject_teacher
        self.last_name = "Dela Cruz"
        self.first_name = "Maria"
        self.middle_initial = "S"
        self.suffix = None
        # Assembled by the real model; the stub states them so ProfileOut can
        # be validated without a database row.
        self.full_name = "Maria S. Dela Cruz"
        self.formal_name = "Dela Cruz, Maria S."
        self.username = "mdelacruz"
        self.email = "maria@paliparan3.deped.gov.ph"
        self.phone = None
        self.is_active = is_active
        self.school_id = None
        self.learning_area_id = None


class StubProfiles:
    def __init__(self, profile: StubProfile | None) -> None:
        self._profile = profile

    def get(self, user_id: uuid.UUID):
        return self._profile if self._profile and user_id == USER_ID else None


class StubGateway:
    """Stands in for the credential layer, and records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def sign_in(self, email: str, password: str) -> AuthSession:
        self.calls.append(("sign_in", email))
        if password != PASSWORD:
            raise AuthenticationError("Those credentials were not accepted.")
        return AuthSession("access-token-1", "refresh-token-1", 3600, AuthUser(USER_ID, email))

    def refresh(self, refresh_token: str) -> AuthSession:
        self.calls.append(("refresh", refresh_token))
        return AuthSession("access-token-2", "refresh-token-2", 3600, AuthUser(USER_ID, "maria@x.ph"))

    def sign_out(self, access_token: str) -> None:
        self.calls.append(("sign_out", access_token))

    def create_user(self, email: str, password: str): ...
    def delete_user(self, user_id: uuid.UUID) -> None: ...
    def send_password_reset(self, email: str) -> None: ...


def use_service(*, profile: StubProfile | None) -> StubGateway:
    """Point the app at a stubbed service and hand back its gateway."""
    gateway = StubGateway()
    service = AuthService(gateway, StubProfiles(profile), None, None, None)
    app.dependency_overrides[get_auth_service] = lambda: service
    return gateway


@pytest.fixture
def client():
    limiter.reset()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_auth_service, None)
    limiter.reset()


def login(client: TestClient, password: str = PASSWORD, email: str = "maria@x.ph"):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def test_login_returns_a_session_and_the_profile(client):
    gateway = use_service(profile=StubProfile())

    response = login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "access-token-1"
    assert body["refresh_token"] == "refresh-token-1"
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    # The workspace can be rendered without a second request.
    assert body["profile"]["username"] == "mdelacruz"
    assert body["profile"]["last_name"] == "Dela Cruz"
    assert body["profile"]["first_name"] == "Maria"
    assert body["profile"]["full_name"] == "Maria S. Dela Cruz"
    assert body["profile"]["role"] == UserRole.subject_teacher.value
    assert gateway.calls == [("sign_in", "maria@x.ph")]


def test_login_never_echoes_the_password(client):
    use_service(profile=StubProfile())
    assert PASSWORD not in login(client).text


def test_wrong_password_is_rejected_without_a_token(client):
    use_service(profile=StubProfile())

    response = login(client, password="wrong")

    assert response.status_code == 401
    assert "access_token" not in response.text


def test_account_unknown_to_this_school_is_rejected(client):
    """The credentials are accepted, but there is no profile to act with."""
    use_service(profile=None)

    assert login(client).status_code == 401


def test_deactivated_account_cannot_sign_in(client):
    """The password is still valid; the school's own rule is what refuses."""
    use_service(profile=StubProfile(is_active=False))

    assert login(client).status_code == 403


def test_refresh_rotates_the_tokens(client):
    use_service(profile=StubProfile())

    response = client.post("/api/v1/auth/refresh", json={"refresh_token": "refresh-token-1"})

    assert response.status_code == 200
    assert response.json()["refresh_token"] == "refresh-token-2"


def test_refresh_rechecks_the_profile(client):
    """Revoking an account takes effect within one token lifetime."""
    use_service(profile=StubProfile(is_active=False))

    response = client.post("/api/v1/auth/refresh", json={"refresh_token": "refresh-token-1"})

    assert response.status_code == 403


def test_logout_revokes_upstream(client):
    gateway = use_service(profile=StubProfile())

    response = client.post("/api/v1/auth/logout", headers={"Authorization": "Bearer access-token-1"})

    assert response.status_code == 200
    assert ("sign_out", "access-token-1") in gateway.calls


def test_logout_without_a_token_still_succeeds(client):
    """Signing out must never be something a user can get stuck on."""
    use_service(profile=StubProfile())

    assert client.post("/api/v1/auth/logout").status_code == 200


def test_malformed_email_is_rejected_before_the_gateway(client):
    gateway = use_service(profile=StubProfile())

    response = client.post("/api/v1/auth/login", json={"email": "nope", "password": "x"})

    assert response.status_code == 422
    assert gateway.calls == []


def test_sign_in_attempts_are_capped_per_five_minute_window(client):
    use_service(profile=StubProfile())

    codes = [login(client, password="wrong").status_code for _ in range(12)]

    assert codes.count(429) == 2, codes
    assert all(code == 401 for code in codes[:10]), codes


def test_rate_limited_response_reports_the_window(client):
    use_service(profile=StubProfile())
    for _ in range(11):
        login(client, password="wrong")

    response = login(client, password="wrong")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "300"


# --------------------------------------------------------------------------- #
# Signing in by username (administrators)                                      #
# --------------------------------------------------------------------------- #
class UsernameProfiles(StubProfiles):
    """Resolves a username the way the real repository does."""

    def by_username(self, username):
        profile = self._profile if hasattr(self, "_profile") else None
        profile = profile or getattr(self, "profile", None)
        if profile is not None and profile.username == username:
            return profile
        return None


def use_username_service(*, profile: StubProfile | None):
    gateway = StubGateway()
    profiles = UsernameProfiles(profile)
    profiles.profile = profile
    service = AuthService(gateway, profiles, None, None, None)
    app.dependency_overrides[get_auth_service] = lambda: service
    return gateway


def test_administrator_can_sign_in_with_a_username(client):
    gateway = use_username_service(profile=StubProfile())

    response = client.post(
        "/api/v1/auth/login", json={"username": "mdelacruz", "password": PASSWORD}
    )

    assert response.status_code == 200, response.text
    # Accounts are keyed by email, so the username had to be resolved first.
    assert gateway.calls == [("sign_in", "maria@paliparan3.deped.gov.ph")]


def test_an_unknown_username_reads_like_a_wrong_password(client):
    """Otherwise the form says which names are taken."""
    use_username_service(profile=StubProfile())

    response = client.post(
        "/api/v1/auth/login", json={"username": "nobody", "password": PASSWORD}
    )

    assert response.status_code == 401
    assert "not accepted" in response.text


def test_sending_both_an_email_and_a_username_is_rejected(client):
    """Which one identifies the account when they disagree? Refuse to guess."""
    use_username_service(profile=StubProfile())

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "maria@x.ph", "username": "mdelacruz", "password": PASSWORD},
    )

    assert response.status_code == 422


def test_sending_neither_is_rejected(client):
    use_username_service(profile=StubProfile())

    response = client.post("/api/v1/auth/login", json={"password": PASSWORD})

    assert response.status_code == 422
