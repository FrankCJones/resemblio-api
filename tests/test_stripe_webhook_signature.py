"""Tests for Stripe webhook signature verification and replay handling."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.email import get_email_sender_factory
from app.main import app
from app.models import CreditLedger, StripeEventSeen, TopupSession
from tests.conftest import seed_user

WEBHOOK_SECRET = "whsec_resemblio_dummy"


class FakeEmailSender:
    """Email fake that records top-up messages."""

    def __init__(self) -> None:
        """Create an empty sent-message log."""
        self.sent: list[tuple[str, int, int]] = []

    def send_topup_cleared(self, to_email: str, amount_cents: int, balance_cents: int) -> None:
        """Record the top-up email arguments."""
        self.sent.append((to_email, amount_cents, balance_cents))


def test_valid_signature_processes_and_replay_is_noop(client: TestClient, session: Session) -> None:
    """A valid signed event credits once and duplicate delivery is skipped."""
    user, _api_key, _plaintext = seed_user(session)
    session.add(TopupSession(id="cs_test_123", user_id=user.id, amount_cents=2000, status="pending"))
    session.commit()
    fake_email = FakeEmailSender()
    app.dependency_overrides[get_email_sender_factory] = lambda: lambda: fake_email
    payload = _checkout_event_payload("evt_replay", user.id, 2000)
    signature = _signature_header(payload)
    first = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})
    second = client.post("/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})
    assert first.status_code == 200
    # Duplicate event delivery is now 200 with duplicate=true (was 202 pre-H1).
    # The insert-first idempotency pattern claims the event_id on first hit; a
    # redelivery hits the unique constraint, rolls back, and returns 200.
    assert second.status_code == 200
    assert second.json().get("duplicate") is True
    topups = session.query(CreditLedger).filter(CreditLedger.entry_type == "topup").all()
    assert len(topups) == 1
    assert topups[0].amount_cents == 2000
    assert session.query(StripeEventSeen).filter(StripeEventSeen.event_id == "evt_replay").count() == 1
    assert fake_email.sent == [("frank@optsus.com", 2000, 3000)]


def test_tampered_signature_returns_400(client: TestClient, session: Session) -> None:
    """A tampered body is rejected and no ledger entry is written."""
    user, _api_key, _plaintext = seed_user(session)
    fake_email = FakeEmailSender()
    app.dependency_overrides[get_email_sender_factory] = lambda: lambda: fake_email
    payload = _checkout_event_payload("evt_bad_sig", user.id, 2000)
    signature = _signature_header(payload)
    tampered = payload.replace(b"2000", b"3000")
    response = client.post("/v1/webhooks/stripe", content=tampered, headers={"Stripe-Signature": signature})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_signature"
    assert session.query(CreditLedger).filter(CreditLedger.entry_type == "topup").count() == 0
    assert fake_email.sent == []


def test_missing_signature_returns_400_without_email_dependency(client: TestClient) -> None:
    """Signature rejection happens before constructing the Resend sender."""
    app.dependency_overrides.pop(get_email_sender_factory, None)
    response = client.post("/v1/webhooks/stripe", content=b"{}")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_signature"


def _checkout_event_payload(event_id: str, user_id: int, amount_cents: int) -> bytes:
    """Build a minimal checkout.session.completed event payload."""
    return json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_123",
                    "amount_total": amount_cents,
                    "payment_status": "paid",
                    "payment_intent": "pi_test_123",
                    "metadata": {"user_id": str(user_id), "purpose": "credit_topup"},
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _signature_header(payload: bytes) -> str:
    """Return a Stripe-compatible test signature header."""
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"
