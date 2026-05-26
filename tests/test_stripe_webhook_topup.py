"""Tests for Stripe Checkout top-up ledger application."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.email import get_email_sender_factory
from app.main import app
from app.models import CreditLedger, TopupSession
from tests.conftest import seed_user

WEBHOOK_SECRET = "whsec_resemblio_dummy"


class FakeEmailSender:
    """Email fake that stores sent top-up notices."""

    def __init__(self) -> None:
        """Create an empty sent-message list."""
        self.sent: list[dict[str, int | str]] = []

    def send_topup_cleared(self, to_email: str, amount_cents: int, balance_cents: int) -> None:
        """Record one top-up notification."""
        self.sent.append({"to": to_email, "amount_cents": amount_cents, "balance_cents": balance_cents})


def test_checkout_topup_appends_ledger_and_sends_email(client: TestClient, session: Session) -> None:
    """A credit_topup Checkout event creates the expected ledger row and email."""
    user, _api_key, _plaintext = seed_user(session)
    session.add(TopupSession(id="cs_test_topup", user_id=user.id, amount_cents=2000, status="pending"))
    session.commit()
    fake_email = FakeEmailSender()
    app.dependency_overrides[get_email_sender_factory] = lambda: lambda: fake_email
    payload = _payload("evt_topup", user.id, 2000)
    response = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": _signature_header(payload)})
    assert response.status_code == 200
    topup = session.query(CreditLedger).filter(CreditLedger.entry_type == "topup").one()
    assert topup.user_id == user.id
    assert topup.amount_cents == 2000
    assert topup.balance_after_cents == 3000
    assert topup.stripe_payment_intent_id == "pi_test_topup"
    assert topup.note == "Stripe Checkout topup"
    assert fake_email.sent == [{"to": "frank@optsus.com", "amount_cents": 2000, "balance_cents": 3000}]


def test_non_topup_stripe_event_is_recorded_without_ledger(client: TestClient, session: Session) -> None:
    """Handled but non-topup event types are acknowledged without credit changes."""
    _user, _api_key, _plaintext = seed_user(session)
    fake_email = FakeEmailSender()
    app.dependency_overrides[get_email_sender_factory] = lambda: lambda: fake_email
    payload = json.dumps(
        {
            "id": "evt_payment_intent",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_test_topup"}},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    response = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": _signature_header(payload)})
    assert response.status_code == 202
    assert session.query(CreditLedger).filter(CreditLedger.entry_type == "topup").count() == 0
    assert fake_email.sent == []


def _payload(event_id: str, user_id: int, amount_cents: int) -> bytes:
    """Build a minimal top-up Checkout event."""
    return json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_topup",
                    "amount_total": amount_cents,
                    "payment_status": "paid",
                    "payment_intent": "pi_test_topup",
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
