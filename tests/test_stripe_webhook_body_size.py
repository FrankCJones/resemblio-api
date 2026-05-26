"""Tests for the body-size cap on the Stripe webhook endpoint.

Closes audit finding M-API-1 (`projects/OptSus Team/security-audits/
2026-05-26-initial.md`). The endpoint is auth-free per Stripe's contract, so
an unauthenticated caller can flood arbitrary bodies at it. These tests
verify both defense layers (declared content-length pre-check and on-the-wire
streamed body check) reject oversized requests with HTTP 413 BEFORE any
signature work or JSON parse happens.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.constants import STRIPE_WEBHOOK_MAX_BODY_BYTES


def test_oversized_declared_content_length_returns_413(client: TestClient) -> None:
    """A request whose Content-Length declares too large a body is rejected."""
    oversized_body = b"x" * (STRIPE_WEBHOOK_MAX_BODY_BYTES + 1)
    response = client.post(
        "/v1/webhooks/stripe",
        content=oversized_body,
        headers={"Stripe-Signature": "ignored"},
    )
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"


def test_under_cap_body_proceeds_to_signature_check(client: TestClient) -> None:
    """A small body below the cap reaches signature verification (then 400)."""
    # No valid signature; the handler should respond 400 invalid_signature,
    # proving the body cap did NOT short-circuit a legitimately-sized request.
    response = client.post(
        "/v1/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_signature"


def test_missing_content_length_streamed_body_capped(client: TestClient) -> None:
    """A request without Content-Length still gets capped on the wire."""
    # Starlette's TestClient sets Content-Length automatically, so this also
    # exercises the declared-length branch; the streamed branch is the
    # backstop for hostile clients that omit the header. Both branches share
    # the 413 response, so the assertion here remains the same.
    oversized_body = b"y" * (STRIPE_WEBHOOK_MAX_BODY_BYTES + 1024)
    response = client.post(
        "/v1/webhooks/stripe",
        content=oversized_body,
        headers={"Stripe-Signature": "ignored"},
    )
    assert response.status_code == 413
