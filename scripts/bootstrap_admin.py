"""Create the first administrator.

    python scripts/bootstrap_admin.py --email you@deped.gov.ph \
        --last-name Dela-Cruz --first-name Maria

Roles are handed out by an administrator, which leaves the first one with
nobody to grant it - so it is made here, on the server, by whoever is setting
the system up. Everyone else registers through the app and is promoted by this
account.

The password is read from a prompt, never from an argument: anything typed on a
command line lands in the shell's history file. It is hashed with Argon2id and
neither stored in the clear, logged, nor echoed.

`--type` picks which kind of administrator. The default is the assessment
coordinator, because that is the one who can open a term - and until a term is
open, nothing else in the system can be used.

Safe to re-run: an account that already exists is promoted rather than
duplicated.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.core.config import settings
from api.core.mongo import get_database, ping
from api.models.documents import AdministratorProfile
from api.models.enums import AdminType, UserRole
from api.repositories.auth_repository import EmailTokenRepository, SessionRepository
from api.repositories.profile_repository import (
    CoordinatorAssignmentRepository,
    ProfileRepository,
)
from api.repositories.school_repository import LearningAreaRepository, SchoolRepository
from api.services.auth_service import AuthService, MongoAuthGateway

MIN_PASSWORD = 12


def read_password() -> str:
    first = getpass.getpass("Password (not echoed): ")
    if len(first) < MIN_PASSWORD:
        sys.exit(f"Password must be at least {MIN_PASSWORD} characters.")
    if first != getpass.getpass("Confirm: "):
        sys.exit("Passwords did not match.")
    return first


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or promote the first administrator.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--last-name", required=True, help="Surname")
    parser.add_argument("--first-name", required=True, help="Given name")
    parser.add_argument("--middle-initial", help="Middle initial, if any")
    parser.add_argument("--suffix", help="Name extension: Jr., Sr., III")
    parser.add_argument("--username", help="Defaults to the part of the email before @")
    parser.add_argument(
        "--type",
        choices=[kind.value for kind in AdminType],
        default=AdminType.assessment_coordinator.value,
        help="Which kind of administrator (default: assessment_coordinator)",
    )
    parser.add_argument(
        "--department",
        help="Department code, required for --type master_teacher (e.g. SCIENCE)",
    )
    args = parser.parse_args()

    if not settings.MONGODB_URI:
        sys.exit("MONGODB_URI is not set. Configure backend/.env, then run scripts/doctor.py.")
    if len(settings.JWT_SECRET) < 32:
        sys.exit(
            "JWT_SECRET is not set (or is too short). Without it no token can be signed.\n"
            'Generate one:  python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )

    try:
        ping()
    except Exception as exc:
        sys.exit(f"Cannot reach the cluster: {exc}\nRun scripts/doctor.py for the usual causes.")

    admin_type = AdminType(args.type)
    email = args.email.strip().lower()
    username = (args.username or email.split("@")[0]).strip().lower()

    db = get_database()
    profiles = ProfileRepository(db)
    schools = SchoolRepository(db)

    department_id = None
    if admin_type is AdminType.master_teacher:
        if not args.department:
            sys.exit("A master teacher oversees one department. Pass --department CODE.")
        school = schools.current()
        found = db["departments"].find_one(
            {"code": args.department.strip().upper(), "school_id": str(school.id) if school else None}
        )
        if found is None:
            known = sorted(d["code"] for d in db["departments"].find({}, {"code": 1}))
            sys.exit(
                f"No department with code {args.department!r}. "
                f"Known: {', '.join(known) or 'none - run scripts/seed.py'}"
            )
        department_id = found["_id"]
    elif args.department:
        sys.exit(
            f"--department applies only to a master teacher; "
            f"a {admin_type.value} sees the whole school."
        )

    service = AuthService(
        gateway=MongoAuthGateway(
            profiles=profiles,
            sessions=SessionRepository(db),
            email_tokens=EmailTokenRepository(db),
        ),
        profiles=profiles,
        schools=schools,
        learning_areas=LearningAreaRepository(db),
        assignments=CoordinatorAssignmentRepository(db),
    )

    existing = profiles.by_email(email)
    if existing is not None:
        if existing.role is UserRole.administrator and existing.admin_type is admin_type:
            print(f"{email} is already a {admin_type.value}.")
            return 0

        service.assign_role(user_id=existing.id, role=UserRole.administrator)
        refreshed = profiles.get(existing.id)
        profiles.update(
            refreshed,
            administrator_profile=AdministratorProfile(
                admin_type=admin_type, department_id=department_id
            ),
        )
        print(f"Promoted {email} to {admin_type.value}.")
        return 0

    if profiles.by_username(username) is not None:
        sys.exit(f"The username {username!r} is taken. Pass a different --username.")

    password = read_password()
    # The type goes in with the registration rather than being set afterwards:
    # an administrator with no type is refused, and a two-step create would
    # leave one in that state if the second step failed.
    profile = service.register(
        last_name=args.last_name,
        first_name=args.first_name,
        middle_initial=args.middle_initial,
        suffix=args.suffix,
        username=username,
        email=email,
        password=password,
        role=UserRole.administrator,
        school_id=None,
        learning_area_id=None,
        admin_type=admin_type,
        department_id=uuid.UUID(department_id) if department_id else None,
        # The one account that skips confirmation: there is nobody to grant it
        # access, and SMTP may not be configured yet, so a code it could never
        # receive would lock the system before it opened.
        require_confirmation=False,
    )

    print(f"Created {admin_type.value} {profile.formal_name} ({profile.username}).")
    print("Sign in through the app; everyone else registers and is promoted by this account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
