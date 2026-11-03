# E-SURI API

FastAPI + MongoDB backend for **IMECAR / E-SURI**, which scans Grade 11 answer
sheets for Paliparan III Senior High School and produces the EQWART workbook for
CSDO submission.

## Where the credentials live

The browser holds **no database credential**. It is configured with one value —
this API's address — and every call to MongoDB is made here, with the URI in
`backend/.env`. Sign-in and refresh go through `/auth/login` and `/auth/refresh`,
so the API's rate limiting, role assignment and active-account check cannot be
sidestepped.

This server also **issues and verifies its own tokens**. There is no separate
sign-in provider: passwords are hashed with Argon2id, access tokens are signed
JWTs, and refresh tokens are recorded so that signing out really ends a session.
`JWT_SECRET` is what every token is signed with — an empty or guessable value
lets anyone mint a token for any account, so the server refuses to start in
production without at least 32 characters.

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Layers

```
routes -> controllers -> services -> repositories -> documents
                      \-> utils/omr (scanning, sheets, EQWART, certificates)
```

- Routes do HTTP only; services hold the rules; repositories are the only DB code.
- `MongoRepository[T]` holds shared CRUD once (DRY); concrete repos subclass it
  (OCP). `EmbeddedRepository[T]` does the same for an array inside another
  document.
- Services depend on Protocols (`AuthGateway`, `ObjectStore`, `Repository`), wired
  in `api/deps.py` (DIP) — which is what lets the tests run with no network.

## Setup

```bash
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`: a connection string from Atlas (Database → Connect → Drivers)
and a generated `JWT_SECRET`. Two things catch people out:

- the password in the URI must be **percent-encoded** if it contains
  `@ : / ? # [ ] %`;
- this machine's IP has to be on the cluster's access list (Atlas → Network
  Access), or the connection times out with no useful message.

Then set the cluster up, in this order:

```bash
python scripts/sync_indexes.py     # indexes and uniqueness
python scripts/seed.py             # the school, 5 departments, 8 subjects, 18 sections
python scripts/bootstrap_admin.py --email you@deped.gov.ph \
    --last-name "Dela Cruz" --first-name "Maria" --middle-initial S
```

`sync_indexes.py` declares the state the cluster should be in and converges on
it, so it is safe on every deploy. It is **not** a migration runner: creating an
index is idempotent, so there is no version to record and nothing is append-only.
It refuses rather than guesses if an index already exists under the same name
with different keys.

`seed.py` is re-runnable, and that is how a renamed subject or a corrected
`sheet_name` gets applied — every write is an upsert keyed the way the unique
index is keyed, so the ids everything else references survive.

`bootstrap_admin.py` creates the first administrator, because roles are handed
out by an administrator and the first one has nobody to grant it. `--type` picks
which kind; the default is the assessment coordinator, since nothing else in the
system can be used until a term is open and only that type can open one.

## How the data is shaped

Twenty collections, from twenty-three tables. Five tables became embedded arrays
or sub-documents, because each was only ever read with its parent and each is
bounded:

| Was a table | Now a field on |
| --- | --- |
| `responses` (60 rows per learner) | `submissions.responses` |
| `answer_keys` (up to 60 per assessment) | `assessments.answer_keys` |
| `least_mastered` (5 per report) | `eqwart_reports.least_mastered` |
| `coordinator_assignments` | `users.coordinator_assignment` |
| `administrator_profiles` | `users.administrator_profile` |

That is what removes the N+1s rather than working around them. Loading a
submission with its answers is one read; resolving a caller's role on every
authenticated request is one read, where separate collections would make it
three.

What is queried on its own stays its own collection, referenced by id, and
`ClassRepository.hydrate` fills the **hydration slots** — `Submission.student`,
`Class.section` — with one batched query per relation, however many records are
being hydrated. Resolving them one record at a time would be the N+1 this
exists to avoid.

The accounts collection is called **`users`**, and the document model that maps
to it is called `Profile`. That difference is deliberate: the document carries
the password hash and the confirmation state, so it *is* the account and
`users` is what the database should call it - while `Principal` distinguishes
`user`, the identity a token proves, from `profile`, the staff record read from
the database, and a request needs both. The mapping is declared once, in
`COLLECTIONS`, and there is a test for each half so neither gets tidied into
the other.

