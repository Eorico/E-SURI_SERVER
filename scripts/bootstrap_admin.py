"""Create the first administrator.

    python scripts/bootstrap_admin.py --email you@deped.gov.ph --name "Your Name"

Roles are handed out by an administrator, which leaves the first one with
nobody to grant it — so it is made here, on the server, by whoever holds the
service-role key. Everyone else registers through the app and is promoted by
this account.

The password is read from a prompt, never from an argument: anything typed on
a command line lands in the shell's history file. It is sent to Supabase and
not stored, logged or echoed here.

Safe to re-run: if the account already exists it is promoted rather than
duplicated.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from api.core.config import settings
from api.core.database import SessionLocal
from api.models.enums import UserRole
from api.repositories.profile_repository import ProfileRepository
from api.repositories.school_repository import LearningAreaRepository, SchoolRepository
from api.services.auth_service import AuthService, SupabaseAuthGateway

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
    parser.add_argument("--name", required=True, help="Full name, as it should appear on reports")
    parser.add_argument("--username", help="Defaults to the part of the email before @")
    args = parser.parse_args()

    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "DATABASE_URL"):
        if not getattr(settings, key):
            sys.exit(f"{key} is not set. Configure backend/.env first, then run scripts/doctor.py.")

    email = args.email.strip().lower()
    username = (args.username or email.split("@")[0]).strip().lower()

    session = SessionLocal()
    try:
        profiles = ProfileRepository(session)
        service = AuthService(
            SupabaseAuthGateway(),
            profiles,
            SchoolRepository(session),
            LearningAreaRepository(session),
        )

        existing = profiles.by_email(email)
        if existing is not None:
            if existing.role is UserRole.administrator:
                print(f"{email} is already an administrator.")
                return 0
            service.assign_role(profile_id=existing.id, role=UserRole.administrator)
            session.commit()
            print(f"Promoted {email} to administrator.")
            return 0

        password = read_password()
        profile = service.register(
            full_name=args.name,
            username=username,
            email=email,
            password=password,
            role=UserRole.administrator,
            school_id=None,
            learning_area_id=None,
            # The whole point of this script: the one place a privileged role
            # may be created without an administrator already signed in.
            allow_privileged=True,
        )
        session.commit()
        print(f"Created administrator {profile.email} ({profile.username}).")
        print("Sign in through the app; everyone else registers and is promoted by this account.")
        return 0
    except Exception as exc:
        session.rollback()
        sys.exit(f"{type(exc).__name__}: {exc}")
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
