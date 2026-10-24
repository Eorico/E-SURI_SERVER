"""Sign-in, registration, password recovery and the caller's own profile.

This API is the frontend's only credential. The browser is configured with one
value — the address of this service — and holds no Supabase URL or key, so the
sign-in and refresh grants are proxied here with the keys that live in
`backend/.env`. Supabase still issues the tokens; only this service may ask it.

Every route below is on the AUTH rate-limit tier: ten attempts per caller per
five-minute window, applied by path in the rate-limit middleware.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from fastapi.security import HTTPAuthorizationCredentials

from api.core.security import Principal, bearer_scheme, get_principal, require_admin
from api.deps import get_auth_service, get_profiles
from api.models.enums import UserRole
from api.repositories.profile_repository import ProfileRepository
from api.services.auth_service import AuthService, AuthSession
from dto.common import Message
from dto.schemas import (
    LoginRequest,
    PasswordResetRequest,
    ProfileOut,
    ProfileUpdate,
    RefreshRequest,
    RegisterRequest,
    SessionOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _session_response(session: AuthSession, profile) -> SessionOut:
    """One shape for both grants, so the client has a single thing to parse."""
    return SessionOut(
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        expires_in=session.expires_in,
        profile=ProfileOut.model_validate(profile),
    )


@router.post("/login", response_model=SessionOut)
def login(payload: LoginRequest, service: AuthService = Depends(get_auth_service)) -> SessionOut:
    """Exchange credentials for a session.

    The password reaches Supabase through this service and is never stored or
    logged here. A wrong password, an unknown address and a deactivated account
    are reported the same way, so the endpoint cannot be used to discover who
    holds an account at the school.
    """
    session, profile = service.sign_in(email=payload.email, password=payload.password)
    return _session_response(session, profile)


@router.post("/refresh", response_model=SessionOut)
def refresh(payload: RefreshRequest, service: AuthService = Depends(get_auth_service)) -> SessionOut:
    """Renew an expiring session.

    The profile is re-read on every refresh, so revoking an account takes effect
    within one token lifetime rather than whenever the user next signs in.
    """
    session, profile = service.refresh(refresh_token=payload.refresh_token)
    return _session_response(session, profile)


@router.post("/logout", response_model=Message)
def logout(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    service: AuthService = Depends(get_auth_service),
) -> Message:
    """Revoke the session upstream. Succeeds even without a usable token, so
    signing out is never something the user can get stuck on."""
    if credentials is not None:
        service.sign_out(access_token=credentials.credentials)
    return Message(message="Signed out.")


@router.post("/register", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, service: AuthService = Depends(get_auth_service)) -> ProfileOut:
    """Self-service sign-up. Always creates a subject teacher.

    A privileged role is granted afterwards by an administrator, so a caller
    cannot make themselves a coordinator by editing the request.
    """
    profile = service.register(
        full_name=payload.full_name,
        username=payload.username,
        email=payload.email,
        password=payload.password,
        role=payload.role,
        school_id=payload.school_id,
        learning_area_id=payload.learning_area_id,
        allow_privileged=False,
    )
    return ProfileOut.model_validate(profile)


@router.post("/password-reset", response_model=Message)
def request_password_reset(
    payload: PasswordResetRequest, service: AuthService = Depends(get_auth_service)
) -> Message:
    """Always reports success, so the endpoint cannot enumerate accounts."""
    service.request_password_reset(payload.email)
    return Message(message="If that account exists, a reset link is on its way.")


@router.get("/me", response_model=ProfileOut)
def me(principal: Principal = Depends(get_principal)) -> ProfileOut:
    return ProfileOut.model_validate(principal.profile)


@router.patch("/me", response_model=ProfileOut)
def update_me(
    payload: ProfileUpdate,
    principal: Principal = Depends(get_principal),
    profiles: ProfileRepository = Depends(get_profiles),
) -> ProfileOut:
    changes = payload.model_dump(exclude_none=True)
    updated = profiles.update(principal.profile, **changes) if changes else principal.profile
    return ProfileOut.model_validate(updated)


@router.get("/users", response_model=list[ProfileOut])
def list_users(
    _: Principal = Depends(require_admin),
    profiles: ProfileRepository = Depends(get_profiles),
) -> list[ProfileOut]:
    return [ProfileOut.model_validate(p) for p in profiles.list()]


@router.post("/users/{profile_id}/role", response_model=ProfileOut)
def assign_role(
    profile_id: uuid.UUID,
    role: UserRole,
    learning_area_id: uuid.UUID | None = None,
    _: Principal = Depends(require_admin),
    service: AuthService = Depends(get_auth_service),
) -> ProfileOut:
    """Administrator action: promote a colleague and scope them if needed."""
    return ProfileOut.model_validate(
        service.assign_role(profile_id=profile_id, role=role, learning_area_id=learning_area_id)
    )
