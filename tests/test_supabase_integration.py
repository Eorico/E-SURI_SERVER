"""The seams where this server meets Supabase.

These do not need a project: they pin the decisions that were wrong before, so
a later change cannot quietly undo them.

* The shared auth clients must be stateless, or concurrent sign-ins overwrite
  each other's session on the cached client and each one starts a background
  refresh thread for a session this process never uses.
* Sign-in must use the anon key. The service-role key bypasses row-level
  security, so a token minted with it would carry no restrictions at all.
* A missing storage object must raise, not return an empty URL that travels to
  a broken download button.
* The generated migrations must match the models.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from api.core.exceptions import UpstreamError
from api.models.tables import Base
from api.services.storage_service import SupabaseObjectStore

MIGRATIONS = Path(__file__).resolve().parent.parent / "supabase" / "migrations"


# --------------------------------------------------------------------------- #
# Clients                                                                      #
# --------------------------------------------------------------------------- #
def test_auth_clients_are_stateless():
    """`_STATELESS` is what stops the cached client holding a user's session."""
    from api.core.supabase import _STATELESS

    assert _STATELESS.auto_refresh_token is False, (
        "auto_refresh_token must stay off: sign_in_with_password starts a "
        "background refresh timer for whoever signed in last"
    )
    assert _STATELESS.persist_session is False


def test_both_clients_pass_the_stateless_options():
    """Guards against a client being built without the options by mistake."""
    source = (Path(__file__).resolve().parent.parent / "api" / "core" / "supabase.py").read_text(
        encoding="utf-8"
    )
    assert source.count("options=_STATELESS") == 2


def test_missing_configuration_names_the_key():
    from api.core import supabase as module

    with pytest.raises(UpstreamError) as caught:
        module._require("SUPABASE_URL", "")
    assert "SUPABASE_URL" in str(caught.value)
    assert "backend/.env" in str(caught.value)


def test_sign_in_uses_the_anon_client_not_the_service_role():
    """A service-role sign-in would mint a token that bypasses every policy."""
    from api.services.auth_service import SupabaseAuthGateway

    used: list[str] = []

    class Recorder:
        def __init__(self, label: str) -> None:
            self.label = label
            self.auth = self

        def sign_in_with_password(self, credentials):
            used.append(self.label)
            raise RuntimeError("stop here; we only care which client was used")

    gateway = SupabaseAuthGateway(
        client_factory=lambda: Recorder("service"),
        public_client_factory=lambda: Recorder("anon"),
    )
    with pytest.raises(Exception):
        gateway.sign_in("someone@example.ph", "password")

    assert used == ["anon"]


# --------------------------------------------------------------------------- #
# Storage                                                                      #
# --------------------------------------------------------------------------- #
class _Bucket:
    def __init__(self, result) -> None:
        self._result = result

    def create_signed_url(self, path, expires):
        return self._result


class _Storage:
    def __init__(self, result) -> None:
        self._result = result

    def from_(self, bucket):
        return _Bucket(self._result)


class _Client:
    def __init__(self, result) -> None:
        self.storage = _Storage(result)


def store_returning(result) -> SupabaseObjectStore:
    return SupabaseObjectStore(client_factory=lambda: _Client(result))


def test_signed_url_returns_the_url():
    url = store_returning({"signedURL": "https://x/y?token=1", "signedUrl": "https://x/y?token=1"})
    assert url.signed_url("answer-sheets", "a/b.jpg", 60) == "https://x/y?token=1"


def test_signed_url_accepts_the_camel_case_spelling():
    """Storage returns both spellings; neither may be the only one we read."""
    url = store_returning({"signedUrl": "https://x/y?token=2"})
    assert url.signed_url("answer-sheets", "a/b.jpg", 60) == "https://x/y?token=2"


def test_missing_object_raises_instead_of_returning_empty():
    """Storage reports a missing object with a null URL rather than an error."""
    with pytest.raises(UpstreamError):
        store_returning({"signedURL": None, "signedUrl": None}).signed_url(
            "answer-sheets", "gone.jpg", 60
        )


# --------------------------------------------------------------------------- #
# Migrations                                                                   #
# --------------------------------------------------------------------------- #
def read(name: str) -> str:
    return (MIGRATIONS / name).read_text(encoding="utf-8")


def test_migrations_are_present_and_ordered():
    names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert names == [
        "0001_schema.sql",
        "0002_rls.sql",
        "0003_storage.sql",
        "0004_seed.sql",
    ]


