"""Check this deployment against the real cluster.

    python scripts/doctor.py

The test suite runs on `mongomock`, which implements the query language but not
the server. So there are things no test here can establish, and they are exactly
the things that break a deployment:

* whether the cluster is reachable at all, and from this machine's IP;
* whether the **unique** indexes exist - mongomock does not enforce uniqueness,
  so a missing one passes every test and then lets two accounts share an email;
* whether the TTL indexes exist, without which sessions and codes accumulate
  forever;
* whether the signing key, SMTP and storage are configured.

This script is where those are verified. It writes nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymongo.errors import PyMongoError

from api.core.config import settings
from api.core.indexes import SCHEMA, index_report
from api.core.mongo import get_database, ping
from api.models.documents import COLLECTIONS

OK, WARN, FAIL = "ok  ", "warn", "FAIL"
_results: list[tuple[str, str, str]] = []


def record(status: str, check: str, detail: str = "") -> None:
    _results.append((status, check, detail))
    print(f"  [{status}] {check}" + (f" - {detail}" if detail else ""))


def check_configuration() -> None:
    print("\nconfiguration")
    uri = settings.MONGODB_URI
    if not uri:
        record(FAIL, "MONGODB_URI", "not set in backend/.env")
    else:
        # Never print the URI itself: an Atlas SRV string carries the password.
        host = uri.split("@")[-1].split("/")[0] if "@" in uri else uri
        record(OK, "MONGODB_URI", host)

    record(OK if settings.MONGODB_DB else FAIL, "MONGODB_DB", settings.MONGODB_DB)

    if len(settings.JWT_SECRET) >= 32:
        record(OK, "JWT_SECRET", f"{len(settings.JWT_SECRET)} characters")
    elif settings.JWT_SECRET:
        record(FAIL, "JWT_SECRET", "shorter than 32 characters - too weak to sign with")
    else:
        record(
            FAIL,
            "JWT_SECRET",
            'not set; generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"',
        )

    if settings.EMAIL_ENABLED:
        missing = [
            name
            for name in ("SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL")
            if not getattr(settings, name)
        ]
        if missing:
            record(FAIL, "SMTP", f"EMAIL_ENABLED but missing {', '.join(missing)}")
        else:
            record(OK, "SMTP", f"{settings.SMTP_HOST}:{settings.SMTP_PORT}")
    else:
        record(
            WARN,
            "SMTP",
            "EMAIL_ENABLED is false - registration codes are logged, not mailed",
        )

    if settings.is_production and "localhost" in settings.PUBLIC_API_URL:
        record(FAIL, "PUBLIC_API_URL", "still localhost; file links will not resolve")
    else:
        record(OK, "PUBLIC_API_URL", settings.PUBLIC_API_URL)


def check_connection() -> bool:
    print("\nconnection")
    try:
        latency = ping()
    except Exception as exc:
        record(FAIL, "reachable", str(exc)[:120])
        print(
            "\n  Most often one of:\n"
            "    - this machine's IP is not on the cluster's access list (Atlas: Network Access)\n"
            "    - the password in MONGODB_URI needs percent-encoding\n"
            "    - the cluster is paused"
        )
        return False

    record(OK, "reachable", f"{latency} ms")
    if latency > 500:
        record(WARN, "latency", f"{latency} ms is high - is the cluster in another region?")
    return True


def check_indexes() -> None:
    print("\nindexes")
    db = get_database()
    rows = index_report(db)
    missing = [(collection, name) for collection, name, present in rows if not present]

    if not missing:
        record(OK, "all present", f"{len(rows)} indexes")
    else:
        for collection, name in missing:
            record(FAIL, f"{collection}.{name}", "missing")
        print("\n  Fix with:  python scripts/sync_indexes.py")

    # Uniqueness is the half mongomock cannot check, so it is checked here
    # against what the server actually reports.
    declared_unique = {
        (spec.collection, index.document["name"])
        for spec in SCHEMA
        for index in spec.indexes
        if index.document.get("unique")
    }
    not_unique = []
    for collection, name in sorted(declared_unique):
        try:
            info = db[collection].index_information().get(name)
        except PyMongoError:
            continue
        if info is not None and not info.get("unique"):
            not_unique.append(f"{collection}.{name}")

    if not_unique:
        record(FAIL, "uniqueness", f"exists but not unique: {', '.join(not_unique)}")
        print("\n  Drop each and re-run sync_indexes.py, or two records can collide.")
    else:
        record(OK, "uniqueness", f"{len(declared_unique)} unique indexes enforced")

    ttl = [
        (spec.collection, index.document["name"])
        for spec in SCHEMA
        for index in spec.indexes
        if "expireAfterSeconds" in index.document
    ]
    live = [
        f"{collection}.{name}"
        for collection, name in ttl
        if (db[collection].index_information().get(name) or {}).get("expireAfterSeconds")
        is not None
    ]
    if len(live) == len(ttl):
        record(OK, "expiry", f"{len(ttl)} TTL indexes active")
    else:
        record(
            FAIL,
            "expiry",
            f"only {len(live)} of {len(ttl)} TTL indexes - lapsed sessions will pile up",
        )


def check_for_a_renamed_collection() -> None:
    """Warn about a `profiles` collection left behind by the rename.

    The accounts collection is `users` now. A database set up before that still
    holds the old one, and nothing reads it - so every account would appear to
    have vanished with no error anywhere to explain it. Worth one check, to
    turn a confusing hour into one command.
    """
    db = get_database()
    if "profiles" not in db.list_collection_names():
        return

    left_behind = db["profiles"].count_documents({})
    if left_behind == 0:
        record(WARN, "profiles", "empty leftover collection; safe to drop")
        return

    record(
        FAIL,
        "profiles",
        f"{left_behind} documents in the old collection, which nothing reads",
    )
    print(
        "\n  The accounts collection is named `users` now. Move it with mongosh:\n"
        '    db.profiles.renameCollection("users")\n'
        "  then re-run:  python scripts/sync_indexes.py"
    )


def check_data() -> None:
    print("\ndata")
    db = get_database()
    counts = {name: db[name].count_documents({}) for name in sorted(COLLECTIONS.values())}

    if counts.get("schools", 0) == 0:
        record(FAIL, "schools", "none - run: python scripts/seed.py")
    else:
        record(OK, "schools", str(counts["schools"]))

    for name, label in (
        ("departments", 5),
        ("learning_areas", 8),
        ("sections", 18),
    ):
        found = counts.get(name, 0)
        if found == 0:
            record(FAIL, name, "none - run: python scripts/seed.py")
        elif found < label:
            record(WARN, name, f"{found}, seed creates {label}")
        else:
            record(OK, name, str(found))

    administrators = db["users"].count_documents({"role": "administrator"})
    if administrators == 0:
        record(
            FAIL,
            "administrators",
            "none - run: python scripts/bootstrap_admin.py, or nobody can open a term",
        )
    else:
        record(OK, "administrators", str(administrators))

    unconfirmed = db["users"].count_documents({"email_confirmed_at": None})
    if unconfirmed:
        record(
            WARN,
            "unconfirmed accounts",
            f"{unconfirmed} cannot sign in until they enter their code",
        )

    # An account with no password hash cannot sign in and never will; it is the
    # shape a half-finished migration leaves behind.
    no_password = db["users"].count_documents(
        {"$or": [{"password_hash": None}, {"password_hash": {"$exists": False}}]}
    )
    if no_password:
        record(FAIL, "accounts with no password", f"{no_password} can never sign in")

    busiest = {
        name: count for name, count in counts.items() if count
    }
    print("\n  documents: " + ", ".join(f"{name}={count}" for name, count in busiest.items()))


def check_storage() -> None:
    print("\nstorage (GridFS)")
    db = get_database()
    total = 0
    for bucket in (
        settings.ANSWER_SHEET_BUCKET,
        settings.CERTIFICATE_BUCKET,
        settings.REPORT_TEMPLATE_BUCKET,
    ):
        files = db[f"{bucket}.files"]
        count = files.count_documents({})
        used = next(
            files.aggregate(
                [{"$group": {"_id": None, "bytes": {"$sum": "$length"}}}]
            ),
            {},
        ).get("bytes", 0)
        total += used
        record(OK, bucket, f"{count} files, {used / 1_048_576:.1f} MB")

    record(
        OK if settings.SHEET_RECOMPRESS else WARN,
        "sheet recompression",
        f"on, {settings.SHEET_MAX_EDGE_PX}px"
        if settings.SHEET_RECOMPRESS
        else "OFF - sheets are stored at full size and will fill a small cluster",
    )

    # GridFS shares the cluster's quota with the records, which a bucket did
    # not, so the number is worth stating rather than leaving to be discovered.
    free_tier = 512 * 1_048_576
    if total > free_tier * 0.7:
        record(
            WARN,
            "quota",
            f"{total / 1_048_576:.0f} MB stored; a free M0 cluster holds 512 MB in total",
        )


def main() -> int:
    print("E-SURI doctor")
    print("=" * 60)

    check_configuration()
    if check_connection():
        check_for_a_renamed_collection()
        check_indexes()
        check_data()
        try:
            check_storage()
        except PyMongoError as exc:
            record(WARN, "storage", str(exc)[:100])

    failures = [row for row in _results if row[0] == FAIL]
    warnings = [row for row in _results if row[0] == WARN]

    print("\n" + "=" * 60)
    print(f"{len(_results)} checks: {len(failures)} failed, {len(warnings)} warnings")
    if failures:
        print("\nfailed:")
        for _, check, detail in failures:
            print(f"  - {check}" + (f": {detail}" if detail else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
