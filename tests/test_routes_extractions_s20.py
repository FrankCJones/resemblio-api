"""S20 integration tests: route handler + refund + idempotency.

Synthetic fixtures; no network. Exercises the wire-up between
`POST /v1/extractions`, `app.quality_scoring`, and the refund helper.
"""
from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.extractor_bridge import ExtractionBundle, bundle_from_token_set
from app.failure_modes import REFUNDABLE_CODES, FailureCode
from app.models import CreditLedger, Extraction
from app.routes.extractions import _refund, get_extractor
from tests.conftest import auth_headers, seed_user


# ----------------------------------------------------------------------
# Tokens that score low (grayscale, no type scale, single spacing)
# ----------------------------------------------------------------------


_LOW_QUALITY_TOKENS: dict[str, str] = {
    "bg": "#ffffff",
    "text": "#111111",
    # No accent, no border, no text_muted -> palette_role_coverage = 2/5
    # No vivid color -> chroma_diversity = 0.0
    # Single font, no weights -> type_pairing = 0.0
    "font_display": "Inter, sans-serif",
    "font_body": "Inter, sans-serif",
    # No text_* sizes -> type_scale = 0.0
    # One spacing only -> spacing_scale = 1/5 = 0.2
    "space_1": "4px",
    # token diversity will be 4/4 = 1.0 (all values unique) but the other
    # dimensions sink the composite well below 0.55.
}


_HIGH_QUALITY_TOKENS: dict[str, str] = {
    "bg": "#ffffff",
    "text": "#111111",
    "accent": "#ff0000",
    "text_muted": "#555555",
    "border": "#dddddd",
    "font_display": "Playfair, serif",
    "font_body": "Inter, sans-serif",
    "text_sm": "14px",
    "text_base": "16px",
    "text_lg": "18px",
    "text_xl": "24px",
    "space_1": "4px",
    "space_2": "8px",
    "space_3": "12px",
    "space_4": "16px",
    "space_5": "24px",
}


def _balance(session: Session) -> int:
    """Return the user's credit balance from the ledger."""
    rows = session.query(CreditLedger).all()
    if not rows:
        return 0
    return sum(int(row.amount_cents) for row in rows)


# ----------------------------------------------------------------------
# Route integration
# ----------------------------------------------------------------------


def test_low_quality_response_status_and_refund(
    client: TestClient,
    session: Session,
) -> None:
    """A low-scoring extraction returns status=low_quality, refunds, and flags review."""
    _, _, plaintext = seed_user(session)

    def low_quality_extractor(url: str) -> ExtractionBundle:
        return bundle_from_token_set(url, _LOW_QUALITY_TOKENS)

    from app.main import app
    app.dependency_overrides[get_extractor] = lambda: low_quality_extractor

    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "low_quality"
    assert body["error_code"] == "low_quality_output"
    assert body["refunded"] is True
    assert body["quality_score"] is not None
    assert body["quality_score"] < 0.55

    extraction = session.query(Extraction).one()
    assert extraction.status == "low_quality"
    assert extraction.low_quality_review_pending is True
    assert extraction.quality_score is not None

    # Ledger: onboarding_grant + extraction_charge + refund == balance 1000
    entries = [entry.entry_type for entry in session.query(CreditLedger).order_by(CreditLedger.id).all()]
    assert entries == ["onboarding_grant", "extraction_charge", "refund"]
    assert _balance(session) == 1000


def test_high_quality_response_status_ok(
    client: TestClient,
    session: Session,
) -> None:
    """A high-scoring extraction returns status=ok with no refund."""
    _, _, plaintext = seed_user(session)

    def high_quality_extractor(url: str) -> ExtractionBundle:
        return bundle_from_token_set(url, _HIGH_QUALITY_TOKENS)

    from app.main import app
    app.dependency_overrides[get_extractor] = lambda: high_quality_extractor

    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body.get("refunded") in (None, False)
    extraction = session.query(Extraction).one()
    assert extraction.status == "ok"
    assert extraction.low_quality_review_pending is False
    # Score was still computed and persisted, even though it passed threshold
    assert extraction.quality_score is not None
    assert extraction.quality_score >= 0.55
    # Customer was charged once with no refund
    assert _balance(session) == 500


