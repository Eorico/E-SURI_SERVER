"""Apply the index definitions in `api/core/indexes.py` to the cluster.

    python scripts/sync_indexes.py

This replaces `scripts/migrate.py`, and it is a different kind of thing. SQL
migrations were append-only history: once a statement had run somewhere, editing
it meant two databases that claimed the same version while holding different
schemas, so the file could never be touched again. Creating an index is
idempotent, so this declares the state the cluster should be in and converges on
it. Run it as often as you like, including on every deploy.

It will refuse rather than guess. An index that already exists under the same
name but with different keys is a disagreement between the code and the cluster,
and MongoDB raises on it - which is the useful outcome, because quietly keeping
the old definition would leave a query plan nobody could explain.
"""

from __future__ import annotations

# Run as `python scripts/<name>.py` from the backend folder: Python puts this
# file's own directory on sys.path, not the project root, so `api` would not be
# importable without this.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymongo.errors import OperationFailure, PyMongoError

from api.core.config import settings
from api.core.indexes import SCHEMA, ensure_indexes
from api.core.mongo import get_database, ping


def main() -> int:
    target = settings.MONGODB_URI
    # Never print a URI: an Atlas SRV string carries the password.
    host = target.split("@")[-1].split("/")[0] if "@" in target else target
    print(f"cluster  : {host}")
    print(f"database : {settings.MONGODB_DB}")

    try:
        print(f"reachable: yes ({ping()} ms)")
    except Exception as exc:
        print(f"reachable: NO - {exc}")
        print(
            "\nCheck MONGODB_URI in backend/.env, and that this machine's IP is on\n"
            "the cluster's access list (Atlas: Network Access)."
        )
        return 1

    db = get_database()
    expected = sum(len(spec.indexes) for spec in SCHEMA)
    print(f"\napplying {expected} indexes across {len(SCHEMA)} collections...")

    try:
        created = ensure_indexes(db)
    except OperationFailure as exc:
        print(f"\nREFUSED: {exc}")
        print(
            "\nAn index of that name already exists with different keys. Drop it and\n"
            "run this again:  db.<collection>.dropIndex('<name>')"
        )
        return 1
    except PyMongoError as exc:
        print(f"\nFAILED: {exc}")
        return 1

    for collection, names in sorted(created.items()):
        print(f"  {collection:<22} {len(names):>2}  {', '.join(sorted(names))}")

    print(f"\nok - {sum(len(n) for n in created.values())} indexes in place")
    print("Next:  python scripts/seed.py        (the school, subjects and sections)")
    print("       python scripts/bootstrap_admin.py   (the first administrator)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