Identifiers stay `uuid.UUID` in Python, stored as their 36-character string
rather than BSON binary. The reason is in `_bson_safe` in
`api/models/documents.py`: the suite runs against `mongomock`, which refuses a
custom `uuidRepresentation`, so binary ids would have meant shipping the data
layer with no test behind it. They are also legible in the Atlas console, which
matters for a system that gets administered by hand.

## Email

Confirmation codes and password-reset links are minted, signed, and sent by this
server over SMTP. The wording, the sender and the expiry all live in the
repository.

```
EMAIL_ENABLED=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_STARTTLS=true
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your-16-char-app-password
SMTP_FROM_EMAIL=you@gmail.com
PUBLIC_APP_URL=http://localhost:8080
```

For Gmail, `SMTP_PASSWORD` must be an **App Password**, not the account
password: turn on 2-Step Verification, then Google Account → Security → App
passwords. A normal password is rejected, and the error Gmail returns does not
say why — so the mailer rewrites it.

With `EMAIL_ENABLED=false` nothing is sent and the code is written to the log
instead, so a development machine can never mail a real address — and so you can
still register an account on a machine with no SMTP.

| Flow | What happens |
| --- | --- |
| `/auth/register` | account created **unconfirmed**; a six-digit code is emailed. Signing in before it is used returns "Confirm your email address first", not "credentials not accepted" — the password was right, and saying otherwise sends people round a loop. |
| `/auth/verify-code` | confirms the address and signs them in. The code is single-use and dies after 5 wrong attempts, because six digits is one of a million and the rate limiter is per caller. |
| `/auth/resend-code` | mints a new code and spends the old one, so a resend restarts the window rather than widening it. |
| `/auth/password-reset` | emails a reset link. An unknown address takes the same path and reports the same thing, so the endpoint cannot be used to find out who holds an account. |
| `/auth/password-reset/confirm` | sets the new password and **ends every other session**, because whoever knew the old password may be holding a refresh token. |
| `scripts/bootstrap_admin.py` | the one exception — the first administrator is created already confirmed, since there is nobody to grant them access and SMTP may not be configured yet. |

### An address that will not register again

This used to be a real trap: an account was two rows in two systems, the cascade
ran one way, and deleting the profile left an auth user that refused the address
with no way to clear it from inside the app.

That cannot happen now. A profile **is** the account, so registration is a single
document insert — atomic, with no second system to fall out of step with. To
remove an account, delete its profile; `MongoAuthGateway.delete_user` also clears
its sessions and tokens so nothing is left that could still authenticate it.

## Files

Answer sheets, certificates and templates live in **GridFS**, inside the same
cluster as the records. Two consequences:

- **Storage is shared with the data.** A free M0 cluster holds 512MB in total. So
  sheets are recompressed on upload: at 1600px and grayscale a sheet lands near
  200KB instead of 2–3MB, which is roughly 6,000 sheets per 512MB rather than
  1,200. `SHEET_RECOMPRESS=false` keeps originals. The scan itself runs against
  the full-resolution upload; what is recompressed is the copy kept for a teacher
  to look at.
- **There are no signed URLs.** GridFS serves bytes over the database connection,
  so the API signs a short-lived token and serves the file from
  `GET /api/v1/files/{token}`. The capability is in the URL because a browser
  fetching an `<img src>` cannot send an Authorization header — so the token names
  one file, expires within the hour, and carries a `typ` that the access-token
  verifier refuses. `PUBLIC_API_URL` has to be set, or those links resolve against
  the frontend's origin.

Swapping to S3 or Cloudflare R2 later is one class: implement `ObjectStore`
(`upload`, `signed_url`, `open`) and change the provider in `api/deps.py`.

## Caching

Reads that barely change are served from an in-process cache
(`api/core/cache.py`). A warm dashboard load makes **zero** round trips to the
cluster, against ten cold — at a realistic 40ms away that is 400ms of waiting
removed from every page.

What is cached, and why each is safe to serve a few seconds late:

| Entry | TTL | Read on |
| --- | --- | --- |
| the caller's profile | 15s | **every authenticated request** |
| the school | 60s | nearly every write path, to resolve `school_id` |
| the subject list | 60s | every report and every picker |
| the section list | 60s | once per sheet, per export |
| a teacher's classes | 60s | their dashboard |
| the active template | 60s | every export |

