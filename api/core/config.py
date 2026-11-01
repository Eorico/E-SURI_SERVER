"""Application settings, loaded once from the environment (DRY: single source).

Every tunable lives here so no module reads `os.environ` on its own. Values are
validated at import, which means a misconfigured deployment fails at start-up
rather than on the first request that needs the missing key.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: str | list[str]) -> list[str]:
    """Accept either a JSON list or a plain comma-separated string from .env."""
    if isinstance(value, list):
        return value
    value = value.strip()
    if value.startswith("["):
        # Still allow the JSON form, since NoDecode turned off the automatic one.
        import json

        return [str(item).strip() for item in json.loads(value)]
    return [item.strip() for item in value.split(",") if item.strip()]


#: A list that is written as `a,b,c` in `.env`.
#:
#: `NoDecode` is what makes `_split_csv` run at all. Without it pydantic-settings
#: JSON-decodes every complex field inside the *source*, before field validators
#: are reached, so a plain comma-separated line raised `SettingsError` and the
#: server would not start — exactly the format `.env.example` documents.
CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

    # --- Environment ------------------------------------------------------
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    # --- Authentication ---------------------------------------------------
    #: This server issues and verifies its own tokens: password storage, token
    #: signing, confirmation codes and sign-out all happen here, and there is
    #: no external identity provider involved in any of it.
    #:
    #: The secret signs every access and refresh token. There is deliberately
    #: no default - a guessable signing key lets anyone mint a token for any
    #: account - so the server refuses to start in production without one.
    #: Generate one with:
    #:     python -c "import secrets; print(secrets.token_urlsafe(48))"
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "esuri"
    #: Left as "authenticated" so the claim the verifier checks is unchanged.
    JWT_AUDIENCE: str = "authenticated"
    #: An hour. Short enough that a leaked access token is not worth much,
    #: long enough that the client is not refreshing constantly - and it is
    #: what the frontend's 401-then-refresh path is built around.
    ACCESS_TOKEN_TTL_SECONDS: int = 3600
    #: Two weeks. Refresh tokens are recorded, so signing out really ends the
    #: session instead of leaving a usable token until it lapses.
    REFRESH_TOKEN_TTL_SECONDS: int = 60 * 60 * 24 * 14
    #: Registration codes are typed by hand off a phone screen, so they are
    #: deliberately short-lived.
    SIGNUP_CODE_TTL_SECONDS: int = 900
    PASSWORD_RESET_TTL_SECONDS: int = 3600
    #: Six digits, which is what the form was built around. The input stays
    #: length-agnostic regardless: a form that assumes a length breaks the
    #: moment this number changes, and that has happened once already.
    SIGNUP_CODE_LENGTH: int = 6
    MIN_PASSWORD_LENGTH: int = 8
    #: Failed sign-ins before an account refuses attempts for a while.
    #:
    #: This is per account, where the rate limiter is per caller: sign-in has
    #: no token to key on, so it keys on the client address, and an attacker
    #: spread over many addresses would never trip it. Ten is high enough that
    #: somebody mistyping their own password will not notice.
    MAX_FAILED_SIGN_INS: int = 10
    #: How long the account then refuses attempts. Long enough to make grinding
    #: pointless, short enough that a locked-out teacher can wait it out rather
    #: than needing an administrator.
    SIGN_IN_LOCKOUT_SECONDS: int = 900

    #: Argon2id cost. These are argon2-cffi's own defaults, written down so a
    #: slow sign-in traces to a deliberate number rather than a mystery.
    ARGON2_TIME_COST: int = 3
    ARGON2_MEMORY_COST_KIB: int = 65_536
    ARGON2_PARALLELISM: int = 4

    # --- MongoDB ----------------------------------------------------------
    #: An Atlas SRV string in deployment:
    #:   mongodb+srv://USER:PASS@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
    MONGODB_URI: str = "mongodb://localhost:27017"
    MONGODB_DB: str = "esuri"
    MONGO_MAX_POOL_SIZE: int = 20
    MONGO_MIN_POOL_SIZE: int = 0
    #: Fail a request rather than hang when the cluster cannot be reached.
    MONGO_SERVER_SELECTION_TIMEOUT_MS: int = 8_000
    MONGO_CONNECT_TIMEOUT_MS: int = 8_000
    #: The ceiling on any one operation. A runaway query is a denial-of-
    #: service risk, so it is capped rather than left to finish.
    MONGO_SOCKET_TIMEOUT_MS: int = 20_000
    #: Retire an idle connection before Atlas drops it at the far end. A
    #: connection closed remotely is not noticed until it is used, so it costs
    #: a failed round trip before it is replaced.
    MONGO_MAX_IDLE_TIME_MS: int = 900_000

    # --- HTTP -------------------------------------------------------------
    API_PREFIX: str = "/api/v1"
    CORS_ORIGINS: CsvList = ["http://localhost:8080"]
    TRUSTED_HOSTS: CsvList = ["*"]
    #: Hard ceiling on a request body. Answer-sheet photos are the largest input.
    MAX_REQUEST_BYTES: int = 8 * 1024 * 1024
    MAX_UPLOAD_BYTES: int = 6 * 1024 * 1024
    ALLOWED_UPLOAD_TYPES: CsvList = ["image/jpeg", "image/png", "image/webp"]

    # --- Rate limiting ----------------------------------------------------
    #: Every limit uses the same five-minute window.
    RATE_LIMIT_WINDOW_SECONDS: int = 300
    RATE_LIMIT_ENABLED: bool = True
    #: Requests allowed per window, per client, by route class.
    RATE_LIMIT_AUTH: int = 10
    #: Token renewal. Generous on purpose: a dashboard opens a dozen queries
    #: at once, and if the access token has just expired they all meet a 401
    #: together. Throttling that locks a legitimate person out of their own
    #: session, which is what RATE_LIMIT_AUTH did when refresh shared it.
    RATE_LIMIT_SESSION: int = 120
    RATE_LIMIT_SCAN: int = 60
    RATE_LIMIT_WRITE: int = 100
    RATE_LIMIT_READ: int = 300
    RATE_LIMIT_EXPORT: int = 10

    # --- Storage (GridFS, inside the same cluster) ------------------------
    #: Bucket names are unchanged, so every stored path still reads the same.
    ANSWER_SHEET_BUCKET: str = "answer-sheets"
    CERTIFICATE_BUCKET: str = "certificates"
    SIGNED_URL_TTL_SECONDS: int = 3600

    #: Sheets are recompressed before they are stored, because GridFS shares
    #: the cluster's storage with every record in it. A 3MB phone photo holds
    #: far more detail than the grid needs: at 1600px on the long edge and in
    #: grayscale a sheet lands near 200KB, which is the difference between a
    #: few hundred sheets fitting and a few thousand.
    #:
    #: Set SHEET_RECOMPRESS=false to keep originals byte for byte.
    SHEET_RECOMPRESS: bool = True
    SHEET_MAX_EDGE_PX: int = 1600
    SHEET_JPEG_QUALITY: int = 72
    SHEET_GRAYSCALE: bool = True

    # --- Reporting --------------------------------------------------------
    EQWART_TEMPLATE_PATH: str = "db/templates/eqwart_grade11.xlsm"
    REPORT_TEMPLATE_BUCKET: str = "report-templates"
    #: Uploaded templates are workbooks, not photos, so they get their own cap.
    MAX_TEMPLATE_BYTES: int = 12 * 1024 * 1024
    ALLOWED_TEMPLATE_TYPES: CsvList = [
        "application/vnd.ms-excel.sheet.macroEnabled.12",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ]

    # --- Email ------------------------------------------------------------
    #: Confirmation and password-reset mail is composed, signed and sent
    #: here. The code and the link both carry a token this server issued and
    #: this server verifies, so the wording, the sender and the expiry all
    #: live in the repository rather than in somebody else's dashboard.
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    #: For Gmail this is an App Password, not the account password.
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_FROM_NAME: str = "E-SURI"
    #: 587 wants STARTTLS; 465 wants implicit TLS. Set together with the port.
    SMTP_STARTTLS: bool = True
    SMTP_TIMEOUT_SECONDS: int = 20
    #: Off by default so a development machine never mails a real address; the
    #: link is written to the log instead.
    EMAIL_ENABLED: bool = False
    #: Where a confirmation or reset link sends the person afterwards.
    PUBLIC_APP_URL: str = "http://localhost:8080"
    #: This API's own address, as a browser reaches it. Needed because a file
    #: link now points here rather than at a storage provider: GridFS has no
    #: URL of its own, so the API serves the bytes and the link must be
    #: absolute - an `<img src>` would otherwise resolve it against the
    #: frontend's origin.
    PUBLIC_API_URL: str = "http://localhost:8000"

    # --- OpenAI (template mapping only; never student data) ---------------
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_TIMEOUT_SECONDS: int = 60

    _split = field_validator(
        "CORS_ORIGINS",
        "TRUSTED_HOSTS",
        "ALLOWED_UPLOAD_TYPES",
        "ALLOWED_TEMPLATE_TYPES",
        mode="before",
    )(_split_csv)

    @field_validator("MONGODB_URI")
    @classmethod
    def _require_a_real_cluster_in_production(cls, value: str, info) -> str:
        if info.data.get("ENVIRONMENT") != "production":
            return value
        if "localhost" in value or "127.0.0.1" in value:
            raise ValueError("MONGODB_URI must point at a real cluster in production")
        return value

    @field_validator("JWT_SECRET")
    @classmethod
    def _require_a_signing_key_in_production(cls, value: str, info) -> str:
        """Fail at start-up rather than on the first sign-in.

        An empty signing key is not merely a weak one: anyone who knows the
        algorithm can mint a token for any account, an administrator's
        included. There is no safe default, so there is no default.
        """
        if info.data.get("ENVIRONMENT") == "production" and len(value) < 32:
            raise ValueError(
                "JWT_SECRET must be at least 32 characters in production. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return value


@lru_cache
def get_settings() -> Settings:
    """Cached accessor, so tests can clear it and re-read the environment."""
    return Settings()


settings = get_settings()
