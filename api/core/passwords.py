"""Password hashing.

Argon2id, because it is memory-hard: a GPU can try billions of SHA-256 guesses
a second, and bcrypt resists that only by raising its iteration count, while
Argon2id also demands 64MB per guess and so cannot be parallelised cheaply. It
won the Password Hashing Competition and is what OWASP recommends first.

Nothing here logs, returns or formats a password. The only values that leave
are hashes, and `verify` answers a boolean.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from api.core.config import settings
from api.core.exceptions import ValidationError

#: One hasher for the process; it holds no per-call state.
_hasher = PasswordHasher(
    time_cost=settings.ARGON2_TIME_COST,
    memory_cost=settings.ARGON2_MEMORY_COST_KIB,
    parallelism=settings.ARGON2_PARALLELISM,
)


def hash_password(password: str) -> str:
    """Hash a password for storage, refusing one that is too short.

    The length check lives here rather than only in the DTO so that no path
    into the system - a script, a seed, a future endpoint - can store a
    password the sign-in form would have rejected.
    """
    if len(password) < settings.MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"A password must be at least {settings.MIN_PASSWORD_LENGTH} characters."
        )
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Whether the password matches. False for every kind of failure.

    An account with no hash - one migrated before it had a password, or a
    document tampered with - returns False rather than raising, so a caller
    cannot tell "no password set" apart from "wrong password". Telling those
    apart is how an attacker finds the accounts worth attacking.
    """
    if not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


#: A hash of a value nothing can match, built once at import.
#:
#: Verified against when there is no account, so that a sign-in attempt costs
#: the same whether the address exists or not. Without it the endpoint answered
#: an unknown address in 0.0 ms and a real one in 83 ms - the identical error
#: messages were defeated by a stopwatch.
_ABSENT_ACCOUNT_HASH = _hasher.hash(secrets.token_urlsafe(32))


def waste_a_verification() -> None:
    """Spend the time a real password check would take, and discard it.

    Called where a caller would otherwise be refused before any hashing
    happened. It is not a no-op the compiler can remove: Argon2id is a real
    64MB, three-pass computation, which is the whole point.
    """
    try:
        _hasher.verify(_ABSENT_ACCOUNT_HASH, "no account")
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash was made with weaker parameters than we now use.

    Raising the cost in `.env` only protects accounts whose hash is rebuilt, so
    sign-in checks this and re-hashes with the password it has just verified -
    the one moment the plaintext is available.
    """
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True
