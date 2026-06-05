"""Tests for extraction creation and retrieval routes."""
from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.extractor_bridge import ExtractionBridgeError, ExtractionBundle, bundle_from_token_set
from app.models import CreditLedger, Extraction
from app.routes.extractions import get_extractor
from tests.conftest import TOKEN_SET, FakeStorage, auth_headers, seed_user


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


def test_list_extractions_advertises_schema_v1_1_on_wrapper_and_items(
    client: TestClient,
    session: Session,
) -> None:
    """List endpoint response-contract version is SCHEMA_V1_1 on wrapper + each item.

    Regression guard for the 2026-06-01 list-endpoint shape fix: clients that
    switch on `schema_version` for the list response shape must see v1.1 (=2)
    on both the wrapper and every per-item row, matching the detail endpoint's
    response-contract version. The row's extractor `schema_version` column is
    a separate concern and is not surfaced on list items.
    """
    _, _, plaintext = seed_user(session)
    client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com/a"})
    client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com/b"})
    listed = client.get("/v1/extractions", headers=auth_headers(plaintext)).json()
    assert listed["schema_version"] == 2
    assert len(listed["items"]) == 2
    for item in listed["items"]:
        assert item["schema_version"] == 2
        # List items stay narrow: full envelope is detail-only.
        assert set(item.keys()) == {"id", "url", "status", "extracted_at", "schema_version"}


def test_detail_endpoint_still_returns_full_envelope_with_schema_v1_1(
    client: TestClient,
    session: Session,
) -> None:
    """Regression guard: detail endpoint shape (schema_version=2 + manifest + tokens_url) unchanged."""
    _, _, plaintext = seed_user(session)
    created = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    assert created.status_code == 200
    extraction = session.query(Extraction).one()
    fetched = client.get(f"/v1/extractions/{extraction.id}", headers=auth_headers(plaintext))
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["schema_version"] == 2
    assert body["tokens_url"] is not None
    assert isinstance(body["manifest"], dict)
    assert body["manifest"]["schema_version"] == 2
    assert body["download_url"] is not None


def test_post_extraction_surfaces_palette_completeness_warning_when_extractor_flags_one(
    client: TestClient,
    session: Session,
) -> None:
    """A1.1 Part 2: a bundle carrying a warning surfaces it on the response envelope.

    Locks the additive ``palette_completeness_warning`` contract:
    - Non-empty list passes through verbatim onto the JSON response.
    - The field rides ALONGSIDE the existing schema_version=2 v1.1
      envelope (manifest, tokens_url, download_url); none of them
      collapse when the warning fires.
    """
    _, _, plaintext = seed_user(session)
    warning = ["#f8485e", "#592a8a"]

    def _extract_with_warning(url: str) -> ExtractionBundle:
        return bundle_from_token_set(
            url, TOKEN_SET, palette_completeness_warning=warning
        )

    from app.main import app

    app.dependency_overrides[get_extractor] = lambda: _extract_with_warning
    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://encexplorer.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["palette_completeness_warning"] == warning
    # Additive: the v1.1 envelope is unchanged.
    assert body["schema_version"] == 2
    assert body["manifest"] is not None


def test_post_extraction_omits_palette_warning_when_extractor_returns_none(
    client: TestClient,
    session: Session,
) -> None:
    """The warning is null when the extractor passes None (palette complete or pass unavailable)."""
    _, _, plaintext = seed_user(session)
    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    # `fake_extractor` from conftest does not pass a warning; expect null.
    assert body["palette_completeness_warning"] is None


def test_get_cached_extraction_returns_null_palette_warning(
    client: TestClient,
    session: Session,
) -> None:
    """Cached GET reads carry palette_completeness_warning=null by contract.

    The warning is recomputed per extraction and not persisted on the
    extraction row, so any GET against a historic row returns null.
    Callers needing the signal must re-run extraction.
    """
    _, _, plaintext = seed_user(session)
    created = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    assert created.status_code == 200
    extraction = session.query(Extraction).one()
    fetched = client.get(
        f"/v1/extractions/{extraction.id}", headers=auth_headers(plaintext)
    )
    assert fetched.status_code == 200
    assert fetched.json()["palette_completeness_warning"] is None


def _balance(session: Session) -> int:
    """Return the current seeded user's balance."""
    return sum(entry.amount_cents for entry in session.query(CreditLedger).all())

