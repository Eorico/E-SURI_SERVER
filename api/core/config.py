"""Application settings, loaded once from the environment (DRY: single source).

Every tunable lives here so no module reads `os.environ` on its own. Values are
validated at import, which means a misconfigured deployment fails at start-up
rather than on the first request that needs the missing key.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str | list[str]) -> list[str]:
    """Accept either a JSON list or a plain comma-separated string from .env."""
    if isinstance(value, list):
        return value
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

    # --- Environment ------------------------------------------------------
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    # --- Supabase ---------------------------------------------------------
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    SUPABASE_JWT_SECRET: str = ""
    SUPABASE_JWT_AUDIENCE: str = "authenticated"

    # --- Database ---------------------------------------------------------
    DATABASE_URL: str = "sqlite+pysqlite:///./dev.db"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_STATEMENT_TIMEOUT_MS: int = 15_000

    # --- HTTP -------------------------------------------------------------
    API_PREFIX: str = "/api/v1"
    CORS_ORIGINS: list[str] = ["http://localhost:8080"]
    TRUSTED_HOSTS: list[str] = ["*"]
    #: Hard ceiling on a request body. Answer-sheet photos are the largest input.
    MAX_REQUEST_BYTES: int = 8 * 1024 * 1024
    MAX_UPLOAD_BYTES: int = 6 * 1024 * 1024
    ALLOWED_UPLOAD_TYPES: list[str] = ["image/jpeg", "image/png", "image/webp"]

    # --- Rate limiting ----------------------------------------------------
    #: Every limit uses the same five-minute window.
    RATE_LIMIT_WINDOW_SECONDS: int = 300
    RATE_LIMIT_ENABLED: bool = True
    #: Requests allowed per window, per client, by route class.
    RATE_LIMIT_AUTH: int = 10
    RATE_LIMIT_SCAN: int = 60
    RATE_LIMIT_WRITE: int = 100
    RATE_LIMIT_READ: int = 300
    RATE_LIMIT_EXPORT: int = 10

    # --- Storage ----------------------------------------------------------
    ANSWER_SHEET_BUCKET: str = "answer-sheets"
    CERTIFICATE_BUCKET: str = "certificates"
    SIGNED_URL_TTL_SECONDS: int = 3600

    # --- Reporting --------------------------------------------------------
    EQWART_TEMPLATE_PATH: str = "supabase/templates/eqwart_grade11.xlsm"
    REPORT_TEMPLATE_BUCKET: str = "report-templates"
    #: Uploaded templates are workbooks, not photos, so they get their own cap.
    MAX_TEMPLATE_BYTES: int = 12 * 1024 * 1024
    ALLOWED_TEMPLATE_TYPES: list[str] = [
        "application/vnd.ms-excel.sheet.macroEnabled.12",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ]

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

    @field_validator("DATABASE_URL")
    @classmethod
    def _require_real_database_in_production(cls, value: str, info) -> str:
        if info.data.get("ENVIRONMENT") == "production" and value.startswith("sqlite"):
            raise ValueError("DATABASE_URL must point at Postgres in production")
        return value


@lru_cache
def get_settings() -> Settings:
    """Cached accessor, so tests can clear it and re-read the environment."""
    return Settings()


settings = get_settings()
