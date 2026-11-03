"""Sign-in, registration, password recovery and the caller's own profile.

This API is the frontend's only credential. The browser is configured with one
value - the address of this service - and holds no database URI and no signing
key, so every exchange happens here with what lives in `backend/.env`. This
service issues the tokens as well as verifying them; see `api/core/tokens.py`.

Most routes below are on the AUTH rate-limit tier: ten attempts per caller per
five-minute window. Renewing and ending a session are on SESSION instead, which
is far more generous, because a token expiring mid-session is routine while a
sign-in attempt is the thing worth throttling.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from fastapi.security import HTTPAuthorizationCredentials

from api.core.security import Principal, bearer_scheme, get_principal, require_admin
from api.deps import (
    get_auth_service,
    get_departments,
    get_learning_areas,
    get_profiles,
    get_schools,
)
from api.models.enums import UserRole
from api.repositories.profile_repository import ProfileRepository
from api.services.auth_service import AuthService, AuthSession
from dto.common import Message
from api.repositories.school_repository import (
    DepartmentRepository,
    LearningAreaRepository,
    SchoolRepository,
)
from dto.schemas import (
    DepartmentOut,
    LearningAreaOut,
    LoginRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    ProfileOut,
    ProfileUpdate,
    RefreshRequest,
    RegisterRequest,
    ResendCodeRequest,
    SessionOut,
    SignupOptions,
    VerifyCodeRequest,
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

    Takes an email address, or a username for administrators. Either way the
    password is checked against an Argon2id hash and is never stored in the
    clear, echoed, or logged. A wrong password, an unknown address, an unknown username and
    a deactivated account are reported the same way, so the endpoint cannot be
    used to discover who holds an account at the school.
    """
    session, profile = service.sign_in(
        email=payload.email, username=payload.username, password=payload.password
    )
    return _session_response(session, profile)


@router.post("/verify-code", response_model=SessionOut)
def verify_code(
    payload: VerifyCodeRequest, service: AuthService = Depends(get_auth_service)
) -> SessionOut:
    """Finish a registration with the code that was emailed.

    Verifying confirms the address and signs the person in, so there is no
    second step asking for the password they chose a moment ago.
    """
    session, profile = service.verify_signup_code(email=payload.email, code=payload.code)
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
    """Self-service sign-up, with the role the form asked for.

    The account is created unconfirmed: a six-digit code goes to the address,
    and `/auth/verify-code` is what turns it into a usable account.
    """
    profile = service.register(
        last_name=payload.last_name,
        first_name=payload.first_name,
        middle_initial=payload.middle_initial,
        suffix=payload.suffix,
        username=payload.username,
        email=payload.email,
        password=payload.password,
        role=payload.role,
        school_id=payload.school_id,
        learning_area_id=payload.learning_area_id,
        admin_type=payload.admin_type,
        department_id=payload.department_id,
    )
    return ProfileOut.model_validate(profile)


@router.get("/signup-options", response_model=SignupOptions)
def signup_options(
    departments: DepartmentRepository = Depends(get_departments),
    areas: LearningAreaRepository = Depends(get_learning_areas),
    schools: SchoolRepository = Depends(get_schools),
) -> SignupOptions:
    """The departments and subjects the sign-up form offers.

    Unauthenticated, because it is read before the account exists. It returns
    names only - a school's departments and subjects are its published
    curriculum, so this reveals nothing a prospectus does not, while anything
    that would say who works there stays behind authentication.

    It is on the AUTH rate-limit tier like the rest of this router.
    """
    school = schools.current()
    school_id = school.id if school else None
    return SignupOptions(
        departments=[
            DepartmentOut.model_validate(d)
            for d in (departments.ordered(school_id) if school_id else departments.list())
        ],
        learning_areas=[
            LearningAreaOut.model_validate(a)
            for a in (areas.ordered(school_id) if school_id else areas.list())
        ],
    )


@router.post("/password-reset", response_model=Message)
def request_password_reset(
    payload: PasswordResetRequest, service: AuthService = Depends(get_auth_service)
) -> Message:
    """Always reports success, so the endpoint cannot enumerate accounts."""
    service.request_password_reset(payload.email)
    return Message(message="If that account exists, a reset link is on its way.")


@router.post("/password-reset/confirm", response_model=Message)
def confirm_password_reset(
    payload: PasswordResetConfirm, service: AuthService = Depends(get_auth_service)
) -> Message:
    """Set a new password from a reset link.

    The token is single-use and spent whether or not the new password is
    accepted, so a link cannot be replayed. Completing a reset also ends every
    other session on the account: whoever knew the old password may be holding
    a refresh token, and taking the password away has to take that with it.
    """
    service.complete_password_reset(token=payload.token, password=payload.password)
    return Message(message="Your password has been changed. Sign in with it now.")


@router.post("/resend-code", response_model=Message)
def resend_signup_code(
    payload: ResendCodeRequest, service: AuthService = Depends(get_auth_service)
) -> Message:
    """Mail a fresh registration code.

    Reports success whether or not the address is registered, and whether or
    not it is already confirmed - for the same reason `password-reset` does.
    """
    service.resend_signup_code(email=payload.email)
    return Message(message="If that account is awaiting confirmation, a new code is on its way.")


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


@router.post("/users/{user_id}/role", response_model=ProfileOut)
def assign_role(
    user_id: uuid.UUID,
    role: UserRole,
    learning_area_id: uuid.UUID | None = None,
    _: Principal = Depends(require_admin),
    service: AuthService = Depends(get_auth_service),
) -> ProfileOut:
    """Administrator action: promote a colleague and scope them if needed."""
    return ProfileOut.model_validate(
        service.assign_role(user_id=user_id, role=role, learning_area_id=learning_area_id)
    )
