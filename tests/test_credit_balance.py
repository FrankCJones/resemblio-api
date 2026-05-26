"""Tests for credit balance ledger behavior."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import CreditLedger
from app.routes.account import credit_balance
from tests.conftest import auth_headers, seed_user


def test_credit_balance_sums_signed_ledger_entries(session: Session) -> None:
    """Balance is derived from append-only signed ledger entries."""
    user, api_key, _plaintext = seed_user(session, balance=1000)
    session.add(
        CreditLedger(
            user_id=user.id,
            entry_type="extraction_charge",
            amount_cents=-500,
            balance_after_cents=500,
            api_key_id=api_key.id,
            note="Public extraction",
        )
    )
    session.add(
        CreditLedger(
            user_id=user.id,
            entry_type="refund",
            amount_cents=500,
            balance_after_cents=1000,
            api_key_id=api_key.id,
            note="Extraction failed",
        )
    )
    session.commit()
    assert credit_balance(session, user.id) == 1000


def test_charge_gate_prevents_negative_balance(client: TestClient, session: Session) -> None:
    """A request that lacks enough credit returns 402 before writing a charge."""
    _user, _api_key, plaintext = seed_user(session, balance=400)
    response = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    assert response.status_code == 402
    assert response.json() == {"error": "insufficient_credit", "balance_cents": 400, "required_cents": 500}
    entries = session.query(CreditLedger).all()
    assert [entry.amount_cents for entry in entries] == [400]
