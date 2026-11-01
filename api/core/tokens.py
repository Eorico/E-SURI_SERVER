"""Access tokens, refresh tokens and the short codes mailed to people.

Everything that proves who a request is from begins here.

The two token kinds are different on purpose:

* An **access token** is a signed JWT and is never looked up. That is what
  makes it cheap: a request proves who it is from the signature alone, with no
  database read, which is why it is short-lived.
* A **refresh token** is opaque random bytes, recorded in `sessions`. It has to
  be revocable, because signing out must actually end a session - a stateless
  refresh token cannot be withdrawn and stays usable until it lapses.

Only hashes of refresh tokens and codes are stored, for the same reason
passwords are hashed: a leaked database should not hand over working
credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from api.core.config import settings
from api.core.exceptions import AuthenticationError

#: Codes are digits only: they are read off a phone and typed into a form, and
#: a mixed-case code invites "was that an l or a 1?".
_CODE_ALPHABET = "0123456789"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _secret() -> str:
    if not settings.JWT_SECRET:
        raise AuthenticationError("Authentication is not configured on this server.")
    return settings.JWT_SECRET


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    #: The `sessions` document the refresh token belongs to, so sign-out can
    #: find it from the access token alone.
    session_id: uuid.UUID


def digest(value: str) -> str:
    """SHA-256 of a token or code, for storage and lookup.

    Plain SHA-256 rather than a password hash, deliberately: these values are
    long random strings or are rate-limited and single-use, so there is no
    low-entropy guess to slow down - and a lookup by hash has to be fast
    enough to do on every refresh.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def matches(value: str, stored_digest: str) -> bool:
    """Compare in constant time, so a timing difference leaks nothing."""
    return hmac.compare_digest(digest(value), stored_digest)


# --------------------------------------------------------------------------- #
# Access tokens                                                                #
# --------------------------------------------------------------------------- #
def issue_access_token(
    *, user_id: uuid.UUID, email: str | None, session_id: uuid.UUID
) -> tuple[str, int]:
    """A signed JWT and its lifetime in seconds."""
    issued = _now()
    expires = issued + timedelta(seconds=settings.ACCESS_TOKEN_TTL_SECONDS)
    claims = {
        "sub": str(user_id),
        "email": email,
        "session_id": str(session_id),
        "aud": settings.JWT_AUDIENCE,
        "iss": settings.JWT_ISSUER,
        "iat": issued,
        "exp": expires,
        #: Marks this as an access token, so a refresh token can never be
        #: presented as a bearer credential even if both were signed.
        "typ": "access",
    }
    token = jwt.encode(claims, _secret(), algorithm=settings.JWT_ALGORITHM)
    return token, settings.ACCESS_TOKEN_TTL_SECONDS


def decode_access_token(token: str) -> dict:
    """Verify signature, audience and expiry. Raises on anything unexpected."""
    try:
        claims = jwt.decode(
            token,
            _secret(),
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            options={"require": ["exp", "sub"], "verify_exp": True, "verify_aud": True},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Your session has expired. Sign in again.") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid authentication token.") from exc

    # A token minted for another purpose is not a credential for this one.
    if claims.get("typ") not in (None, "access"):
        raise AuthenticationError("Invalid authentication token.")
    return claims


# --------------------------------------------------------------------------- #
# Refresh tokens                                                               #
# --------------------------------------------------------------------------- #
def new_refresh_token() -> str:
    """Opaque, 256 bits of entropy. Never signed, because it is looked up."""
    return secrets.token_urlsafe(32)


def refresh_expiry() -> datetime:
    return _now() + timedelta(seconds=settings.REFRESH_TOKEN_TTL_SECONDS)


# --------------------------------------------------------------------------- #
# Mailed codes                                                                 #
# --------------------------------------------------------------------------- #
def new_code(length: int | None = None) -> str:
    """A numeric code for registration or password reset.

    `secrets.choice`, not `random`: the latter is seeded predictably and a code
    that can be predicted is a code that can be used by somebody else.
    """
    size = length or settings.SIGNUP_CODE_LENGTH
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(size))


def signup_code_expiry() -> datetime:
    return _now() + timedelta(seconds=settings.SIGNUP_CODE_TTL_SECONDS)


def password_reset_expiry() -> datetime:
    return _now() + timedelta(seconds=settings.PASSWORD_RESET_TTL_SECONDS)


def new_reset_token() -> str:
    """The token inside a password-reset link.

    Longer than a mailed code and not numeric, because it is clicked rather
    than typed and so has no reason to be short.
    """
    return secrets.token_urlsafe(32)


def session_id_of(token: str) -> uuid.UUID | None:
    """The session an access token belongs to, accepting an expired one.

    Signing out has to work on a token that has just lapsed. The signature is
    still verified - an unsigned token names somebody else's session, and
    revoking it would be an invitation to sign other people out - but the
    expiry is not, because a lapsed token is exactly what someone clicking
    "sign out" after leaving a tab open all night will be holding.
    """
    try:
        claims = jwt.decode(
            token,
            _secret(),
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            options={"verify_exp": False, "verify_aud": True},
        )
    except jwt.PyJWTError:
        return None

    raw = claims.get("session_id")
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# File access                                                                  #
# --------------------------------------------------------------------------- #
#: Marks a token that grants a read of one stored file and nothing else.
FILE_TOKEN_TYPE = "file"


def issue_file_token(*, bucket: str, path: str, expires_in: int) -> str:
    """A short-lived token naming one stored file.

    GridFS serves bytes over the database connection and has no URL of its
    own, so the API signs a token naming one file and serves that file itself.

    The URL it goes into is fetched by the browser as an `<img src>` or a
    download, which cannot carry an Authorization header - so the capability
    has to be in the URL. That is why it expires quickly and why `typ` pins it
    to reading a file: presented as a bearer credential it is refused, because
    `decode_access_token` accepts only `typ: access`.
    """
    issued = _now()
    claims = {
        "bucket": bucket,
        "path": path,
        "aud": settings.JWT_AUDIENCE,
        "iss": settings.JWT_ISSUER,
        "iat": issued,
        "exp": issued + timedelta(seconds=expires_in),
        "typ": FILE_TOKEN_TYPE,
    }
    return jwt.encode(claims, _secret(), algorithm=settings.JWT_ALGORITHM)


def read_file_token(token: str) -> tuple[str, str]:
    """The (bucket, path) a file token grants, or raise."""
    try:
        claims = jwt.decode(
            token,
            _secret(),
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            options={"require": ["exp"], "verify_exp": True, "verify_aud": True},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("That link has expired. Reload the page.") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("That link is not valid.") from exc

    if claims.get("typ") != FILE_TOKEN_TYPE:
        raise AuthenticationError("That link is not valid.")

    bucket, path = claims.get("bucket"), claims.get("path")
    if not bucket or not path:
        raise AuthenticationError("That link is not valid.")
    return str(bucket), str(path)
