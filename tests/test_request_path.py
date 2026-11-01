"""The request path itself: middleware order, budgets, and cost per request.

These three bugs all showed up in the browser as the same thing — a wall of
failed requests on the dashboard — while having nothing to do with each other.
What they shared was that none of them was visible in any single route's code,
so nothing in the suite would have caught them. They are pinned here.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from api.core.config import settings
from api.core.rate_limit import RateLimitTier, limiter
from api.models import documents as D
from api.models.enums import AdminType, UserRole
from api.http.middleware import INCOMPRESSIBLE
from api.http.middleware.rate_limit import tier_for
from api.main import app, create_app
from api.repositories.profile_repository import ProfileRepository

ORIGIN = "http://localhost:8080"


@pytest.fixture(autouse=True)
def clean_limiter():
    limiter.reset()
    yield
    limiter.reset()


# --------------------------------------------------------------------------- #
# 1. CORS has to be the outermost layer                                        #
# --------------------------------------------------------------------------- #
def test_cors_is_the_outermost_middleware():
    """Registered last, so it wraps everything that can answer early.

    A middleware that replies on its own — the rate limiter does — never calls
    the layers inside it. With CORS inside the limiter, a 429 went back with no
    Access-Control-Allow-Origin, the browser refused to read it, and the console
    showed a CORS error instead of the real cause.
    """
    stack = [m.cls.__name__ for m in create_app().user_middleware]
    assert stack[0] == CORSMiddleware.__name__, (
        f"CORS must be registered last so it is outermost; stack is {stack}"
    )


def test_a_rate_limited_reply_still_carries_cors_headers(monkeypatch):
    """The behaviour the ordering exists for.

    The budget is shrunk rather than exhausted: the policy reads the setting on
    every hit, so this takes the same path a real flood does in a few requests
    instead of a hundred and twenty.
    """
    monkeypatch.setattr(settings, "RATE_LIMIT_SESSION", 3)

    client = TestClient(app)
    for _ in range(settings.RATE_LIMIT_SESSION + 5):
        response = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "not-a-real-token"},
            headers={"Origin": ORIGIN},
        )
        if response.status_code == 429:
            break
    else:
        pytest.fail("the session budget never tripped, so nothing was verified")

    assert response.headers.get("access-control-allow-origin") == ORIGIN
    assert response.headers.get("retry-after")
    assert response.json()["error"]["code"] == "rate_limited"


# --------------------------------------------------------------------------- #
# 2. Renewing a session is not a sign-in attempt                               #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path, expected",
    [
        ("/api/v1/auth/refresh", RateLimitTier.SESSION),
        ("/api/v1/auth/logout", RateLimitTier.SESSION),
        ("/api/v1/auth/login", RateLimitTier.AUTH),
        ("/api/v1/auth/register", RateLimitTier.AUTH),
        ("/api/v1/auth/forgot-password", RateLimitTier.AUTH),
    ],
)
def test_session_upkeep_has_its_own_budget(path, expected):
    """A token expiring mid-session is routine; guessing a password is not.

    Sharing one budget meant a few open tabs renewing their tokens could spend
    the sign-in allowance, and the next real sign-in was refused.
    """

    class FakeRequest:
        def __init__(self, p: str) -> None:
            self.url = type("U", (), {"path": p})()
            self.method = "POST"

    assert tier_for(FakeRequest(path)) == expected


def test_the_session_budget_is_larger_than_the_sign_in_budget():
    assert settings.RATE_LIMIT_SESSION > settings.RATE_LIMIT_AUTH


# --------------------------------------------------------------------------- #
# 3. Authorising a request costs one query, not three                          #
# --------------------------------------------------------------------------- #
def test_loading_a_principal_issues_one_read():
    """`get_principal` runs on every authenticated request.

    `Principal.admin_type` and `Principal.learning_area_id` both read the role
    details. Those are embedded, so this is one read; holding them separately
    would make it three on every authenticated request, which is the hottest
    path in the system.

    The count is asserted rather than assumed, because adding a second read
    here would be invisible in every other test.
    """
    import mongomock

    db = mongomock.MongoClient()["esuri_test"]
    profile = ProfileRepository(db).add(
        D.Profile(
            role=UserRole.administrator,
            last_name="Reyes",
            first_name="Ana",
            username="areyes",
            email="areyes@example.ph",
            administrator_profile=D.AdministratorProfile(
                admin_type=AdminType.master_teacher, department_id=uuid.uuid4()
            ),
        )
    )

    reads: list[str] = []
    for name in ("users", "administrator_profiles", "coordinator_assignments"):
        original = db[name].find_one
        db[name].find_one = (
            lambda *a, _name=name, _original=original, **k: (
                reads.append(_name) or _original(*a, **k)
            )
        )

    loaded = ProfileRepository(db).with_role_details(profile.id)

    assert reads == ["users"], f"expected one read of users, issued {reads}"
    assert loaded.admin_type is AdminType.master_teacher
    assert loaded.department_id is not None


def test_the_role_details_are_not_separate_collections_any_more():
    """Two of the twenty-three tables became fields, which is why it is one read."""
    from api.models.documents import COLLECTIONS

    names = set(COLLECTIONS.values())
    assert "administrator_profiles" not in names
    assert "coordinator_assignments" not in names


# --------------------------------------------------------------------------- #
# 4. Long JSON goes over the wire compressed; downloads do not                 #
# --------------------------------------------------------------------------- #
def test_json_responses_are_compressed():
    """The report and analytics payloads are the slow part of a page load."""
    client = TestClient(app)
    response = client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"


@pytest.mark.parametrize(
    "content_type",
    [
        "application/vnd.ms-excel.sheet.macroEnabled.12",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/pdf",
        "application/zip",
    ],
)
def test_already_compressed_downloads_are_left_alone(content_type):
    """A workbook is a zip file. Gzipping one is CPU spent for nothing."""
    assert content_type in INCOMPRESSIBLE


def test_json_is_not_in_the_exclusion_list():
    """Guards the list above against a paste that disables compression."""
    assert "application/json" not in INCOMPRESSIBLE
