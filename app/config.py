"""Environment-backed configuration for the API service."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    # Stripe operating mode. Default is "test" so any deploy that forgets to set
    # the flag falls back to the safer side (rejects LIVE keys at startup rather
    # than silently accepting them). The env var alias intentionally has no
    # "_TEST"/"_LIVE" suffix; the *value* names the mode.
    stripe_mode: Literal["test", "live"] = Field("test", alias="RESEMBLIO_STRIPE_MODE")

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")


def validate_startup_settings(settings: Settings) -> None:
    """Fail fast when security-critical settings are missing or unsafe.

    Dispatches Stripe-key validation to the test- or live-mode validator based
    on ``settings.stripe_mode``. Each validator rejects the OTHER mode's key
    material so a misconfigured env (right value in wrong mode, or right mode
    with wrong key) crashes at boot rather than at first customer charge.
    """
    if len(settings.key_pepper) < MIN_KEY_PEPPER_CHARS:
        raise RuntimeError("RESEMBLIO_KEY_PEPPER must be set to at least 32 characters")
    if settings.stripe_mode == "live":
        validate_stripe_live_settings(settings)
    else:
        validate_stripe_test_settings(settings)


def _key_prefix(value: str, chars: int = 8) -> str:
    """Return the first ``chars`` characters of a secret for error messages.

    Used to surface WHICH key got dropped into the env without leaking the
    full secret. 8 characters is enough to distinguish ``sk_live_`` from
    ``sk_test_`` / ``rk_live_`` / ``rk_test_`` / ``whsec_xx`` without revealing
    meaningful entropy.
    """
    return value[:chars] if value else "<empty>"


def _looks_like_live(value: str) -> bool:
    """Return True if value carries Stripe LIVE key material.

    Covers both restricted/secret keys (``sk_live_*`` / ``rk_live_*``) and any
    value where the substring ``_live_`` appears (defensive against future
    Stripe prefix changes). Webhook signing secrets (``whsec_*``) do NOT carry
    a mode marker; the caller must distinguish webhook secrets some other way.
    """
    lowered = value.lower()
    return lowered.startswith(("sk_live", "rk_live")) or "_live_" in lowered


def _looks_like_test(value: str) -> bool:
    """Return True if value carries Stripe TEST key material."""
    lowered = value.lower()
    return lowered.startswith(("sk_test", "rk_test")) or "_test_" in lowered


def validate_stripe_test_settings(settings: Settings) -> None:
    """Require Stripe TEST credentials and reject any LIVE key material.

    Env var aliases retain the historical ``_TEST`` suffix; see
    ``Resemblio_STRIPE.md`` Section 2 for why the alias name is fixed and the
    *value* determines the mode.
    """
    if not settings.stripe_restricted_key:
        raise RuntimeError("STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST is required for Stripe TEST mode")
    if not settings.stripe_webhook_secret:
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST is required. Create the Stripe webhook endpoint and paste its signing secret into credentials.env."
        )
    values = {
        "STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST": settings.stripe_restricted_key,
        "STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST": settings.stripe_webhook_secret,
    }
    for name, value in values.items():
        if _looks_like_live(value):
            raise RuntimeError(
                f"RESEMBLIO_STRIPE_MODE=test but {name} carries LIVE key material "
                f"(prefix={_key_prefix(value)!r}). Either set RESEMBLIO_STRIPE_MODE=live "
                f"or swap the env value back to the TEST key."
            )


def validate_stripe_live_settings(settings: Settings) -> None:
    """Require Stripe LIVE credentials and reject any TEST key material.

    Mirrors ``validate_stripe_test_settings``. The restricted key MUST start
    with ``sk_live_`` or ``rk_live_``. The webhook signing secret MUST start
    with ``whsec_`` (Stripe uses the same ``whsec_`` prefix for both modes; the
    only way to be wrong about a webhook secret is to bind a TEST endpoint's
    secret to a LIVE-mode process, which silently passes prefix checks but
    fails signature verification on the first real event). Operators verify
    that case via the Section 8 smoke step in ``Resemblio_STRIPE.md`` and via
    ``scripts/smoke_stripe_mode.py``.
    """
    if not settings.stripe_restricted_key:
        raise RuntimeError("STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST is required for Stripe LIVE mode")
    if not settings.stripe_webhook_secret:
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST is required. Create the Stripe webhook endpoint and paste its signing secret into credentials.env."
        )
    restricted_name = "STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST"
    restricted_value = settings.stripe_restricted_key
    if _looks_like_test(restricted_value):
        raise RuntimeError(
            f"RESEMBLIO_STRIPE_MODE=live but {restricted_name} carries TEST key material "
            f"(prefix={_key_prefix(restricted_value)!r}). Either set RESEMBLIO_STRIPE_MODE=test "
            f"or swap the env value to the LIVE key."
        )
    if not (restricted_value.lower().startswith("sk_live") or restricted_value.lower().startswith("rk_live")):
        raise RuntimeError(
            f"RESEMBLIO_STRIPE_MODE=live requires {restricted_name} to start with "
            f"'sk_live_' or 'rk_live_'; got prefix={_key_prefix(restricted_value)!r}."
        )
    webhook_name = "STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST"
    webhook_value = settings.stripe_webhook_secret
    if not webhook_value.lower().startswith("whsec_"):
        raise RuntimeError(
            f"RESEMBLIO_STRIPE_MODE=live requires {webhook_name} to start with 'whsec_'; "
            f"got prefix={_key_prefix(webhook_value)!r}."
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings after loading the shared credentials file once."""
    load_project_env()
    return Settings()


def reset_settings_cache() -> None:
    """Clear cached settings for tests that mutate environment variables."""
    get_settings.cache_clear()
