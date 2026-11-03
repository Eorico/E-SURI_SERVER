"""The whole stack, over HTTP.

Every other test in this suite calls a layer directly. These go through the
app: routing, DTO validation, the middleware stack, the dependency wiring, the
repositories and the database, in one request each.

That is the gap worth covering after a migration of this size. Each layer can
be correct on its own while the wiring between two of them is wrong - a
provider still handing a service the old gateway, a DTO that no longer matches
what a route returns - and nothing but a real request finds it.
"""

from __future__ import annotations

import re

import mongomock
import pytest
from fastapi.testclient import TestClient

from api.core.config import settings
from api.core.mongo import get_db
from api.deps import get_storage
from api.main import create_app
from api.services.mail_service import NullMailer
from api.services.storage_service import InMemoryObjectStore, StorageService

PASSWORD = "a-long-enough-pass-1!"
EMAIL = "maria@deped.gov.ph"


@pytest.fixture
def mailbox() -> NullMailer:
    return NullMailer()


@pytest.fixture
def client(monkeypatch, mailbox) -> TestClient:
    """The real app, over an in-process database, with mail captured.

    The rate limiter is switched off because these walk a whole flow - six
    auth calls against a ten-per-five-minutes budget - and what is being tested
    here is the wiring, not the budget. `test_request_path.py` covers the
    limiter on its own.
    """
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)

    database = mongomock.MongoClient(tz_aware=True)["esuri_smoke"]
    app = create_app()
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_storage] = lambda: StorageService(InMemoryObjectStore())

    # The mailer is built inside the service, so it is replaced where it is
    # built rather than injected through the route.
    monkeypatch.setattr("api.services.auth_service.build_mailer", lambda: mailbox)

    with TestClient(app) as started:
        started.database = database
        yield started


def register(client: TestClient, **overrides) -> dict:
    payload = {
        "last_name": "Dela Cruz",
        "first_name": "Maria",
        "middle_initial": "S",
        "username": "mdelacruz",
        "email": EMAIL,
        "password": PASSWORD,
        "role": "subject_teacher",
    }
    payload.update(overrides)
    return client.post("/api/v1/auth/register", json=payload)


def code_from(mailbox: NullMailer) -> str:
    match = re.search(r"\b(\d{6})\b", mailbox.sent[-1].text)
    assert match, f"no code in:\n{mailbox.sent[-1].text}"
    return match.group(1)