def test_low_quality_response_includes_dimension_scores(
    client: TestClient,
    session: Session,
) -> None:
    """The response body surfaces the per-dimension score breakdown."""
    _, _, plaintext = seed_user(session)

    def low_quality_extractor(url: str) -> ExtractionBundle:
        return bundle_from_token_set(url, _LOW_QUALITY_TOKENS)

    from app.main import app
    app.dependency_overrides[get_extractor] = lambda: low_quality_extractor

    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    body = response.json()
    assert "quality_dimension_scores" in body
    dims = body["quality_dimension_scores"]
    assert "palette_role_coverage" in dims
    assert "color_chroma_diversity" in dims
    # The error_log carries the structured S20 contract
    error_log = body["error_log"]
    assert isinstance(error_log, dict)
    assert error_log["schema_version"].startswith("quality_score_v1@")
    assert "suggestion" in error_log
    assert error_log["suggestion"] != ""


# ----------------------------------------------------------------------
# Idempotency: refund helper short-circuits on duplicate
# ----------------------------------------------------------------------


def test_refund_helper_idempotent_on_same_extraction(
    session: Session,
) -> None:
    """A second `_refund` call on the same extraction does not double-credit."""
    user, api_key, _ = seed_user(session)
    extraction = Extraction(
        user_id=user.id,
        api_key_id=api_key.id,
        url="https://example.com",
        url_normalized="https://example.com",
        status="ok",
        schema_version=1,
        credit_cents=500,
    )
    session.add(extraction)
    session.commit()
    session.refresh(extraction)

    first = _refund(session, user.id, api_key.id, extraction.id, 500)
    session.commit()
    second = _refund(session, user.id, api_key.id, extraction.id, 500)
    session.commit()
    assert first is True
    assert second is False
    refunds = session.query(CreditLedger).filter(
        CreditLedger.extraction_id == extraction.id,
        CreditLedger.entry_type == "refund",
    ).all()
    assert len(refunds) == 1


# ----------------------------------------------------------------------
# Seed-row skip
# ----------------------------------------------------------------------


def test_low_quality_output_is_refundable_code() -> None:
    """`LOW_QUALITY_OUTPUT` is wired into the refundable set."""
    assert FailureCode.LOW_QUALITY_OUTPUT in REFUNDABLE_CODES


def test_low_quality_output_http_status_is_200() -> None:
    """Low-quality classification is HTTP 200 per ADR section 6."""
    from app.failure_modes import http_status_for
    assert http_status_for(FailureCode.LOW_QUALITY_OUTPUT) == 200


# ----------------------------------------------------------------------
# Heuristic-penalty wiring (dispatch 2026-05-31)
# ----------------------------------------------------------------------


# Tokens that score HIGH on the base composite (rich palette, full type
# scale, multiple spacing slots, two font families with type-pair signal)
# but use a 100% system-font stack. The system-font penalty alone is 0.30,
# enough to drop a ~0.7 raw score below the 0.55 threshold. Penalty colors
# are deliberately NOT the all-defaults palette (we want font penalty in
# isolation here, not double-stacked with color penalty).
_SYSTEM_FONT_HIGH_BASE_TOKENS: dict[str, str] = {
    "bg": "#fefae0",
    "text": "#283618",
    "accent": "#bc6c25",
    "text_muted": "#606c38",
    "border": "#dda15e",
    # Both fonts in the system-font set -> SYSTEM_FONT_STACK_PENALTY fires.
    "font_display": "Georgia, serif",
    "font_body": "system-ui, sans-serif",
    "text_sm": "14px",
    "text_base": "16px",
    "text_lg": "18px",
    "text_xl": "24px",
    "space_1": "4px",
    "space_2": "8px",
    "space_3": "12px",
    "space_4": "16px",
    "space_5": "24px",
}


# Tokens whose raw composite clears the threshold and whose penalized
# composite ALSO clears the threshold (no heuristic fires). Brand fonts +
# brand-color palette + full scales. Used to assert "no penalty -> no
# refund" so the wiring does not accidentally over-trigger.
_HIGH_QUALITY_NO_PENALTY_TOKENS: dict[str, str] = {
    "bg": "#fefae0",
    "text": "#283618",
    "accent": "#bc6c25",
    "text_muted": "#606c38",
    "border": "#dda15e",
    "font_display": "Playfair Display, serif",
    "font_body": "Inter, sans-serif",
    "text_sm": "14px",
    "text_base": "16px",
    "text_lg": "18px",
    "text_xl": "24px",
    "space_1": "4px",
    "space_2": "8px",
    "space_3": "12px",
    "space_4": "16px",
    "space_5": "24px",
}


