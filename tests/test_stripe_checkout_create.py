"""Tests for Stripe Checkout top-up creation."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.payments import CheckoutSessionResult
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