Three rules make it safe, and all three are load-bearing:

1. **A write to a collection clears that collection's cache.** Wired into
   `MongoRepository`, not into the call sites — so caching is opt-in while
   invalidation is automatic, which is the safe way round. A read that embeds
   data from elsewhere declares `depends_on`, so renaming a section also clears
   the hydrated classes that carry a copy of it.
2. **The key includes every argument.** A cached read is never shared between
   two callers who asked different questions.
3. **Nothing about a token is cached.** Signature, expiry and audience are
   verified on every single request. What is cached is the profile *behind* an
   already-verified token — which is exactly why deactivating an account or
   changing a role must invalidate it, and why that entry has the shortest TTL
   of any.

Deliberately **not** cached: submissions, responses and item analysis. A
teacher writes and re-reads those in the same breath while reviewing a paper,
and a stale score is worse than a slow one.

**Why not Redis.** It would be another service to pay for and operate, which is
the opposite of why this system is on a free cluster. The cost of going without
is that each worker keeps its own cache, so an invalidation in one is not seen
by the others — which is why every entry also carries a short TTL. Staleness is
therefore bounded by the TTL even in the worst case, and `Cache` is a Protocol,
so a shared implementation can be dropped in without touching a caller.

`GET /api/v1/health/ready` reports the hit rate and entry count. A low hit rate
under real traffic means something is invalidating more than it needs to.

## Check it works

```bash
python scripts/doctor.py
```

The suite runs on `mongomock`, which implements the query language but not the
server — so it cannot establish the things that actually break a deployment:
whether the cluster is reachable from this IP, whether the **unique** indexes
exist (mongomock does not enforce uniqueness, so a missing one passes every test
and then lets two accounts share an email), and whether the TTL indexes are
applied. `doctor.py` is where those are checked. It writes nothing, prints
whether each secret is *set* and never its value, and exits non-zero on failure.

## Logs

```
08:48:44 AM  [esuri]  INFO:    127.0.0.1:52014 - "GET /api/v1/classes HTTP/1.1" 200 OK   12.4ms
08:48:45 AM  [esuri]  INFO:    127.0.0.1:52014 - "POST /api/v1/auth/login HTTP/1.1" 401 Unauthorized   88.0ms
08:48:47 AM  [esuri]  WARNING: sign_in_account_locked  user_id=a3f1b2c4 attempts=10
```

One aligned line per record: the time on a 12-hour clock, the service tag, the
level, then either a request line or a message with whatever fields it carried.
Status codes are coloured by class, so a wall of green with one red line in it
reads at a glance.

`LOG_FORMAT_MODE` chooses the format and defaults to `auto` - **pretty in
development, JSON in production**. That is not a preference: a log aggregator
can do nothing with colour codes, and nobody can read a wall of JSON while
debugging. Colour is dropped when stdout is not a terminal, so redirecting to a
file gives a readable file; `NO_COLOR=1` turns it off anywhere.

Two things are deliberately quiet. uvicorn's own access logger is disabled,
because `RequestContextMiddleware` already logs every request and knows the
correlation id and the real duration - both enabled would print each request
twice. And the chatty libraries (`httpx`, `watchfiles` under `--reload`,
`pymongo`) are raised to WARNING so they do not bury the requests; a real
problem from any of them still surfaces.

Everything is redacted on the way out, in both formats: bearer tokens, bare
JWTs, `password=`/`api_key=` pairs, and credentials inside a connection string
(`mongodb+srv://user:[redacted]@cluster...`, keeping the host so you can still
tell which one it was).

## Run

```bash
uvicorn api.main:app --reload
```

Swagger at http://localhost:8000/docs; health at `GET /api/v1/health`, which
reports the round-trip time to the cluster as well as the verdict.
Run from the repository root so `api/`, `dto/`, `db/` and `utils/` all resolve.

## Tests

```bash
pytest tests -q
```

No cluster, network or credentials required. 319 tests, in six groups:

| File | Covers |
| --- | --- |
| `test_mongo_repositories.py` | every repository method, the id coercion, the embedded arrays, hydration |
| `test_api_smoke.py` | the whole stack over HTTP — register, confirm, sign in, refresh, sign out, reset |
| `test_email_flows.py` | registration and recovery against the real gateway |
| `test_mongo_integration.py` | that every uniqueness rule has an index, and the seed agrees with the workbook |
| `test_cache.py` | that a write invalidates, and that a stale entry can never be an authorisation decision |
| `test_security.py` | the timing equality, the lockout, and what a self-service form may set |

