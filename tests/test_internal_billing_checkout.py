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
    app.dependency_overrides[get_stripe_service] = lambda: fake
    return fake


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
        app.dependency_overrides[get_stripe_service] = lambda f=fake: f
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
