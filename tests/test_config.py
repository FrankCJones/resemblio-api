"""Unit tests for ``app.config`` Stripe mode validation.

Covers the test/live validator pair and the ``RESEMBLIO_STRIPE_MODE`` dispatch
in ``validate_startup_settings``. Synthetic fixtures only: every key value
below is a hand-crafted ``sk_test_FAKE`` / ``rk_live_FAKE`` / ``whsec_FAKE``
string that passes prefix checks but cannot reach real Stripe.
"""
from __future__ import annotations

import pytest

from app.config import (
    Settings,
    validate_startup_settings,
    validate_stripe_live_settings,
    validate_stripe_test_settings,
)

# Synthetic key fixtures. ``FAKE`` suffix exists to make the value visually
# distinct from a real Stripe secret in any log scrape.
_TEST_RESTRICTED = "rk_test_FAKEFAKEFAKE"
_TEST_WEBHOOK_SECRET = "whsec_test_FAKEFAKEFAKE"
_LIVE_RESTRICTED = "rk_live_FAKEFAKEFAKE"
_LIVE_WEBHOOK_SECRET = "whsec_live_FAKEFAKEFAKE"
_PEPPER = "test-pepper-value-with-thirty-two-chars"


def _settings(
    *,
    mode: str = "test",
    restricted: str = _TEST_RESTRICTED,
    webhook: str = _TEST_WEBHOOK_SECRET,
) -> Settings:
    """Build a Settings instance with the bare minimum fields populated."""
    return Settings(
        RESEMBLIO_KEY_PEPPER=_PEPPER,
        STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST=restricted,
        STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST=webhook,
        RESEMBLIO_STRIPE_MODE=mode,
    )


def test_validate_stripe_test_settings_accepts_test_keys() -> None:
    """TEST validator passes silently on test-prefixed keys."""
    validate_stripe_test_settings(_settings(mode="test"))


def test_validate_stripe_test_settings_rejects_live_keys_with_helpful_error() -> None:
    """TEST validator rejects LIVE restricted key material and names the prefix."""
    settings = _settings(mode="test", restricted=_LIVE_RESTRICTED)
    with pytest.raises(RuntimeError) as exc:
        validate_stripe_test_settings(settings)
    message = str(exc.value)
    assert "LIVE key material" in message
    assert "RESEMBLIO_STRIPE_MODE=test" in message
    # Probe-usefulness: the operator sees WHICH key got dropped in.
    assert _LIVE_RESTRICTED[:8] in message


def test_validate_stripe_live_settings_accepts_live_keys() -> None:
    """LIVE validator passes silently on live-prefixed keys."""
    settings = _settings(mode="live", restricted=_LIVE_RESTRICTED, webhook=_LIVE_WEBHOOK_SECRET)
    validate_stripe_live_settings(settings)


def test_validate_stripe_live_settings_rejects_test_keys_with_helpful_error() -> None:
    """LIVE validator rejects TEST restricted key material and names the prefix."""
    settings = _settings(mode="live", restricted=_TEST_RESTRICTED, webhook=_LIVE_WEBHOOK_SECRET)
    with pytest.raises(RuntimeError) as exc:
        validate_stripe_live_settings(settings)
    message = str(exc.value)
    assert "TEST key material" in message
    assert "RESEMBLIO_STRIPE_MODE=live" in message
    assert _TEST_RESTRICTED[:8] in message


def test_validate_stripe_live_settings_rejects_non_whsec_webhook_secret() -> None:
    """LIVE validator rejects a webhook secret that does not start with whsec_."""
    settings = _settings(mode="live", restricted=_LIVE_RESTRICTED, webhook="bogus_FAKE")
    with pytest.raises(RuntimeError) as exc:
        validate_stripe_live_settings(settings)
    assert "whsec_" in str(exc.value)


def test_validate_stripe_live_settings_rejects_non_live_prefix() -> None:
    """LIVE validator rejects a restricted key with neither test nor live prefix."""
    settings = _settings(mode="live", restricted="bogus_FAKE", webhook=_LIVE_WEBHOOK_SECRET)
    with pytest.raises(RuntimeError) as exc:
        validate_stripe_live_settings(settings)
    message = str(exc.value)
    assert "sk_live_" in message or "rk_live_" in message


def test_mode_flag_drives_validator_selection() -> None:
    """validate_startup_settings routes to the validator named by stripe_mode."""
    # mode=test + test keys: passes
    validate_startup_settings(_settings(mode="test"))
    # mode=live + live keys: passes
    validate_startup_settings(
        _settings(mode="live", restricted=_LIVE_RESTRICTED, webhook=_LIVE_WEBHOOK_SECRET)
    )
    # mode=test + live restricted key: TEST validator fires and rejects
    with pytest.raises(RuntimeError, match="LIVE key material"):
        validate_startup_settings(_settings(mode="test", restricted=_LIVE_RESTRICTED))
    # mode=live + test restricted key: LIVE validator fires and rejects
    with pytest.raises(RuntimeError, match="TEST key material"):
        validate_startup_settings(
            _settings(mode="live", restricted=_TEST_RESTRICTED, webhook=_LIVE_WEBHOOK_SECRET)
        )


def test_default_mode_is_test_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset RESEMBLIO_STRIPE_MODE defaults to test (safer-side fallback)."""
    monkeypatch.delenv("RESEMBLIO_STRIPE_MODE", raising=False)
    settings = Settings(
        RESEMBLIO_KEY_PEPPER=_PEPPER,
        STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST=_TEST_RESTRICTED,
        STRIPE_WEBHOOK_SECRET_RESEMBLIO_TEST=_TEST_WEBHOOK_SECRET,
    )
    assert settings.stripe_mode == "test"