## Accounts

A name is stored in parts — `last_name`, `first_name`, optional
`middle_initial`, optional `suffix` (DepEd's "name extension": Jr., Sr., III) —
because every official list sorts on the surname and a single string cannot be
split back apart reliably: "Maria Dela Cruz" has a two-word surname. The model
assembles `full_name` ("Maria S. Dela Cruz") and `formal_name`
("Dela Cruz, Maria S.") from them, so no caller concatenates names itself.

A profile holds only what every member of staff has. Anything belonging to one
role is embedded beside it:

| Field | Role | Holds |
| --- | --- | --- |
| (the document) | all | name, contact, school, active flag, role, password hash |
| `coordinator_assignment` | subject coordinator | the one learning area they are responsible for |
| `administrator_profile` | administrator | which of the three kinds, and the department a master teacher oversees |

A subject teacher's learning areas come from the classes they teach
(`classes.learning_area_id`), and they carry neither sub-document — so no record
holds a field that could not apply to it.

MongoDB has no constraint that can pin a sub-document to a role, so the rule is
enforced where it can be: the document validators refuse administrator details
on a teacher and a coordinator's area on anyone else, and
`CoordinatorAssignmentRepository.assign` refuses outright. There are tests for
each, because this is what decides whose reports someone can read.

`Profile.learning_area_id`, `admin_type` and `department_id` remain readable as
convenience properties, so callers that only need an id do not have to know where
it lives.

### The three kinds of administrator

All three hold the `administrator` role and everything a subject teacher can do;
they differ in how far they see and what they may close.

| Type | Sees | Also |
| --- | --- | --- |
| `assessment_coordinator` | the whole school | opens and closes a term — nothing else works until one is open |
| `master_teacher` | one department | their department's reports only, which is the point of the filter |
| `school_head` | the whole school | reads and downloads, without administering |

## Security

- **Rate limiting**: a 300-second sliding window on every tier, chosen by path
  (`AUTH` 10, `SESSION` 120, `EXPORT` 10, `SCAN` 60, `WRITE` 100, `READ` 300). A
  refusal carries `Retry-After: 300`. `SESSION` is separate from `AUTH` because
  renewing a token is routine and happens once per token lifetime per open tab,
  while a sign-in attempt is the thing worth throttling.
- **CORS is registered last**, making it the outermost layer. The rate limiter
  answers a 429 itself without calling the rest of the stack, so with CORS inside
  it the browser reported a CORS failure instead of "too many requests".
- **Passwords** are Argon2id — memory-hard, so a GPU cannot parallelise guesses
  cheaply. Sign-in re-hashes when the cost settings have been raised, at the one
  moment the plaintext is in hand.
- **Refresh tokens rotate.** Renewing revokes the old token and issues a new one,
  so a stolen one is good for one use at most. Only a SHA-256 of each is stored.
- **Sessions and mailed codes expire by TTL index**, so MongoDB deletes what has
  lapsed without a cleanup job anyone has to remember to write.
- **Files are reached by short-lived signed token only**, and a file token is
  refused as a bearer credential.
- **A rejected sign-in is indistinguishable by any means.** Every one returns
  the same words, and the same *timing*: an unknown address is verified against
  a throwaway hash so that the attempt costs what a real one costs. Without
  that, an unknown address answered in 0.0ms against 83ms for a real one, and
  the identical wording achieved nothing against a stopwatch.
- **An account locks itself** for fifteen minutes after ten failed sign-ins.
  This is per account, where the rate limiter is per caller: sign-in has no
  token to key on, so it keys on the client address, and an attacker spread
  over many addresses would never trip it. Being locked is reported with the
  same generic refusal, because "your account is locked" would be the
  enumeration oracle again.
- **The liveness probe is free; the readiness probe is not.** Readiness pings
  the cluster, so leaving it unmetered would be an unauthenticated endpoint
  anyone could use to make the database do work. It is on the READ tier.
- **`PATCH /auth/me` accepts names and a phone number, nothing else.** The DTO
  forbids unknown fields, so the accepted set is exhaustive and `role`,
  `is_active` and `password_hash` are not in it.
