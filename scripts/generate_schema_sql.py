"""Emit `supabase/migrations/0001_schema.sql` from the SQLAlchemy models.

The models are the single definition of the schema; this renders them as the
Postgres DDL Supabase runs, so the two cannot drift. Re-run it after changing
`api/models/tables.py` and commit the result:

    python scripts/generate_schema_sql.py

Three things are added that the models cannot express, because they reference
Supabase's own `auth` schema or exist only in Postgres:

* `profiles.id` references `auth.users(id)`. A profile row *is* the auth user —
  `AuthService.register` creates both with the same id — so deleting the user
  must take the profile with it rather than leave an orphan that can never sign
  in.
* `gen_random_uuid()` defaults on primary keys. The application always supplies
  an id, but the seed file and anything written by hand in the SQL editor
  should not have to.
* CHECK constraints on the columns whose values are a closed set. They render
  as `VARCHAR` deliberately — a Postgres ENUM would need a migration every time
  DepEd adds a mastery band — so the check is what keeps a typo out.
"""

from __future__ import annotations

import enum

# Run as `python scripts/<name>.py` from the backend folder: Python puts this
# file's own directory on sys.path, not the project root, so `api` would not be
# importable without this.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from api.models import enums as domain_enums
from api.models.tables import Base

OUT = Path(__file__).resolve().parent.parent / "supabase" / "migrations" / "0001_schema.sql"

#: column -> the enum whose values it may hold. Only columns where a wrong
#: value would quietly corrupt a report are constrained.
CHECKED: dict[tuple[str, str], type[enum.Enum]] = {
    ("profiles", "role"): domain_enums.UserRole,
    ("assessments", "type"): domain_enums.AssessmentType,
    ("assessments", "status"): domain_enums.AssessmentStatus,
    ("submissions", "status"): domain_enums.SubmissionStatus,
    ("responses", "flag"): domain_enums.ResponseFlag,
    ("enrollments", "status"): domain_enums.EnrollmentStatus,
    ("certificates", "type"): domain_enums.CertificateType,
    ("certificates", "status"): domain_enums.CertificateStatus,
    ("report_templates", "status"): domain_enums.TemplateStatus,
    ("report_templates", "mapping_source"): domain_enums.MappingSource,
    ("eqwart_reports", "status"): domain_enums.ReportStatus,
    ("eqwart_reports", "assessment_type"): domain_enums.AssessmentType,
    ("eqwart_reports", "intervention_mode"): domain_enums.InterventionMode,
    ("item_analysis", "mastery_level"): domain_enums.MasteryLevel,
    ("item_analysis", "instructional_assistance"): domain_enums.InstructionalAssistance,
}

HEADER = """\
-- ---------------------------------------------------------------------------
-- E-SURI / IMECAR — schema
--
-- GENERATED from api/models/tables.py by scripts/generate_schema_sql.py.
-- Do not edit by hand: change the models and regenerate, or the API and the
-- database will disagree.
--
-- Run first, before 0002_rls.sql.
-- ---------------------------------------------------------------------------

create extension if not exists "pgcrypto";

"""

FOOTER = """\

-- ---------------------------------------------------------------------------
-- Ties a profile to the Supabase auth user it belongs to.
--
-- Added here rather than in the models because `auth.users` is Supabase's own
-- table. Deleting the auth user now removes the profile with it, so no row is
-- left behind that nobody can sign in as.
-- ---------------------------------------------------------------------------
alter table public.profiles
    drop constraint if exists fk_profiles_id_auth_users;

alter table public.profiles
    add constraint fk_profiles_id_auth_users
    foreign key (id) references auth.users (id) on delete cascade;
"""

#: Lookups the screens make constantly that no existing constraint already
#: indexes. Every (table, column) here is checked against the models before the
#: file is written - a hand-written index on a column that does not exist took
#: down the whole migration once, because DDL runs in one transaction.
#:
#: A UNIQUE constraint already creates a btree, and Postgres can use its leading
#: column, so there is no entry here for a column that is the first column of a
#: unique constraint - that index would be pure write cost.
EXTRA_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_classes_teacher", "classes", "teacher_id"),
    ("ix_classes_area", "classes", "learning_area_id"),
    # Enrollment is to a SECTION, not a class. The unique constraint leads with
    # student_id, so section_id lookups ("who is in this section?") need this.
    ("ix_enrollments_section", "enrollments", "section_id"),
    # uq_submissions_assessment_student leads with assessment_id, so a
    # per-learner lookup across assessments is not covered by it.
    ("ix_submissions_student", "submissions", "student_id"),
)


def extra_indexes() -> str:
    """Render the hand-chosen indexes, refusing any that names a real column wrong."""
    lines = ["\n-- Lookups every screen makes, not already covered by a constraint."]
    for name, table, column in EXTRA_INDEXES:
        if table not in Base.metadata.tables:
            raise SystemExit(f"EXTRA_INDEXES names an unknown table: {table}")
        if column not in Base.metadata.tables[table].columns:
            known = ", ".join(Base.metadata.tables[table].columns.keys())
            raise SystemExit(
                f"EXTRA_INDEXES names an unknown column: {table}.{column}\n"
                f"  {table} has: {known}"
            )
        lines.append(f"create index if not exists {name} on public.{table} ({column});")
    return "\n".join(lines) + "\n"


def check_constraints() -> str:
    """Render one CHECK per constrained column, as its own statement."""
    lines = ["\n-- Value sets, enforced in the database as well as in the DTOs."]
    for (table, column), enum_type in CHECKED.items():
        if table not in Base.metadata.tables:
            raise SystemExit(f"CHECKED names an unknown table: {table}")
        if column not in Base.metadata.tables[table].columns:
            raise SystemExit(f"CHECKED names an unknown column: {table}.{column}")

        allowed = ", ".join(f"'{member.value}'" for member in enum_type)
        name = f"ck_{table}_{column}"
        lines.append(
            f"alter table public.{table} drop constraint if exists {name};\n"
            f"alter table public.{table}\n"
            f"    add constraint {name} check ({column} in ({allowed}));"
        )
    return "\n".join(lines) + "\n"


def render() -> str:
    dialect = postgresql.dialect()
    parts = [HEADER]

    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip()
        # Every primary key is a UUID the application supplies; the default is
        # for rows written outside it (the seed file, the SQL editor).
        ddl = ddl.replace("\tid UUID NOT NULL", "\tid UUID NOT NULL DEFAULT gen_random_uuid()")
        parts.append(ddl + ";\n")
        for index in table.indexes:
            parts.append(str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect)).strip() + ";\n")

    parts.append(check_constraints())
    parts.append(FOOTER)
    parts.append(extra_indexes())
    return "\n".join(parts)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(OUT.parent.parent.parent)} "
          f"({len(Base.metadata.tables)} tables, {len(CHECKED)} checks)")


if __name__ == "__main__":
    main()