def test_schema_creates_every_model_table():
    """The generated file must not fall behind api/models/tables.py."""
    sql = read("0001_schema.sql")
    missing = [
        name for name in Base.metadata.tables if f"CREATE TABLE IF NOT EXISTS {name} " not in sql
    ]
    assert not missing, f"regenerate with scripts/generate_schema_sql.py; missing: {missing}"


def test_profiles_are_tied_to_auth_users():
    assert "references auth.users (id) on delete cascade" in read("0001_schema.sql")


def test_rls_is_enabled_on_every_table():
    sql = read("0002_rls.sql")
    missing = [
        name
        for name in Base.metadata.tables
        if not re.search(rf"alter table public\.{name}\s+enable row level security", sql)
    ]
    assert not missing, f"tables without RLS: {missing}"


def test_policy_helpers_are_security_definer():
    """A helper that is not SECURITY DEFINER recurses through profiles' own policy."""
    sql = read("0002_rls.sql")
    bodies = re.findall(
        r"create or replace function public\.(\w+)\(.*?\n\$\$;", sql, re.S
    )
    assert bodies, "no helper functions found"
    for name in ("can_read_class", "can_read_student", "current_role_name"):
        block = re.search(
            rf"create or replace function public\.{name}\(.*?\n\$\$;", sql, re.S
        )
        assert block, f"{name} is missing"
        assert "security definer" in block.group(0), f"{name} must be SECURITY DEFINER"
        assert "set search_path" in block.group(0), f"{name} must pin search_path"


def test_policies_do_not_query_other_tables_directly():
    """Joins belong in the definer helpers; a subquery here re-enters RLS."""
    sql = read("0002_rls.sql")
    without_functions = re.sub(
        r"create or replace function public\.\w+\(.*?\n\$\$;", "", sql, flags=re.S
    )
    policies = re.findall(r"create policy .*?;", without_functions, re.S)
    assert len(policies) >= 20
    offenders = [p.split("\n")[0] for p in policies if " from public." in p]
    assert not offenders, f"policies with a direct subquery: {offenders}"


def test_no_write_policy_is_granted_beyond_a_users_own_profile():
    """Every other write goes through the API, which is what makes the audit
    log complete and the rate limits meaningful."""
    sql = read("0002_rls.sql")
    writes = re.findall(r"create policy (\w+) on public\.(\w+)\s+for (insert|update|delete)", sql)
    assert writes == [("profiles_update_self", "profiles", "update")], writes


def test_buckets_are_private():
    sql = read("0003_storage.sql")
    assert "'answer-sheets', 'answer-sheets', false" in sql
    assert "'certificates', 'certificates', false" in sql
    assert "'report-templates', 'report-templates', false" in sql
    assert re.search(r"set\s+public\s*=\s*excluded\.public", sql)


def test_bucket_names_match_the_settings():
    from api.core.config import settings

    sql = read("0003_storage.sql")
    for bucket in (
        settings.ANSWER_SHEET_BUCKET,
        settings.CERTIFICATE_BUCKET,
        settings.REPORT_TEMPLATE_BUCKET,
    ):
        assert f"'{bucket}'" in sql, f"0003_storage.sql does not create '{bucket}'"


def test_seed_matches_the_workbook_sheet_names():
    """`sheet_name` must equal the worksheet tab or the exporter cannot find it."""
    sql = read("0004_seed.sql")
    for sheet in (
        "Gen Science",
        "Gen Math",
        "CL",
        "SSTP",
        "PKLP",
        "LCS",
        "E.Comm",
        "M. Kommunikasyon",
    ):
        assert f"'{sheet}'" in sql, f"missing learning area sheet {sheet!r}"


def test_seed_is_rerunnable():
    sql = read("0004_seed.sql")
    assert "on conflict" in sql
    assert sql.count("on conflict") >= 3


def test_doctor_checks_every_policy_helper():
    """A helper the doctor does not know about can go missing unnoticed, and a
    policy whose helper is gone returns no rows rather than an error."""
    from scripts.doctor import EXPECTED_FUNCTIONS

    declared = set(re.findall(r"create or replace function public\.(\w+)\(", read("0002_rls.sql")))
    assert declared == set(EXPECTED_FUNCTIONS), (
        f"doctor is out of step; only in migration: {sorted(declared - set(EXPECTED_FUNCTIONS))}, "
        f"only in doctor: {sorted(set(EXPECTED_FUNCTIONS) - declared)}"
    )
