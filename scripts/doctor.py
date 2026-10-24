"""Check this server against the Supabase project it is pointed at.

    python scripts/doctor.py

Everything the API needs at runtime is verified here: configuration, the
Postgres schema, row-level security, the storage buckets and Supabase Auth.
Each check prints OK, WARN or FAIL with what to do about it, and the exit code
is non-zero if anything failed - so it can gate a deploy.

No secret is ever printed. Keys are reported as present or missing and nothing
more, so the output is safe to paste into a chat or an issue.
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass, field

from sqlalchemy import create_engine, text

from api.core.config import settings
from api.models.tables import Base

EXPECTED_BUCKETS = (
    settings.ANSWER_SHEET_BUCKET,
    settings.CERTIFICATE_BUCKET,
    settings.REPORT_TEMPLATE_BUCKET,
)

#: Every helper 0002_rls.sql installs. A policy silently returns *no rows* if
#: its helper is missing, so an absent function looks exactly like "you have no
#: classes" - worth checking explicitly.
EXPECTED_FUNCTIONS = (
    "current_role_name",
    "current_school_id",
    "current_learning_area_id",
    "is_administrator",
    "can_read_class",
    "can_read_student",
    "can_read_assessment",
    "can_read_submission",
    "can_read_answer_key",
    "can_read_learning_area",
    "can_read_report",
    "is_school_learning_area",
)

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


@dataclass
class Report:
    lines: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.lines.append((status, name, detail))
        print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))

    @property
    def failed(self) -> int:
        return sum(1 for status, _, _ in self.lines if status == FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for status, _, _ in self.lines if status == WARN)


def check_config(report: Report) -> bool:
    """Report only whether each key is set. Never print a value."""
    print("\n-- Configuration -------------------------------------------")
    required = {
        "SUPABASE_URL": settings.SUPABASE_URL,
        "SUPABASE_ANON_KEY": settings.SUPABASE_ANON_KEY,
        "SUPABASE_SERVICE_KEY": settings.SUPABASE_SERVICE_KEY,
        "SUPABASE_JWT_SECRET": settings.SUPABASE_JWT_SECRET,
        "DATABASE_URL": settings.DATABASE_URL,
    }
    complete = True
    for name, value in required.items():
        if value:
            report.add(OK, name, "set")
        else:
            complete = False
            report.add(FAIL, name, "missing - add it to backend/.env")

    if settings.SUPABASE_URL and not settings.SUPABASE_URL.startswith("https://"):
        report.add(WARN, "SUPABASE_URL", "does not start with https://")

    if settings.RATE_LIMIT_WINDOW_SECONDS != 300:
        report.add(
            WARN,
            "RATE_LIMIT_WINDOW_SECONDS",
            f"is {settings.RATE_LIMIT_WINDOW_SECONDS}; the system is specified as 300",
        )
    else:
        report.add(OK, "rate limit window", "300s on every tier")

    return complete


def check_database(report: Report) -> None:
    print("\n-- Database ------------------------------------------------")
    if not settings.DATABASE_URL:
        report.add(FAIL, "database", "no DATABASE_URL; skipping schema checks")
        return

    try:
        engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("select 1"))
            report.add(OK, "connection", "reachable")

            # --- tables -------------------------------------------------
            rows = conn.execute(
                text("select tablename from pg_tables where schemaname = 'public'")
            ).scalars().all()
            present = set(rows)
            expected = set(Base.metadata.tables)
            missing = sorted(expected - present)
            if missing:
                report.add(
                    FAIL,
                    "schema",
                    f"{len(missing)} table(s) missing: {', '.join(missing)} "
                    f"- run supabase/migrations/0001_schema.sql",
                )
            else:
                report.add(OK, "schema", f"all {len(expected)} tables present")

            # --- row-level security -------------------------------------
            unprotected = conn.execute(
                text(
                    "select c.relname from pg_class c "
                    "join pg_namespace n on n.oid = c.relnamespace "
                    "where n.nspname = 'public' and c.relkind = 'r' "
                    "and c.relrowsecurity = false"
                )
            ).scalars().all()
            unprotected = sorted(set(unprotected) & expected)
            if unprotected:
                report.add(
                    FAIL,
                    "row-level security",
                    f"off on: {', '.join(unprotected)} - run 0002_rls.sql",
                )
            else:
                report.add(OK, "row-level security", "enabled on every table")

            # --- helper functions ---------------------------------------
            functions = set(
                conn.execute(
                    text(
                        "select p.proname from pg_proc p "
                        "join pg_namespace n on n.oid = p.pronamespace "
                        "where n.nspname = 'public'"
                    )
                ).scalars().all()
            )
            missing_fn = [f for f in EXPECTED_FUNCTIONS if f not in functions]
            if missing_fn:
                report.add(
                    FAIL,
                    "policy helpers",
                    f"missing: {', '.join(missing_fn)} - policies will return no rows",
                )
            else:
                report.add(OK, "policy helpers", f"all {len(EXPECTED_FUNCTIONS)} present")

            # --- policies -----------------------------------------------
            policy_count = conn.execute(
                text("select count(*) from pg_policies where schemaname = 'public'")
            ).scalar_one()
            if policy_count == 0:
                report.add(FAIL, "policies", "none defined - run 0002_rls.sql")
            else:
                report.add(OK, "policies", f"{policy_count} defined")

            # --- the auth.users link ------------------------------------
            linked = conn.execute(
                text(
                    "select count(*) from pg_constraint "
                    "where conname = 'fk_profiles_id_auth_users'"
                )
            ).scalar_one()
            if linked:
                report.add(OK, "profiles -> auth.users", "foreign key in place")
            else:
                report.add(
                    WARN,
                    "profiles -> auth.users",
                    "not linked; deleting a user will leave an orphan profile",
                )

            # --- reference data -----------------------------------------
            if "learning_areas" in present:
                areas = conn.execute(text("select count(*) from public.learning_areas")).scalar_one()
                if areas == 0:
                    report.add(WARN, "reference data", "no learning areas - run 0004_seed.sql")
                else:
                    report.add(OK, "reference data", f"{areas} learning areas")

    except Exception as exc:
        report.add(FAIL, "database", f"{type(exc).__name__}: {exc}")


def check_supabase(report: Report) -> None:
    print("\n-- Supabase ------------------------------------------------")
    if not (settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY):
        report.add(FAIL, "supabase", "not configured; skipping")
        return

    try:
        from api.core.supabase import get_supabase, get_supabase_public

        client = get_supabase()

        # --- storage -------------------------------------------------
        try:
            buckets = {b.id for b in client.storage.list_buckets()}
            for name in EXPECTED_BUCKETS:
                if name in buckets:
                    report.add(OK, f"bucket '{name}'", "exists")
                else:
                    report.add(
                        FAIL, f"bucket '{name}'", "missing - run 0003_storage.sql"
                    )
        except Exception as exc:
            report.add(FAIL, "storage", f"{type(exc).__name__}: {exc}")

        # --- auth ----------------------------------------------------
        try:
            client.auth.admin.list_users()
            report.add(OK, "auth admin", "service key accepted")
        except Exception as exc:
            report.add(
                FAIL,
                "auth admin",
                f"service key rejected ({type(exc).__name__}) - check SUPABASE_SERVICE_KEY",
            )

        # The anon client is what signs users in. A bad password must come
        # back as an auth error, not a transport or key error.
        try:
            get_supabase_public().auth.sign_in_with_password(
                {"email": "doctor-probe@invalid.example", "password": "x" * 24}
            )
            report.add(WARN, "auth sign-in", "probe account unexpectedly signed in")
        except Exception as exc:
            name = type(exc).__name__
            if "Auth" in name or "api" in name.lower():
                report.add(OK, "auth sign-in", "anon key accepted, bad credentials refused")
            else:
                report.add(FAIL, "auth sign-in", f"unexpected {name}: {exc}")

    except Exception as exc:
        report.add(FAIL, "supabase", f"{type(exc).__name__}: {exc}")


def main() -> int:
    print("E-SURI backend doctor")
    print(f"environment: {settings.ENVIRONMENT}")

    report = Report()
    configured = check_config(report)
    if configured:
        check_database(report)
        check_supabase(report)
    else:
        print("\nConfiguration incomplete - skipping the live checks.")

    print("\n-- Summary -------------------------------------------------")
    total = len(report.lines)
    print(f"{total - report.failed - report.warned} ok, {report.warned} warning(s), {report.failed} failure(s)")
    if report.failed:
        print("\nThe backend will not work correctly until the failures above are fixed.")
        return 1
    print("\nBackend and Supabase agree.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception:  # pragma: no cover - the doctor must never be the problem
        traceback.print_exc()
        raise SystemExit(1)
