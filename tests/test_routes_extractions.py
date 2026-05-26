"""Tests for extraction creation and retrieval routes."""
from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.extractor_bridge import ExtractionBridgeError, ExtractionBundle
from app.models import CreditLedger, Extraction
from app.routes.extractions import get_extractor
from tests.conftest import FakeStorage, auth_headers, seed_user


def test_create_get_and_list_extraction_without_recharge(
    client: TestClient,
    session: Session,
    fake_storage: FakeStorage,
) -> None:
    """POST charges once, stores a ZIP, and GET returns cached data for free."""
    _, _, plaintext = seed_user(session)
    created = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "ok"
    assert body["download_url"].startswith("https://r2.test/")
    extraction = session.query(Extraction).one()
    assert extraction.status == "ok"
    assert extraction.r2_zip_key in fake_storage.objects
    assert _balance(session) == 500
    fetched = client.get(f"/v1/extractions/{extraction.id}", headers=auth_headers(plaintext))
    assert fetched.status_code == 200
    assert _balance(session) == 500
    listed = client.get("/v1/extractions", headers=auth_headers(plaintext)).json()
    assert listed["items"][0]["id"] == extraction.id


def test_insufficient_credit_returns_402(client: TestClient, session: Session) -> None:
    """Extraction creation fails before calling the extractor when balance is low."""
    _, _, plaintext = seed_user(session, balance=0)
    response = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    assert response.status_code == 402
    assert response.json()["error"] == "insufficient_credit"


def test_extractor_failure_refunds_charge(
    client: TestClient,
    session: Session,
) -> None:
    """Extractor failure marks the row failed and appends a refund entry."""
    _, _, plaintext = seed_user(session)

    def fail(_url: str) -> ExtractionBundle:
        # "anthropic failed: ..." classifies to MODEL_ERROR per S15 ADR;
        # MODEL_ERROR is Resemblio-attributable so credit is refunded.
        raise ExtractionBridgeError("anthropic failed: timeout")

    from app.main import app

    app.dependency_overrides[get_extractor] = lambda: fail
    response = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    assert response.status_code == 502
    body = response.json()
    assert body["error"] == "extractor_failed"
    assert body["error_code"] == "model_error"
    assert body["schema_version"] == 1
    assert _balance(session) == 1000
    extraction = session.query(Extraction).one()
    assert extraction.status == "failed"
    entries = [entry.entry_type for entry in session.query(CreditLedger).order_by(CreditLedger.id).all()]
    assert entries == ["onboarding_grant", "extraction_charge", "refund"]


def test_extractor_failure_no_refund_for_user_attributable(
    client: TestClient,
    session: Session,
) -> None:
    """User-attributable failures (waf_blocked) consume credit; no refund per S15 ADR."""
    _, _, plaintext = seed_user(session)

    def fail(_url: str) -> ExtractionBundle:
        raise ExtractionBridgeError("fetch failed: status=403 ua=chrome")

    from app.main import app

    app.dependency_overrides[get_extractor] = lambda: fail
    response = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    assert response.status_code == 502
    body = response.json()
    assert body["error_code"] == "waf_blocked"
    # Charge stands; no refund entry.
    entries = [entry.entry_type for entry in session.query(CreditLedger).order_by(CreditLedger.id).all()]
    assert entries == ["onboarding_grant", "extraction_charge"]


def _balance(session: Session) -> int:
    """Return the current seeded user's balance."""
    return sum(entry.amount_cents for entry in session.query(CreditLedger).all())

