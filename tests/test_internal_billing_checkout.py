"""Tests for the S3b Wave 2c internal billing surface.

Covers ``POST /v1/internal/billing/create_checkout_session``, the new
internal-secret-gated route the Next.js BFF calls on a logged-in user's
behalf to create a Stripe Checkout session for credit top-up.

The route is gated by both the ``RESEMBLIO_BILLING_UI_ENABLED`` feature flag
(read at request time) and the ``X-Internal-Auth`` shared secret. Both gates
have their own test below.

No network: the Stripe gateway is replaced with an in-process fake via the
existing ``get_stripe_service`` dependency-override pattern (see
``test_stripe_checkout_create.py``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import reset_settings_cache
from app.constants import (
    BILLING_UI_FLAG_ENV_VAR,
    TOPUP_BUNDLE_100_CENTS_PAID,
    TOPUP_BUNDLE_20_CENTS_PAID,
    TOPUP_BUNDLE_500_CENTS_PAID,
)
from app.main import app
from app.models import TopupSession
from app.payments import CheckoutSessionResult, get_stripe_service
from tests.conftest import seed_user


INTERNAL_SECRET = "test-internal-auth-secret-for-tests"


@dataclass
class _FakeStripeService:
    """Stripe fake that records Checkout session inputs.

    Mirrors the fake used by ``test_stripe_checkout_create.py`` so the two
    test files exercise the gateway protocol identically.
    """

    last_user_id: int | None = None
    last_customer_id: str | None = None
    last_amount_cents: int | None = None
    session_id: str = "cs_live_fake_billing_123"

    def create_customer(self, email: str) -> str:
        """Return a deterministic fake customer id (unused on this surface)."""
        return f"cus_{email.replace('@', '_')}"

    def create_checkout_session(
        self, user_id: int, stripe_customer_id: str, amount_cents: int
    ) -> CheckoutSessionResult:
        """Capture Checkout payload fields and return a fake session."""
        self.last_user_id = user_id
        self.last_customer_id = stripe_customer_id
        self.last_amount_cents = amount_cents
        return CheckoutSessionResult(
            id=self.session_id,
            url=f"https://checkout.stripe.test/session/{self.session_id}",
        )


@pytest.fixture
def enable_billing_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the feature flag to ``true`` and the internal-auth secret.

    Both are read at request time so a single monkeypatch + cache reset is
    enough to make the route accept calls for the duration of one test.
    """
    monkeypatch.setenv(BILLING_UI_FLAG_ENV_VAR, "true")
    monkeypatch.setenv("RESEMBLIO_INTERNAL_AUTH_SECRET", INTERNAL_SECRET)
    reset_settings_cache()


def _install_fake_stripe() -> _FakeStripeService:
    """Bind a fresh fake to the gateway dependency and return it."""
    fake = _FakeStripeService()
    app.dependency_overrides[get_stripe_service] = _bind_fake(fake)
    return fake


def _bind_fake(fake: _FakeStripeService):
    """Return a zero-arg provider closing over ``fake``.

    A bare ``lambda f=fake: f`` exposes ``f`` as a callable parameter, and
    FastAPI's dependency resolver introspects that signature and constructs
    a fresh value for the parameter rather than using the captured default.
    A closure with no parameters sidesteps the resolver entirely.
    """
    def _provider() -> _FakeStripeService:
        return fake
    return _provider


def _internal_headers() -> dict[str, str]:
    """Return the internal-secret header used by the BFF."""
    return {"X-Internal-Auth": INTERNAL_SECRET}


# --- Happy path -----------------------------------------------------------


