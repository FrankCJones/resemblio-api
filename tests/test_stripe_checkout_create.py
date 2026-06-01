"""Tests for Stripe Checkout top-up creation."""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.main import app
from app.payments import (
    CheckoutSessionResult,
    StripeClient,
    StripeCustomerModeError,
    _looks_like_no_such_customer,
)
from app.routes.credit import get_stripe_service
from tests.conftest import auth_headers, seed_user


@dataclass
class FakeStripeService:
    """Stripe fake that records Checkout session inputs."""

    user_id: int | None = None
    customer_id: str | None = None
    amount_cents: int | None = None

    def create_customer(self, email: str) -> str:
        """Return a deterministic fake customer id."""
        return f"cus_{email.replace('@', '_')}"

    def create_checkout_session(self, user_id: int, stripe_customer_id: str, amount_cents: int) -> CheckoutSessionResult:
        """Capture checkout payload fields and return a fake session."""
        self.user_id = user_id
        self.customer_id = stripe_customer_id
        self.amount_cents = amount_cents
        return CheckoutSessionResult(id="cs_test_123", url="https://checkout.stripe.test/session/cs_test_123")


def test_topup_endpoint_creates_checkout_session(client: TestClient, session: Session) -> None:
    """The top-up endpoint asks Stripe for a payment session with user metadata."""
    user, _api_key, plaintext = seed_user(session)
    fake = FakeStripeService()
    app.dependency_overrides[get_stripe_service] = lambda: fake
    response = client.post("/v1/credit/topup", headers=auth_headers(plaintext), json={"amount_cents": 2000})
    assert response.status_code == 200
    assert response.json() == {
        "checkout_session_id": "cs_test_123",
        "checkout_url": "https://checkout.stripe.test/session/cs_test_123",
        "schema_version": 1,
    }
    assert fake.user_id == user.id
    assert fake.customer_id == "cus_test_seed"
    assert fake.amount_cents == 2000


def test_topup_endpoint_enforces_minimum(client: TestClient, session: Session) -> None:
    """Top-ups below 2000 cents are rejected before Checkout creation."""
    _user, _api_key, plaintext = seed_user(session)
    fake = FakeStripeService()
    app.dependency_overrides[get_stripe_service] = lambda: fake
    response = client.post("/v1/credit/topup", headers=auth_headers(plaintext), json={"amount_cents": 1999})
    assert response.status_code == 400
    assert response.json()["error"] == "topup_minimum_not_met"
    assert fake.amount_cents is None


# ---- Defensive check: cross-mode customer id handling --------------------


class _FakeStripeApiError(Exception):
    """Stand-in for ``stripe.error.InvalidRequestError`` in unit tests.

    The real SDK exception is imported lazily by ``app.payments``; tests
    construct a plain ``Exception`` whose ``str()`` carries the Stripe
    "No such customer" message that triggers the typed re-raise.
    """


def test_looks_like_no_such_customer_matches_stripe_phrasing() -> None:
    """The defensive matcher catches Stripe's canonical 'No such customer' error."""
    exc = _FakeStripeApiError("No such customer: 'cus_TestModeOnlyAbc'")
    assert _looks_like_no_such_customer(exc) is True


def test_looks_like_no_such_customer_is_case_insensitive() -> None:
    """Stripe varies capitalisation across SDK versions; matcher tolerates both."""
    assert _looks_like_no_such_customer(Exception("no such customer: cus_x")) is True
    assert _looks_like_no_such_customer(Exception("NO SUCH CUSTOMER: cus_x")) is True


def test_looks_like_no_such_customer_does_not_match_other_errors() -> None:
    """Unrelated Stripe errors must NOT be re-raised as customer-mode errors."""
    assert _looks_like_no_such_customer(Exception("Rate limit exceeded")) is False
    assert _looks_like_no_such_customer(Exception("Invalid API key")) is False
    assert _looks_like_no_such_customer(Exception("")) is False


def test_create_checkout_session_raises_stripe_customer_mode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 'No such customer' error from Stripe surfaces as ``StripeCustomerModeError``.

    Wires a fake Stripe SDK module into ``app.payments`` whose
    ``checkout.Session.create`` raises with the canonical Stripe phrasing.
    The wrapper must re-raise as the typed exception so the route handler
    can return a 409 with a clear error code instead of an opaque 500.
    """

    class _FakeCheckoutSession:
        @staticmethod
        def create(**_kwargs: object) -> None:
            raise _FakeStripeApiError("No such customer: 'cus_TestModeOnlyAbc'")

    class _FakeCheckout:
        Session = _FakeCheckoutSession

    class _FakeStripeModule:
        api_key: str | None = None
        checkout = _FakeCheckout()

    import app.payments as payments_module

    monkeypatch.setattr(payments_module, "_stripe_module", lambda: _FakeStripeModule())
    # Collapse retry sleeps so the test does not waste real seconds.
    monkeypatch.setattr(payments_module, "STRIPE_RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0))

    settings = Settings(
        RESEMBLIO_KEY_PEPPER="x" * 32,
        STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST="rk_test_synth",
    )
    client = StripeClient(settings)
    with pytest.raises(StripeCustomerModeError) as excinfo:
        client.create_checkout_session(user_id=1, stripe_customer_id="cus_TestModeOnlyAbc", amount_cents=2000)
    assert "cus_TestModeOnlyAbc" in str(excinfo.value)
    assert "reconcile" in str(excinfo.value).lower()


def test_create_checkout_session_preserves_runtime_error_for_other_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-customer-mode Stripe failures still raise the generic RuntimeError."""

    class _FakeCheckoutSession:
        @staticmethod
        def create(**_kwargs: object) -> None:
            raise _FakeStripeApiError("Rate limit exceeded")

    class _FakeCheckout:
        Session = _FakeCheckoutSession

    class _FakeStripeModule:
        api_key: str | None = None
        checkout = _FakeCheckout()

    import app.payments as payments_module

    monkeypatch.setattr(payments_module, "_stripe_module", lambda: _FakeStripeModule())
    monkeypatch.setattr(payments_module, "STRIPE_RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0))

    settings = Settings(
        RESEMBLIO_KEY_PEPPER="x" * 32,
        STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST="rk_test_synth",
    )
    client = StripeClient(settings)
    with pytest.raises(RuntimeError) as excinfo:
        client.create_checkout_session(user_id=1, stripe_customer_id="cus_AnyId12345abc", amount_cents=2000)
    assert "Checkout session creation failed" in str(excinfo.value)
    assert not isinstance(excinfo.value, StripeCustomerModeError)