def test_extractions_persists_penalized_score(
    client: TestClient,
    session: Session,
) -> None:
    """Penalized score < raw score is persisted as `quality_score` with raw retained."""
    _, _, plaintext = seed_user(session)

    def system_font_extractor(url: str) -> ExtractionBundle:
        return bundle_from_token_set(url, _SYSTEM_FONT_HIGH_BASE_TOKENS)

    from app.main import app
    app.dependency_overrides[get_extractor] = lambda: system_font_extractor

    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    # The customer-facing quality_score is the PENALIZED value.
    assert body["raw_quality_score"] is not None
    assert body["quality_score"] is not None
    assert body["quality_score"] < body["raw_quality_score"]
    # Components surface the penalty that fired.
    components = body["quality_score_components"]
    assert components is not None
    assert "all_system_font_stack" in components["penalties_applied"]
    assert components["raw"] == body["raw_quality_score"]
    assert components["penalized"] == body["quality_score"]

    # Persisted row mirrors the response.
    extraction = session.query(Extraction).one()
    assert extraction.raw_quality_score is not None
    assert extraction.quality_score is not None
    assert extraction.quality_score < extraction.raw_quality_score


_BOTH_PENALTIES_HIGH_BASE_TOKENS: dict[str, str] = {
    # All three brand-signal color slots (bg/text/accent) in the default set
    # -> COMMON_DEFAULT_COLORS_PENALTY fires (0.30).
    "bg": "#ffffff",
    "text": "#1a1a1a",
    "accent": "#4f46e5",
    # Non-brand-signal slots stay brand-specific so the base composite still
    # scores high on palette role coverage.
    "text_muted": "#606c38",
    "border": "#dda15e",
    # Both fonts in the system-font set -> SYSTEM_FONT_STACK_PENALTY fires (0.30).
    "font_display": "Georgia, serif",
    "font_body": "system-ui, sans-serif",
    "text_sm": "14px",
    "text_base": "16px",
    "text_lg": "18px",
    "text_xl": "24px",
    "space_1": "4px",
    "space_2": "8px",
    "space_3": "12px",
    "space_4": "16px",
    "space_5": "24px",
}


def test_extractions_triggers_refund_when_penalized_below_threshold(
    client: TestClient,
    session: Session,
) -> None:
    """An extraction whose raw clears threshold but penalized fails it auto-refunds."""
    _, _, plaintext = seed_user(session)

    def both_penalties_extractor(url: str) -> ExtractionBundle:
        return bundle_from_token_set(url, _BOTH_PENALTIES_HIGH_BASE_TOKENS)

    from app.main import app
    app.dependency_overrides[get_extractor] = lambda: both_penalties_extractor

    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    # Raw cleared the threshold; penalty knocked it below; status flipped.
    assert body["raw_quality_score"] >= 0.55
    assert body["quality_score"] < 0.55
    assert body["status"] == "low_quality"
    assert body["refunded"] is True

    extraction = session.query(Extraction).one()
    assert extraction.status == "low_quality"
    assert extraction.low_quality_review_pending is True

    # Ledger: onboarding_grant + extraction_charge + refund.
    entries = [
        entry.entry_type
        for entry in session.query(CreditLedger).order_by(CreditLedger.id).all()
    ]
    assert entries == ["onboarding_grant", "extraction_charge", "refund"]


def test_extractions_keeps_charge_when_only_raw_above_threshold(
    client: TestClient,
    session: Session,
) -> None:
    """No heuristic penalty fires -> status stays `ok` and no refund posts."""
    _, _, plaintext = seed_user(session)

    def brand_font_extractor(url: str) -> ExtractionBundle:
        return bundle_from_token_set(url, _HIGH_QUALITY_NO_PENALTY_TOKENS)

    from app.main import app
    app.dependency_overrides[get_extractor] = lambda: brand_font_extractor

    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body.get("refunded") in (None, False)
    # No penalty triggered; raw == penalized.
    assert body["raw_quality_score"] is not None
    assert body["quality_score"] == body["raw_quality_score"]
    components = body["quality_score_components"]
    assert components is not None
    assert components["penalties_applied"] == []

    # Customer was charged once with no refund.
    entries = [
        entry.entry_type
        for entry in session.query(CreditLedger).order_by(CreditLedger.id).all()
    ]
    assert entries == ["onboarding_grant", "extraction_charge"]
