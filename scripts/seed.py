"""Create the school, its departments, subjects, sections and opening term.

    python scripts/seed.py

Re-runnable: every write is an upsert keyed the way the unique index is keyed,
so running it twice updates names rather than creating a second copy. That
matters because this is how a renamed subject or a corrected sheet name gets
applied.

It does not create any account. `scripts/bootstrap_admin.py` does that.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymongo.errors import PyMongoError

from api.core.config import settings
from api.core.mongo import get_database, ping
from api.models.enums import TermStatus
from db import seed_data


def upsert(collection, key: dict, fields: dict) -> uuid.UUID:
    """Insert or update one document, keyed as its unique index is keyed.

    `$setOnInsert` on the id is what makes this re-runnable: an existing
    document keeps the id everything else already references, while its other
    fields are brought up to date.
    """
    now = datetime.now(timezone.utc)
    collection.update_one(
        key,
        {
            "$set": {**fields, "updated_at": now},
            "$setOnInsert": {"_id": str(uuid.uuid4()), "created_at": now},
        },
        upsert=True,
    )
    return uuid.UUID(collection.find_one(key, projection={"_id": 1})["_id"])


def main() -> int:
    try:
        print(f"reachable: yes ({ping()} ms)")
    except Exception as exc:
        print(f"reachable: NO - {exc}")
        return 1

    db = get_database()
    year = seed_data.SCHOOL["school_year"]

    try:
        school_id = upsert(
            db["schools"],
            {"name": seed_data.SCHOOL["name"], "school_year": year},
            dict(seed_data.SCHOOL),
        )
        print(f"\nschool   : {seed_data.SCHOOL['name']} ({year})")

        departments: dict[str, uuid.UUID] = {}
        for code, name in seed_data.DEPARTMENTS:
            departments[code] = upsert(
                db["departments"],
                {"school_id": str(school_id), "code": code},
                {"school_id": str(school_id), "code": code, "name": name},
            )
        print(f"depts    : {len(departments)}")

        for code, name, sheet_name, department_code in seed_data.LEARNING_AREAS:
            upsert(
                db["learning_areas"],
                {"school_id": str(school_id), "code": code},
                {
                    "school_id": str(school_id),
                    "code": code,
                    "name": name,
                    "sheet_name": sheet_name,
                    "grade_level": seed_data.GRADE_LEVEL,
                    "department_id": (
                        str(departments[department_code]) if department_code else None
                    ),
                },
            )
        print(f"subjects : {len(seed_data.LEARNING_AREAS)}")

        for position, name in enumerate(seed_data.SECTIONS, start=1):
            upsert(
                db["sections"],
                {"school_id": str(school_id), "name": name, "school_year": year},
                {
                    "school_id": str(school_id),
                    "name": name,
                    "school_year": year,
                    "grade_level": seed_data.GRADE_LEVEL,
                    "position": position,
                },
            )
        print(f"sections : {len(seed_data.SECTIONS)}")

        # The opening cycle, so a class created on day one has a term to sit in.
        # Left alone if it already exists: re-seeding must not reopen a term an
        # assessment coordinator has closed.
        term_key = {
            "school_id": str(school_id),
            "school_year": year,
            "term_no": seed_data.FIRST_TERM_NO,
        }
        existing = db["terms"].find_one(term_key)
        if existing is None:
            upsert(
                db["terms"],
                term_key,
                {
                    **term_key,
                    "status": TermStatus.open.value,
                    "opened_at": datetime.now(timezone.utc),
                },
            )
            print(f"term     : {seed_data.FIRST_TERM_NO} opened")
        else:
            print(f"term     : {seed_data.FIRST_TERM_NO} already {existing['status']}, left alone")

    except PyMongoError as exc:
        print(f"\nFAILED: {exc}")
        print("Run `python scripts/sync_indexes.py` first if the indexes are missing.")
        return 1

    print("\nok - re-run this any time to apply a renamed subject or sheet name")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