def test_create_checkout_session_happy_path(
    client: TestClient, session: Session, enable_billing_flag: None
) -> None:
    """A valid bundle amount mints a Checkout session and writes TopupSession."""
    user, _api_key, _plaintext = seed_user(session)
    fake = _install_fake_stripe()
    response = client.post(
        "/v1/internal/billing/create_checkout_session",
        headers=_internal_headers(),
        json={"user_id": user.id, "amount_cents": TOPUP_BUNDLE_20_CENTS_PAID},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == fake.session_id
    assert body["checkout_url"].endswith(fake.session_id)
    assert body["schema_version"] == 1
    # Stripe gateway captured the correct args.
    assert fake.last_user_id == user.id
    assert fake.last_customer_id == "cus_test_seed"
    assert fake.last_amount_cents == TOPUP_BUNDLE_20_CENTS_PAID
    # TopupSession row exists, binding session_id -> (user_id, amount).
    topup = session.get(TopupSession, fake.session_id)
    assert topup is not None
    assert topup.user_id == user.id
    assert topup.amount_cents == TOPUP_BUNDLE_20_CENTS_PAID
    assert topup.status == "pending"


def test_create_checkout_session_accepts_all_three_bundle_tiers(
    client: TestClient, session: Session, enable_billing_flag: None
) -> None:
    """Each of the three documented bundle tiers ($20/$100/$500) is accepted.

    The closed-set check at the route boundary is the only place client-side
    amount values are trusted; this test asserts the set matches the
    canonical pricing reference exactly.
    """
    user, _api_key, _plaintext = seed_user(session)
    for tier, amount in enumerate(
        [TOPUP_BUNDLE_20_CENTS_PAID, TOPUP_BUNDLE_100_CENTS_PAID, TOPUP_BUNDLE_500_CENTS_PAID]
    ):
        fake = _FakeStripeService(session_id=f"cs_live_fake_{tier}")
        app.dependency_overrides[get_stripe_service] = _bind_fake(fake)
        response = client.post(
            "/v1/internal/billing/create_checkout_session",
            headers=_internal_headers(),
            json={"user_id": user.id, "amount_cents": amount},
        )
        assert response.status_code == 200, (amount, response.text)
        assert fake.last_amount_cents == amount


# --- Feature flag gating --------------------------------------------------


def test_create_checkout_session_returns_503_when_flag_unset(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``RESEMBLIO_BILLING_UI_ENABLED`` unset the route returns 503.

    This is the DEFAULT POST-DEPLOY STATE before Frank's own-card LIVE smoke;
    no Stripe Checkout sessions can be created until he flips the flag.
    """
    monkeypatch.delenv(BILLING_UI_FLAG_ENV_VAR, raising=False)
    monkeypatch.setenv("RESEMBLIO_INTERNAL_AUTH_SECRET", INTERNAL_SECRET)
    reset_settings_cache()
    user, _api_key, _plaintext = seed_user(session)
    fake = _install_fake_stripe()
    response = client.post(
        "/v1/internal/billing/create_checkout_session",
        headers=_internal_headers(),
        json={"user_id": user.id, "amount_cents": TOPUP_BUNDLE_20_CENTS_PAID},
    )
    assert response.status_code == 503
    assert response.json()["error"] == "billing_ui_disabled"
    # The Stripe gateway must NOT have been called.
    assert fake.last_amount_cents is None


def test_create_checkout_session_returns_503_when_flag_false(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A literal value other than 'true' (case-insensitive) also disables."""
    monkeypatch.setenv(BILLING_UI_FLAG_ENV_VAR, "false")
    monkeypatch.setenv("RESEMBLIO_INTERNAL_AUTH_SECRET", INTERNAL_SECRET)
    reset_settings_cache()
    user, _api_key, _plaintext = seed_user(session)
    _install_fake_stripe()
    response = client.post(
        "/v1/internal/billing/create_checkout_session",
        headers=_internal_headers(),
        json={"user_id": user.id, "amount_cents": TOPUP_BUNDLE_20_CENTS_PAID},
    )
    assert response.status_code == 503
    assert response.json()["error"] == "billing_ui_disabled"


# --- Internal-secret gating -----------------------------------------------


def test_create_checkout_session_rejects_missing_internal_secret(
    client: TestClient, session: Session, enable_billing_flag: None
) -> None:
    """Missing ``X-Internal-Auth`` header returns 401, no Stripe call."""
    user, _api_key, _plaintext = seed_user(session)
    fake = _install_fake_stripe()
    response = client.post(
        "/v1/internal/billing/create_checkout_session",
        json={"user_id": user.id, "amount_cents": TOPUP_BUNDLE_20_CENTS_PAID},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "internal_auth_invalid"
    assert fake.last_amount_cents is None


def test_create_checkout_session_rejects_wrong_internal_secret(
    client: TestClient, session: Session, enable_billing_flag: None
) -> None:
    """Wrong shared secret returns 401, no Stripe call."""
    user, _api_key, _plaintext = seed_user(session)
    fake = _install_fake_stripe()
    response = client.post(
        "/v1/internal/billing/create_checkout_session",
        headers={"X-Internal-Auth": "not-the-real-secret"},
        json={"user_id": user.id, "amount_cents": TOPUP_BUNDLE_20_CENTS_PAID},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "internal_auth_invalid"
    assert fake.last_amount_cents is None


# --- Validation -----------------------------------------------------------


def test_create_checkout_session_rejects_off_bundle_amount(
    client: TestClient, session: Session, enable_billing_flag: None
) -> None:
    """Amounts outside the documented bundle set are rejected at the boundary.

    Belt-and-braces defense against a tampered BFF call (or a manually
    crafted internal-secret-holder request) that tries to create a Checkout
    session for an amount other than the $20/$100/$500 tiers.
    """
    user, _api_key, _plaintext = seed_user(session)
    fake = _install_fake_stripe()
    response = client.post(
        "/v1/internal/billing/create_checkout_session",
        headers=_internal_headers(),
        json={"user_id": user.id, "amount_cents": 7777},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "amount_not_in_bundle_set"
    assert sorted(body["allowed_cents"]) == [
        TOPUP_BUNDLE_20_CENTS_PAID,
        TOPUP_BUNDLE_100_CENTS_PAID,
        TOPUP_BUNDLE_500_CENTS_PAID,
    ]
    assert fake.last_amount_cents is None


def test_create_checkout_session_returns_404_when_user_missing(
    client: TestClient, session: Session, enable_billing_flag: None
) -> None:
    """A user_id that does not exist returns 404 without touching Stripe."""
    # No user seeded.
    fake = _install_fake_stripe()
    response = client.post(
        "/v1/internal/billing/create_checkout_session",
        headers=_internal_headers(),
        json={"user_id": 999_999, "amount_cents": TOPUP_BUNDLE_20_CENTS_PAID},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "user_not_found"
    assert fake.last_amount_cents is None


# --- Logging tests (Phase A2) ------------------------------------------------
#
# Every non-success path in billing.py now emits a structured log line so
# operators can monitor first-transaction behavior via journald. The tests
# below use ``caplog`` to assert those lines appear without leaking secrets.
#
# The success path logs ``internal_checkout_created`` with the session id
# masked to ``<first-12-chars>***``; the raw id must NOT appear anywhere.
#
# Convention: the logger name is ``app.routes.billing``; caplog is scoped
# to that logger at the appropriate level to avoid noise from other modules.


_BILLING_LOGGER = "app.routes.billing"


def test_success_path_logs_masked_session_id(
    client: TestClient, session: Session, enable_billing_flag: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Success path emits info log with masked session id and no raw secret.

    The ``_mask_session_id`` helper truncates to first 12 chars + ``***``.
    The raw id (``cs_live_fake_billing_123``, 24 chars) must NOT appear.
    The internal-auth secret value must NOT appear.
    """
    user, _api_key, _plaintext = seed_user(session)
    fake = _install_fake_stripe()
    with caplog.at_level(logging.INFO, logger=_BILLING_LOGGER):
        response = client.post(
            "/v1/internal/billing/create_checkout_session",
            headers=_internal_headers(),
            json={"user_id": user.id, "amount_cents": TOPUP_BUNDLE_20_CENTS_PAID},
        )
    assert response.status_code == 200
    log_text = " ".join(r.message for r in caplog.records)
    assert "internal_checkout_created" in log_text, f"expected event in log: {log_text}"
    # First 12 chars of "cs_live_fake_billing_123" is "cs_live_fake".
    expected_masked = f"{fake.session_id[:12]}***"
    assert expected_masked in log_text, (
        f"expected masked session id {expected_masked!r} in log: {log_text}"
    )
    # Raw session id must not appear in any log record.
    assert fake.session_id not in log_text, (
        f"raw session id {fake.session_id!r} must not appear in log: {log_text}"
    )
    # Internal auth secret must not appear in any log record.
    assert INTERNAL_SECRET not in log_text, (
        f"internal auth secret must not appear in log: {log_text}"
    )


def test_internal_auth_invalid_logs_warning(
    client: TestClient, session: Session, enable_billing_flag: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Wrong ``X-Internal-Auth`` header emits a WARNING without the header value."""
    user, _api_key, _plaintext = seed_user(session)
    _install_fake_stripe()
    wrong_secret = "not-the-real-secret-xxxxx"
    with caplog.at_level(logging.WARNING, logger=_BILLING_LOGGER):
        response = client.post(
            "/v1/internal/billing/create_checkout_session",
            headers={"X-Internal-Auth": wrong_secret},
            json={"user_id": user.id, "amount_cents": TOPUP_BUNDLE_20_CENTS_PAID},
        )
    assert response.status_code == 401
    log_text = " ".join(r.message for r in caplog.records)
    assert "internal_auth_invalid" in log_text, f"expected log line: {log_text}"
    # Neither the supplied wrong secret nor the configured secret should appear.
    assert wrong_secret not in log_text, f"header value must not appear in log: {log_text}"
    assert INTERNAL_SECRET not in log_text, f"configured secret must not appear in log: {log_text}"


def test_amount_not_in_bundle_set_logs_warning_with_amount(
    client: TestClient, session: Session, enable_billing_flag: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Off-bundle amount emits a WARNING carrying the attempted amount for forensics."""
    user, _api_key, _plaintext = seed_user(session)
    _install_fake_stripe()
    bad_amount = 9999
    with caplog.at_level(logging.WARNING, logger=_BILLING_LOGGER):
        response = client.post(
            "/v1/internal/billing/create_checkout_session",
            headers=_internal_headers(),
            json={"user_id": user.id, "amount_cents": bad_amount},
        )
    assert response.status_code == 400
    log_text = " ".join(r.message for r in caplog.records)
    assert "amount_not_in_bundle_set" in log_text, f"expected log line: {log_text}"
    assert str(bad_amount) in log_text, f"expected amount {bad_amount} in log: {log_text}"


def test_user_not_found_logs_warning_with_user_id(
    client: TestClient, session: Session, enable_billing_flag: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing user emits a WARNING carrying the attempted user_id for reconciliation."""
    _install_fake_stripe()
    missing_user_id = 999_888
    with caplog.at_level(logging.WARNING, logger=_BILLING_LOGGER):
        response = client.post(
            "/v1/internal/billing/create_checkout_session",
            headers=_internal_headers(),
            json={"user_id": missing_user_id, "amount_cents": TOPUP_BUNDLE_20_CENTS_PAID},
        )
    assert response.status_code == 404
    log_text = " ".join(r.message for r in caplog.records)
    assert "user_not_found" in log_text, f"expected log line: {log_text}"
    assert str(missing_user_id) in log_text, f"expected user_id {missing_user_id} in log: {log_text}"


def test_stripe_customer_missing_logs_warning(
    client: TestClient, session: Session, enable_billing_flag: None, caplog: pytest.LogCaptureFixture
) -> None:
    """User without a stripe_customer_id emits a WARNING with user_id for reconciliation."""
    user, _api_key, _plaintext = seed_user(session)
    # Null out stripe_customer_id after seeding.
    user.stripe_customer_id = None  # type: ignore[assignment]
    session.add(user)
    session.commit()
    _install_fake_stripe()
    with caplog.at_level(logging.WARNING, logger=_BILLING_LOGGER):
        response = client.post(
            "/v1/internal/billing/create_checkout_session",
            headers=_internal_headers(),
            json={"user_id": user.id, "amount_cents": TOPUP_BUNDLE_20_CENTS_PAID},
        )
    assert response.status_code == 409
    assert response.json()["error"] == "stripe_customer_missing"
    log_text = " ".join(r.message for r in caplog.records)
    assert "stripe_customer_missing" in log_text, f"expected log line: {log_text}"
    assert str(user.id) in log_text, f"expected user_id in log: {log_text}"


@dataclass
class _FakeMismatchStripe(_FakeStripeService):
    """Stripe fake that raises StripeCustomerModeError unconditionally.

    Used to exercise the ``customer_mode_mismatch`` path in billing.py without
    needing a real live/test customer id pair.
    """

    def create_checkout_session(
        self, user_id: int, stripe_customer_id: str, amount_cents: int
    ) -> "CheckoutSessionResult":  # type: ignore[override]
        from app.payments import StripeCustomerModeError
        raise StripeCustomerModeError("test: injected mode mismatch")


def test_customer_mode_mismatch_logs_error(
    client: TestClient, session: Session, enable_billing_flag: None, caplog: pytest.LogCaptureFixture
) -> None:
    """StripeCustomerModeError emits an ERROR log so the operator alert fires."""
    user, _api_key, _plaintext = seed_user(session)
    mismatch_fake = _FakeMismatchStripe()
    app.dependency_overrides[get_stripe_service] = _bind_fake(mismatch_fake)
    with caplog.at_level(logging.ERROR, logger=_BILLING_LOGGER):
        response = client.post(
            "/v1/internal/billing/create_checkout_session",
            headers=_internal_headers(),
            json={"user_id": user.id, "amount_cents": TOPUP_BUNDLE_20_CENTS_PAID},
        )
    assert response.status_code == 409
    assert response.json()["error"] == "customer_mode_mismatch"
    log_text = " ".join(r.message for r in caplog.records)
    assert "customer_mode_mismatch" in log_text, f"expected log line: {log_text}"
    assert str(user.id) in log_text, f"expected user_id in log: {log_text}"
