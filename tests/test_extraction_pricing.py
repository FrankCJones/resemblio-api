"""Tests for public and private extraction pricing."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import EXTRACTION_PRIVATE_CENTS, EXTRACTION_PUBLIC_CENTS
from app.models import CreditLedger, Extraction
from app.routes.extractions import extraction_price_cents
from tests.conftest import auth_headers, seed_user


def test_extraction_price_helper_public_and_private() -> None:
    """Pricing helper returns canonical public and private cents."""
    assert extraction_price_cents(False) == EXTRACTION_PUBLIC_CENTS
    assert extraction_price_cents(True) == EXTRACTION_PRIVATE_CENTS


def test_private_request_charges_private_rate(client: TestClient, session: Session) -> None:
    """The optional private flag switches the ledger charge to 1000 cents."""
    _user, _api_key, plaintext = seed_user(session, balance=2000)
    public_response = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    private_response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.org", "private": True},
    )
    assert public_response.status_code == 200
    assert private_response.status_code == 200
    charges = session.query(CreditLedger).filter(CreditLedger.entry_type == "extraction_charge").order_by(CreditLedger.id).all()
    assert [charge.amount_cents for charge in charges] == [-500, -1000]
    extractions = session.query(Extraction).order_by(Extraction.id).all()
    assert [extraction.credit_cents for extraction in extractions] == [500, 1000]
