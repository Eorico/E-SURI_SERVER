# E-SURI API

FastAPI + Supabase backend for **IMECAR / E-SURI**, which scans Grade 11 answer
sheets for Paliparan III Senior High School and produces the EQWART workbook for
CSDO submission.

## Where the keys live

The browser holds **no Supabase credential**. It is configured with one value —
this API's address — and every call to Supabase is made here, with the keys in
`backend/.env`. Sign-in and refresh are proxied through `/auth/login` and
`/auth/refresh`, so the API's rate limiting, role assignment and active-account
check cannot be sidestepped by calling Supabase directly.

Two clients, deliberately (`api/core/supabase.py`):

| Client | Key | Used for |
| --- | --- | --- |
| `get_supabase_public()` | anon | the sign-in and refresh grants, so a user's token carries only their own privileges |
| `get_supabase()` | service role | creating/deleting auth users, Storage; bypasses RLS, so it never signs a user in |

Both are built with `auto_refresh_token=False, persist_session=False`. They are
cached and therefore shared, and the Supabase SDK otherwise stores "the current
session" on the client and starts a background refresh thread per sign-in —
neither of which a server may have.

## Layers

```
routes -> controllers -> services -> repositories -> models
                      \-> utils/omr (scanning, sheets, EQWART, certificates)
```

- Routes do HTTP only; services hold the rules; repositories are the only DB code.
- `SqlRepository[T]` holds shared CRUD once (DRY); concrete repos subclass it (OCP).
- Services depend on Protocols (`AuthGateway`, `ObjectStore`, `Repository`), wired
  in `api/deps.py` (DIP) — which is what lets the tests run with no network.

## Setup

```bash
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env` from your Supabase project (Project Settings -> API, and
Settings -> Database for `DATABASE_URL`).

Then apply the migrations **in order**, via the Supabase SQL editor or `psql`:

| File | What it does |
| --- | --- |
| `supabase/migrations/0001_schema.sql` | 19 tables, constraints, indexes, `profiles -> auth.users` |
| `supabase/migrations/0002_rls.sql` | row-level security: 12 helper functions, 21 policies |
| `supabase/migrations/0003_storage.sql` | the three private buckets |
| `supabase/migrations/0004_seed.sql` | the school, its 8 learning areas, its sections |

`0001_schema.sql` is **generated** from `api/models/tables.py`. After changing a
model, regenerate it rather than editing the SQL:

```bash
python scripts/generate_schema_sql.py
```

Create the first administrator (everyone else registers in the app and is
promoted by this account):

```bash
python scripts/bootstrap_admin.py --email you@deped.gov.ph --name "Your Name"
```

## Check it works

```bash
python scripts/doctor.py
```

It verifies configuration, the schema, that RLS is on with its helpers and
policies, the storage buckets, and that both Supabase keys are accepted. It
prints whether each key is *set*, never its value, so the output is safe to
share. Exit code is non-zero if anything failed.

## Run

```bash
uvicorn api.main:app --reload
```

Swagger at http://localhost:8000/docs; health at `GET /api/v1/health`.
Run from the repository root so `api/`, `dto/` and `utils/` all resolve.

## Tests

```bash
pytest tests -q
```

No Supabase project, network or keys required — the gateways are stubbed.

## Security

- **Rate limiting**: a 300-second sliding window on every tier, chosen by path
  (`AUTH` 10, `EXPORT` 10, `SCAN` 60, `WRITE` 100, `READ` 300). A refusal carries
  `Retry-After: 300`.
- **Row-level security** is the second lock. The API already enforces roles, but
  if a token leaks the database still refuses to hand a teacher another teacher's
  learners. The rule: a teacher sees their own classes, a coordinator their
  learning area, an administrator the school. The answer key is the exception —
  the owning teacher and administrators only.
- **Writes** are granted to `authenticated` nowhere except a user's own profile.
  Everything else goes through the API, which is what makes the audit log
  complete and the rate limits meaningful.
- **Buckets are private.** Files are reached by short-lived signed URL only.
