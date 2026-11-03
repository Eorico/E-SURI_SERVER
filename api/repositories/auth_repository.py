"""Sessions and mailed tokens.

Both collections carry a TTL index on `expires_at` (see `api/core/indexes.py`),
so MongoDB removes what has lapsed by itself. These are the two
fastest-growing collections in the system - one document per sign-in, one per
registration or reset - and without the TTL they would need a cleanup job
somebody has to remember to write.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pymongo import DESCENDING

from api.models.documents import PURPOSE_RESET, PURPOSE_SIGNUP, EmailToken, Session
from api.repositories.base import MongoRepository

#: How many wrong attempts a mailed code survives.
#:
#: A six-digit code is one of a million, which is minutes of guessing if the
#: attempts are free. Spending the token after a few wrong tries is what makes
#: the short, typeable code safe; the rate limiter alone would not, because it
#: is per caller and an attacker can change callers.
MAX_CODE_ATTEMPTS = 5


class SessionRepository(MongoRepository[Session]):
    """Issued refresh tokens, so that signing out really ends a session."""

    model = Session
    default_sort = (("issued_at", DESCENDING),)

    def by_token(self, token_hash: str) -> Session | None:
        return self.first(token_hash=token_hash)

    def live_for_user(self, user_id: uuid.UUID) -> list[Session]:
        """Sessions that could still be used - every device someone is signed in on."""
        return [
            session
            for session in self.list(user_id=user_id)
            if session.is_live
        ]

    def revoke(self, session: Session) -> Session:
        """End one session. Idempotent: revoking twice is not an error."""
        if session.revoked_at is None:
            return self.update(session, revoked_at=datetime.now(timezone.utc))
        return session

    def revoke_by_id(self, session_id: uuid.UUID) -> bool:
        """End the session an access token names, without reading it first."""
        result = self.collection.update_one(
            {"_id": session_id, "revoked_at": None},
            {"$set": {"revoked_at": datetime.now(timezone.utc)}},
        )
        return result.modified_count == 1

    def revoke_all_for(self, user_id: uuid.UUID) -> int:
        """Sign someone out everywhere.

        Used when a password changes: whoever knew the old one may be holding a
        refresh token, and changing the password has to take that away too.
        """
        result = self.collection.update_many(
            {"user_id": user_id, "revoked_at": None},
            {"$set": {"revoked_at": datetime.now(timezone.utc)}},
        )
        return result.modified_count


class EmailTokenRepository(MongoRepository[EmailToken]):
    """Registration codes and password-reset tokens."""

    model = EmailToken
    default_sort = (("created_at", DESCENDING),)

    def newest_for(self, user_id: uuid.UUID, purpose: str) -> EmailToken | None:
        """The one unspent token of this kind, if there is one.

        Issuing a new code supersedes the previous one (see `supersede`), so at
        most one is ever unspent - a person who asks for a second code must use
        the second code.

        Selected on `consumed_at` rather than by taking the newest by date, and
        that is not a stylistic choice. A BSON datetime holds milliseconds, so
        superseding the old token and writing the new one inside the same
        millisecond gives both the same `created_at`; MongoDB returns ties in
        no defined order, and picking the spent one would tell the person their
        brand-new code had expired - with no way to ever get a working one.
        """
        return self.first(user_id=user_id, purpose=purpose, consumed_at=None)

    def supersede(self, user_id: uuid.UUID, purpose: str) -> int:
        """Spend every outstanding token of this kind before issuing another.

        Without this, requesting a fresh code would leave the old one working,
        so every resend would widen the window rather than restart it.
        """
        result = self.collection.update_many(
            {"user_id": user_id, "purpose": purpose, "consumed_at": None},
            {"$set": {"consumed_at": datetime.now(timezone.utc)}},
        )
        return result.modified_count

    def by_token_hash(self, code_hash: str, purpose: str) -> EmailToken | None:
        """Find a reset token by its value - the link carries no profile id."""
        return self.first(code_hash=code_hash, purpose=purpose)

    def consume(self, token: EmailToken) -> EmailToken:
        """Mark it used, so a code works exactly once."""
        return self.update(token, consumed_at=datetime.now(timezone.utc))

    def record_failure(self, token: EmailToken) -> EmailToken:
        """Count a wrong attempt, and spend the token once there are too many."""
        attempts = token.attempts + 1
        changes: dict = {"attempts": attempts}
        if attempts >= MAX_CODE_ATTEMPTS:
            changes["consumed_at"] = datetime.now(timezone.utc)
        return self.update(token, **changes)

    def issue_signup(
        self, *, user_id: uuid.UUID, email: str, code: str, expires_at: datetime
    ) -> EmailToken:
        from api.core.tokens import digest

        self.supersede(user_id, PURPOSE_SIGNUP)
        return self.add(
            EmailToken(
                user_id=user_id,
                email=email,
                purpose=PURPOSE_SIGNUP,
                code_hash=digest(code),
                expires_at=expires_at,
            )
        )

    def issue_reset(
        self, *, user_id: uuid.UUID, email: str, token: str, expires_at: datetime
    ) -> EmailToken:
        from api.core.tokens import digest

        self.supersede(user_id, PURPOSE_RESET)
        return self.add(
            EmailToken(
                user_id=user_id,
                email=email,
                purpose=PURPOSE_RESET,
                code_hash=digest(token),
                expires_at=expires_at,
            )
        )
