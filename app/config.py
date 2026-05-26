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
    # In the workspace dev layout, _credentials/ sits 3-5 parents up; in
    # production deploy (/opt/resemblio-api/app/app/config.py) it doesn't exist
    # and the env comes from systemd EnvironmentFile. Walk safely.
    here = Path(__file__).resolve()
    candidates = []
    for depth in range(2, 8):
        try:
            candidates.append(here.parents[depth] / "_credentials" / "credentials.env")
        except IndexError:
            break
    for candidate in candidates:
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            clean_key = key.strip()
            if clean_key.startswith("STRIPE_") and clean_key.endswith("_LIVE"):
                continue
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
    stripe_restricted_key: str | None = Field(None, alias="STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST")
    stripe_webhook_secret: str | None = Field(None, alias="STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST")
    topup_success_url: str = Field("https://resemblio.com/dashboard/credit?topup=success", alias="RESEMBLIO_TOPUP_SUCCESS_URL")
    topup_cancel_url: str = Field("https://resemblio.com/dashboard/credit?topup=cancel", alias="RESEMBLIO_TOPUP_CANCEL_URL")
    resend_api_key: str | None = Field(None, alias="RESEND_API_KEY")
    resend_from_email: str = Field("Resemblio <hello@resemblio.com>", alias="RESEMBLIO_RESEND_FROM_EMAIL")
    default_key_env: str = Field("live", alias="RESEMBLIO_KEY_ENV")
    log_level: str = Field("INFO", alias="RESEMBLIO_LOG_LEVEL")

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")


def validate_startup_settings(settings: Settings) -> None:
    """Fail fast when security-critical settings are missing or unsafe."""
    if len(settings.key_pepper) < MIN_KEY_PEPPER_CHARS:
        raise RuntimeError("RESEMBLIO_KEY_PEPPER must be set to at least 32 characters")
    validate_stripe_test_settings(settings)


def validate_stripe_test_settings(settings: Settings) -> None:
    """Require Stripe test-mode credentials and reject live-mode key material."""
    if not settings.stripe_restricted_key:
        raise RuntimeError("STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST is required for Stripe TEST mode")
    if not settings.stripe_webhook_secret:
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST is required. Create the Stripe webhook endpoint and paste its signing secret into credentials.env."
        )
    test_values = {
        "STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST": settings.stripe_restricted_key,
        "STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST": settings.stripe_webhook_secret,
    }
    for name, value in test_values.items():
        if "_live_" in value.lower() or value.lower().startswith(("sk_live", "rk_live")):
            raise RuntimeError(f"{name} contains Stripe LIVE key material; TEST mode is required")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings after loading the shared credentials file once."""
    load_project_env()
    return Settings()


def reset_settings_cache() -> None:
    """Clear cached settings for tests that mutate environment variables."""
    get_settings.cache_clear()
