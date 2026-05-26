"""S2 acceptance flow with fake Stripe and Resend."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import DEFAULT_API_SCOPE
from app.crypto import generate_api_key
from app.email import get_email_sender_factory
from app.main import app
from app.models import ApiKey, TopupSession
from app.payments import CheckoutSessionResult
from app.routes.account import credit_balance
from app.routes.credit import get_stripe_service
from app.users import create_user_with_customer_and_grant
from tests.conftest import auth_headers

WEBHOOK_SECRET = "whsec_resemblio_dummy"


class FakeStripeService:
    """Stripe fake for customer and Checkout creation."""

    def __init__(self) -> None:
        """Create an empty fake Stripe service."""
        self.checkout_amount_cents: int | None = None

    def create_customer(self, email: str) -> str:
        """Return a deterministic customer id."""
        return f"cus_test_{email.split('@', 1)[0]}"

    def create_checkout_session(self, user_id: int, stripe_customer_id: str, amount_cents: int) -> CheckoutSessionResult:
        """Capture amount and return a fake Checkout session."""
        self.checkout_amount_cents = amount_cents
        return CheckoutSessionResult(id="cs_test_acceptance", url="https://checkout.stripe.test/cs_test_acceptance")


class FakeEmailSender:
    """Email fake for acceptance assertions."""

    def __init__(self) -> None:
        """Create an empty sent-message list."""
        self.sent: list[tuple[str, int, int]] = []

    def send_topup_cleared(self, to_email: str, amount_cents: int, balance_cents: int) -> None:
        """Record the top-up cleared notice."""
        self.sent.append((to_email, amount_cents, balance_cents))


def test_s2_credit_stripe_acceptance_flow(client: TestClient, session: Session) -> None:
    """Signup grant, extraction charge, Checkout top-up, and webhook credit work together."""
    fake_stripe = FakeStripeService()
    fake_email = FakeEmailSender()
    user = create_user_with_customer_and_grant(session, "frank@optsus.com", "password", fake_stripe)
    plaintext, digest, prefix = generate_api_key("live")
    api_key = ApiKey(user_id=user.id, key_hash=digest, key_prefix=prefix, label="acceptance", scopes=[DEFAULT_API_SCOPE])
    session.add(api_key)
    session.commit()
    assert user.stripe_customer_id == "cus_test_frank"
    assert credit_balance(session, user.id) == 1000

    extraction = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://posthog.com"})
    assert extraction.status_code == 200
    assert credit_balance(session, user.id) == 500

    app.dependency_overrides[get_stripe_service] = lambda: fake_stripe
    checkout = client.post("/v1/credit/topup", headers=auth_headers(plaintext), json={"amount_cents": 2000})
    assert checkout.status_code == 200
    assert checkout.json()["checkout_url"] == "https://checkout.stripe.test/cs_test_acceptance"
    assert fake_stripe.checkout_amount_cents == 2000

    app.dependency_overrides[get_email_sender_factory] = lambda: lambda: fake_email
    # The dashboard POST /v1/credit/topup above already inserted a TopupSession
    # with id="cs_test_acceptance" (FakeStripeService returns that id); the
    # webhook will look it up via TopupSession ownership check.
    payload = _checkout_payload("evt_acceptance", user.id, 2000)
    webhook = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": _signature_header(payload)})
    assert webhook.status_code == 200
    assert credit_balance(session, user.id) == 2500
    assert fake_email.sent == [("frank@optsus.com", 2000, 2500)]


def _checkout_payload(event_id: str, user_id: int, amount_cents: int) -> bytes:
    """Build the simulated Stripe Checkout event."""
    return json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_acceptance",
                    "amount_total": amount_cents,
                    "payment_status": "paid",
                    "payment_intent": "pi_test_acceptance",
                    "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _signature_header(payload: bytes) -> str:
    """Return a Stripe-compatible signature header."""
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"
