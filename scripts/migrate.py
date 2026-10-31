"""Apply the SQL migrations in supabase/migrations to the configured database.

    python scripts/migrate.py --status      # what has run, what is pending
    python scripts/migrate.py --dry-run     # what would run, changing nothing
    python scripts/migrate.py               # apply everything pending

Each file runs once, inside one transaction, in filename order, and is then
recorded in `public.schema_migrations`. Re-running is safe: applied files are
skipped, and a file that fails rolls back whole, so the database is never left
half-migrated.

Why this exists rather than `psql` or the Supabase CLI: neither is installed
here, while SQLAlchemy and psycopg2 already are. The CLI remains a fine
alternative — see README.md.

Connection note: use the **direct** connection string (port 5432) or the
session pooler, not the transaction pooler (6543). The transaction pooler
cannot hold the session state that `create function` and `alter table` need
across a transaction.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass

# Run as `python scripts/<name>.py` from the backend folder: Python puts this
# file's own directory on sys.path, not the project root, so `api` would not be
# importable without this.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from api.core.config import settings

MIGRATIONS = Path(__file__).resolve().parent.parent / "supabase" / "migrations"

LEDGER = """
create table if not exists public.schema_migrations (
    version     text primary key,
    checksum    text not null,
    applied_at  timestamptz not null default now()
)
"""


@dataclass(frozen=True)
class Migration:
    path: Path

    @property
    def version(self) -> str:
        return self.path.name

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()[:16]


def discover() -> list[Migration]:
    if not MIGRATIONS.is_dir():
        sys.exit(f"No migrations directory at {MIGRATIONS}")
    found = [Migration(p) for p in sorted(MIGRATIONS.glob("*.sql"))]
    if not found:
        sys.exit(f"No .sql files in {MIGRATIONS}")
    return found


def connect() -> Engine:
    if not settings.DATABASE_URL:
        sys.exit("DATABASE_URL is not set. Add it to backend/.env (Supabase: Settings > Database).")
    if ":6543" in settings.DATABASE_URL:
        print(
            "WARNING: that looks like the transaction pooler (port 6543).\n"
            "         Migrations need the direct connection (5432) or the session pooler.\n"
        )
    try:
        engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, future=True)
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return engine
    except Exception as exc:
        sys.exit(f"Could not connect: {type(exc).__name__}: {exc}")


def applied(engine: Engine) -> dict[str, str]:
    with engine.begin() as conn:
        conn.execute(text(LEDGER))
    with engine.connect() as conn:
        rows = conn.execute(text("select version, checksum from public.schema_migrations")).all()
    return {version: checksum for version, checksum in rows}


def show_status(migrations: list[Migration], done: dict[str, str]) -> int:
    print(f"{'migration':<28} {'status':<10} checksum")
    print("-" * 60)
    changed = 0
    for migration in migrations:
        if migration.version not in done:
            state = "PENDING"
        elif done[migration.version] != migration.checksum:
            state = "CHANGED"
            changed += 1
        else:
            state = "applied"
        print(f"{migration.version:<28} {state:<10} {migration.checksum}")

    if changed:
        print(
            f"\n{changed} applied file(s) have been edited since they ran.\n"
            "A migration that has already run is history: add a new file for the\n"
            "change instead of editing one that is recorded here."
        )
    return changed


def apply(engine: Engine, migration: Migration) -> None:
    """One file, one transaction. A failure rolls the whole file back."""
    with engine.begin() as conn:
        conn.exec_driver_sql(migration.sql)
        conn.execute(
            text(
                "insert into public.schema_migrations (version, checksum) "
                "values (:v, :c) "
                "on conflict (version) do update set checksum = excluded.checksum, "
                "applied_at = now()"
            ),
            {"v": migration.version, "c": migration.checksum},
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the Supabase SQL migrations.")
    parser.add_argument("--status", action="store_true", help="show what has run; change nothing")
    parser.add_argument("--dry-run", action="store_true", help="list what would run")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-apply a file even though it is recorded as applied",
    )
    args = parser.parse_args()

    migrations = discover()
    engine = connect()
    done = applied(engine)

    if args.status:
        show_status(migrations, done)
        return 0

    pending = [m for m in migrations if args.force or m.version not in done]
    if not pending:
        print(f"Up to date - {len(migrations)} migration(s) already applied.")
        return 0

    print(f"Pending: {', '.join(m.version for m in pending)}\n")
    if args.dry_run:
        print("Dry run - nothing was applied.")
        return 0

    for migration in pending:
        print(f"  applying {migration.version} ... ", end="", flush=True)
        try:
            apply(engine, migration)
        except Exception as exc:
            print("FAILED")
            print(f"\n{type(exc).__name__}: {exc}")
            print(
                f"\n{migration.version} was rolled back; nothing from it was applied.\n"
                "Fix the file and run this again - the migrations before it stay applied."
            )
            return 1
        print("ok")

    print(f"\nApplied {len(pending)} migration(s). Now run: python scripts/doctor.py")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