# --------------------------------------------------------------------------- #
# Liveness                                                                     #
# --------------------------------------------------------------------------- #
def test_the_service_answers(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_the_api_describes_itself(client):
    """The schema is generated from the routes, so this catches a broken DTO."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert len(response.json()["paths"]) > 40


# --------------------------------------------------------------------------- #
# The whole sign-up journey                                                    #
# --------------------------------------------------------------------------- #
def test_register_confirm_sign_in_refresh_sign_out(client, mailbox):
    """One account, all the way through, over HTTP.

    This is the path a teacher actually walks, and the one that broke most
    often while it ran through a separate auth provider.
    """
    created = register(client)
    assert created.status_code == 201, created.text
    profile = created.json()
    assert profile["full_name"] == "Maria S. Dela Cruz"
    assert "password_hash" not in profile
    assert "password" not in profile

    # Not yet confirmed, so not yet allowed in.
    refused = client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )
    assert refused.status_code == 401
    assert "inbox" in refused.json()["error"]["message"].lower()

    confirmed = client.post(
        "/api/v1/auth/verify-code", json={"email": EMAIL, "code": code_from(mailbox)}
    )
    assert confirmed.status_code == 200, confirmed.text
    session = confirmed.json()
    assert session["access_token"]
    assert session["profile"]["id"] == profile["id"]

    # The token works on an authenticated route.
    me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {session['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL

    # Signing in again now succeeds.
    signed_in = client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )
    assert signed_in.status_code == 200
    tokens = signed_in.json()

    renewed = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert renewed.status_code == 200
    assert renewed.json()["refresh_token"] != tokens["refresh_token"], "rotated"

    gone = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {renewed.json()['access_token']}"},
    )
    assert gone.status_code == 200

    # And the session really is over.
    stale = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": renewed.json()["refresh_token"]}
    )
    assert stale.status_code == 401


def test_the_password_reset_journey(client, mailbox):
    """The flow that had no endpoint at all before this migration."""
    register(client)
    client.post(
        "/api/v1/auth/verify-code", json={"email": EMAIL, "code": code_from(mailbox)}
    )
    mailbox.sent.clear()

    asked = client.post("/api/v1/auth/password-reset", json={"email": EMAIL})
    assert asked.status_code == 200

    token = mailbox.sent[0].text.split("token=")[1].split()[0].strip()
    changed = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "password": "a-brand-new-pass-2!"},
    )
    assert changed.status_code == 200, changed.text

    assert (
        client.post(
            "/api/v1/auth/login", json={"email": EMAIL, "password": "a-brand-new-pass-2!"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
        ).status_code
        == 401
    )


def test_an_unknown_address_reveals_nothing(client, mailbox):
    """Both reset and resend must answer the same way for a stranger."""
    for path in ("/api/v1/auth/password-reset", "/api/v1/auth/resend-code"):
        response = client.post(path, json={"email": "stranger@example.com"})
        assert response.status_code == 200
        assert mailbox.sent == []


# --------------------------------------------------------------------------- #
# Authorisation                                                                #
# --------------------------------------------------------------------------- #
def test_an_unauthenticated_request_is_refused(client):
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/classes").status_code in (401, 403)


def test_a_forged_token_is_refused(client):
    """The signature is what proves a request, so an unsigned one is nothing."""
    forged = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMTExMTExMS0xMTExLTExMTEtMTExMS0xMTExMTExMTExMTEiLCJhdWQiOiJhdXRoZW50aWNhdGVkIn0."
        "not-a-real-signature"
    )
    response = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"}
    )
    assert response.status_code == 401


def test_a_teacher_cannot_reach_an_administrator_route(client, mailbox):
    register(client)
    session = client.post(
        "/api/v1/auth/verify-code", json={"email": EMAIL, "code": code_from(mailbox)}
    ).json()

    response = client.get(
        "/api/v1/auth/users",
        headers={"Authorization": f"Bearer {session['access_token']}"},
    )
    assert response.status_code == 403


def test_an_administrator_can(client, mailbox):
    """The same route, with the role that is allowed it."""
    register(client, role="administrator", admin_type="assessment_coordinator")
    session = client.post(
        "/api/v1/auth/verify-code", json={"email": EMAIL, "code": code_from(mailbox)}
    ).json()
    assert session["profile"]["role"] == "administrator", "the role asked for is kept"

    response = client.get(
        "/api/v1/auth/users",
        headers={"Authorization": f"Bearer {session['access_token']}"},
    )
    assert response.status_code == 200
    assert len(response.json()) == 1


# --------------------------------------------------------------------------- #
# Validation and error shape                                                   #
# --------------------------------------------------------------------------- #
def test_a_bad_body_is_reported_per_field(client):
    response = client.post(
        "/api/v1/auth/register", json={"email": "not-an-address", "password": "short"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_an_unknown_field_is_refused_rather_than_ignored(client):
    """Request DTOs forbid extras, so a typo is reported instead of dropped."""
    response = register(client, is_admin=True)
    assert response.status_code == 422


def test_a_duplicate_registration_is_a_conflict(client):
    assert register(client).status_code == 201
    assert register(client).status_code == 409


def test_every_error_uses_the_one_envelope(client):
    """The client parses `error.message`, so an endpoint that answers
    differently shows up as a blank toast rather than a message."""
    for response in (
        client.get("/api/v1/auth/me"),
        client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "x" * 12}),
        client.post("/api/v1/auth/register", json={}),
    ):
        assert response.status_code >= 400
        body = response.json()
        assert "error" in body, body
        assert isinstance(body["error"].get("message"), str)
        assert body["error"].get("code")


# --------------------------------------------------------------------------- #
# Files                                                                        #
# --------------------------------------------------------------------------- #
def test_a_file_link_needs_a_signed_token(client):
    assert client.get("/api/v1/files/not-a-token").status_code == 401


def test_a_signed_link_serves_the_bytes(client):
    """The replacement for a storage provider's signed URL."""
    from api.core.tokens import issue_file_token

    store = InMemoryObjectStore()
    store.upload("answer-sheets", "a/b.jpg", b"\xff\xd8\xff" + b"jpeg", "image/jpeg")
    client.app.dependency_overrides[get_storage] = lambda: StorageService(store)

    token = issue_file_token(bucket="answer-sheets", path="a/b.jpg", expires_in=600)
    response = client.get(f"/api/v1/files/{token}")

    assert response.status_code == 200
    assert response.content.startswith(b"\xff\xd8\xff")
    # The URL is a capability, so a shared cache must not hold the bytes.
    assert "private" in response.headers["cache-control"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_signed_link_to_a_missing_file_is_a_404(client):
    from api.core.tokens import issue_file_token

    token = issue_file_token(bucket="answer-sheets", path="gone.jpg", expires_in=600)
    assert client.get(f"/api/v1/files/{token}").status_code == 404
