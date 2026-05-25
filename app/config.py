"""Environment-backed configuration for the API service."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.constants import MIN_KEY_PEPPER_CHARS, R2_BUCKET_NAME


def load_project_env() -> None:
    """Load workspace credentials without printing or overriding existing values.

    The shared credentials file lives outside this project. Missing files are
    ignored so tests and local shells can provide environment variables directly.
    Lines without an equals sign and comment lines are skipped.
    """
    candidates = [
        Path(__file__).resolve().parents[5] / "_credentials" / "credentials.env",
        Path(__file__).resolve().parents[4] / "_credentials" / "credentials.env",
        Path(__file__).resolve().parents[3] / "_credentials" / "credentials.env",
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            clean_key = key.strip()
            clean_value = value.strip().strip('"').strip("'")
            if not os.environ.get(clean_key):
                os.environ[clean_key] = clean_value
        return


class Settings(BaseSettings):
    """Runtime settings loaded from the process environment."""

    database_url: str = Field("sqlite+pysqlite:///./resemblio_dev.db", alias="RESEMBLIO_DB_URL")
    key_pepper: str = Field("", alias="RESEMBLIO_KEY_PEPPER")
    key_pepper_old: str | None = Field(None, alias="RESEMBLIO_KEY_PEPPER_OLD")
    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")
    r2_endpoint: str | None = Field(None, alias="CLOUDFLARE_R2_ENDPOINT")
    r2_access_key: str | None = Field(None, alias="CLOUDFLARE_R2_ACCESS_KEY")
    r2_secret_key: str | None = Field(None, alias="CLOUDFLARE_R2_SECRET_KEY")
    r2_bucket: str = Field(R2_BUCKET_NAME, alias="RESEMBLIO_R2_BUCKET")
    r2_region: str = Field("auto", alias="RESEMBLIO_R2_REGION")
    default_key_env: str = Field("live", alias="RESEMBLIO_KEY_ENV")
    log_level: str = Field("INFO", alias="RESEMBLIO_LOG_LEVEL")

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")


def validate_startup_settings(settings: Settings) -> None:
    """Fail fast when API-key hashing cannot be performed safely."""
    if len(settings.key_pepper) < MIN_KEY_PEPPER_CHARS:
        raise RuntimeError("RESEMBLIO_KEY_PEPPER must be set to at least 32 characters")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings after loading the shared credentials file once."""
    load_project_env()
    return Settings()


def reset_settings_cache() -> None:
    """Clear cached settings for tests that mutate environment variables."""
    get_settings.cache_clear()
