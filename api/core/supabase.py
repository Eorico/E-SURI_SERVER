"""Supabase clients.

Both keys live here and nowhere else. The browser never holds a Supabase key of
any kind: it only knows this API's URL, and every call that needs Supabase is
made on this side of the wire.

Storage access does not belong here — it goes through `ObjectStore` in
`api.services.storage_service`, which is the one path to a bucket and can be
swapped for an in-memory store in tests.
"""

from functools import lru_cache

from supabase import Client, ClientOptions, create_client

from api.core.config import settings
from api.core.exceptions import UpstreamError

#: Both clients are shared across requests, so neither may carry per-user state.
#:
#: `sign_in_with_password` and `refresh_session` call `_save_session()` on the
#: client, and with the defaults that means two things a server must not have:
#: a "current session" field that whichever request signed in last overwrites,
#: and a background timer thread started per sign-in to refresh a session this
#: process will never use. Both settings off, neither happens. The tokens we
#: hand back are read from the HTTP response, never from client state, so
#: nothing here depends on the client remembering anything.
_STATELESS = ClientOptions(auto_refresh_token=False, persist_session=False)


def _require(name: str, value: str) -> str:
    if not value:
        raise UpstreamError(
            f"{name} is not set. Supabase is not configured on this server; "
            f"add it to backend/.env."
        )
    return value


@lru_cache
def get_supabase() -> Client:
    """Service-role client: administrative auth and Storage.

    Bypasses row-level security, so it must never be used to sign a user in —
    the token it could mint would carry no RLS restrictions at all.
    """
    return create_client(
        _require("SUPABASE_URL", settings.SUPABASE_URL),
        _require("SUPABASE_SERVICE_KEY", settings.SUPABASE_SERVICE_KEY),
        options=_STATELESS,
    )


@lru_cache
def get_supabase_public() -> Client:
    """Anon-key client, used only for the sign-in and refresh grants.

    The same key a browser would have used, held server-side: the session it
    returns carries only that user's own privileges, so row-level security
    still applies to everything the token is later used for.
    """
    return create_client(
        _require("SUPABASE_URL", settings.SUPABASE_URL),
        _require("SUPABASE_ANON_KEY", settings.SUPABASE_ANON_KEY),
        options=_STATELESS,
    )


def reset_clients() -> None:
    """Drop the cached clients, so a test or a reload can rebuild them."""
    get_supabase.cache_clear()
    get_supabase_public.cache_clear()
