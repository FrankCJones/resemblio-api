"""Tests for Stage O5 anonymous-to-account conversion (claim endpoint).

Exercises ``POST /v1/internal/auth/claim_anonymous_extraction`` end-to-
end through the same shared-secret middleware that gates the rest of
the internal BFF surface.

Synthetic-only; no network.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import utcnow
from app.config import reset_settings_cache
from app.constants import (
    ANON_EXTRACTION_CLAIM_WINDOW_HOURS,
    SCHEMA_V1,
)
from app.models import AnonymousExtraction, Extraction, User
from app.routes.internal_auth import CLAIM_ANONYMOUS_EXTRACTION_SCHEMA_VERSION


INTERNAL_SECRET = "test-internal-auth-secret-for-tests"
CLAIM_PATH = "/v1/internal/auth/claim_anonymous_extraction"


@pytest.fixture
def pinned_internal_secret(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Pin ``RESEMBLIO_INTERNAL_AUTH_SECRET`` for the duration of one test.

    Mirrors the ``fake_email_sender`` fixture from ``test_internal_auth.py``
    without the email-capture machinery (the claim endpoint does not
    send mail). The settings cache is reset to pick up the new value.
    """
    monkeypatch.setenv("RESEMBLIO_INTERNAL_AUTH_SECRET", INTERNAL_SECRET)
    reset_settings_cache()
    yield
    reset_settings_cache()


def _headers() -> dict[str, str]:
    """Return the shared-secret header the route expects."""
    return {"X-Internal-Auth": INTERNAL_SECRET}


def _seed_user(session: Session, email: str = "claimant@example.com") -> User:
    """Insert a minimal active user and return it.

    The user is the conversion target; the claim endpoint rebinds the
    anonymous extraction's ``user_id`` to this row.
    """
    user = User(email=email, password_hash="argon2id$dummy$hash", status="active")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _seed_anonymous_extraction(
    session: Session,
    *,
    claim_token: str = "test-claim-token-xyz-32-byte-equivalent",
    classification: str = "html_first",
    expired: bool = False,
    no_extraction_row: bool = False,
) -> tuple[AnonymousExtraction, Extraction | None]:
    """Insert an anonymous extraction + matching registry row.

    Returns ``(registry, extraction_or_none)``. When ``no_extraction_row``
    is True the registry's ``extraction_id`` is NULL, modelling the
    out-of-scope classification path that O1 surfaces.
    """
    # A synthetic service user owns the anonymous extraction until the
    # claim binds it to a real account; the test does not exercise the
    # real ``_get_or_create_service_user`` path because the conversion
    # endpoint cares only about the ``extractions`` row's existence.
    service_user = User(
        email="anonymous-service@resemblio.test",
        password_hash="argon2id$dummy$hash",
        status="active",
    )
    session.add(service_user)
    session.flush()

    extraction: Extraction | None = None
    extraction_id: int | None = None
    if not no_extraction_row:
        extraction = Extraction(
            user_id=service_user.id,
            api_key_id=None,
            url="https://example.com",
            url_normalized="https://example.com",
            status="pending",
            schema_version=SCHEMA_V1,
            credit_cents=0,
        )
        session.add(extraction)
        session.flush()
        extraction_id = extraction.id

    now = utcnow()
    expires_at = (
        now - timedelta(hours=1)
        if expired
        else now + timedelta(hours=ANON_EXTRACTION_CLAIM_WINDOW_HOURS)
    )
    registry = AnonymousExtraction(
        claim_token=claim_token,
        ip_hash="a" * 64,
        extraction_id=extraction_id,
        url="https://example.com",
        classification=classification,
        status="pending",
        schema_version=1,
        expires_at=expires_at,
    )
    session.add(registry)
    session.commit()
    session.refresh(registry)
    if extraction is not None:
        session.refresh(extraction)
    return registry, extraction


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_claim_binds_extraction_to_user_and_stamps_registry(
    client: TestClient, session: Session, pinned_internal_secret: None
) -> None:
    """A valid claim rebinds the extraction and marks the registry claimed."""
    user = _seed_user(session)
    registry, extraction = _seed_anonymous_extraction(session)
    assert extraction is not None  # mypy: extraction_id was set

    response = client.post(
        CLAIM_PATH,
        headers=_headers(),
        json={"claim_token": registry.claim_token, "user_id": user.id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "schema_version": CLAIM_ANONYMOUS_EXTRACTION_SCHEMA_VERSION,
        "ok": True,
        "extraction_id": extraction.id,
    }

    session.expire_all()
    refreshed_extraction = session.get(Extraction, extraction.id)
    refreshed_registry = session.get(AnonymousExtraction, registry.id)
    assert refreshed_extraction is not None and refreshed_registry is not None
    assert refreshed_extraction.user_id == user.id
    assert refreshed_registry.claimed_at is not None
    assert refreshed_registry.status == "claimed"


# ---------------------------------------------------------------------------
# Double-claim guard (409)
# ---------------------------------------------------------------------------


def test_double_claim_returns_409(
    client: TestClient, session: Session, pinned_internal_secret: None
) -> None:
    """A second claim with the same token returns 409 ``already_claimed``."""
    user_a = _seed_user(session, email="first@example.com")
    user_b = _seed_user(session, email="second@example.com")
    registry, _ = _seed_anonymous_extraction(session)

    first = client.post(
        CLAIM_PATH,
        headers=_headers(),
        json={"claim_token": registry.claim_token, "user_id": user_a.id},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        CLAIM_PATH,
        headers=_headers(),
        json={"claim_token": registry.claim_token, "user_id": user_b.id},
    )
    assert second.status_code == 409
    assert second.json() == {"error": "already_claimed"}

    # The extraction stays bound to the original claimer; the loser's
    # account does not silently inherit anything.
    session.expire_all()
    refreshed_registry = session.get(AnonymousExtraction, registry.id)
    assert refreshed_registry is not None
    assert refreshed_registry.extraction_id is not None
    refreshed_extraction = session.get(Extraction, refreshed_registry.extraction_id)
    assert refreshed_extraction is not None
    assert refreshed_extraction.user_id == user_a.id


# ---------------------------------------------------------------------------
# Expired claim window (410)
# ---------------------------------------------------------------------------


def test_expired_claim_returns_410(
    client: TestClient, session: Session, pinned_internal_secret: None
) -> None:
    """A claim past ``expires_at`` returns 410 ``claim_expired``."""
    user = _seed_user(session)
    registry, _ = _seed_anonymous_extraction(session, expired=True)

    response = client.post(
        CLAIM_PATH,
        headers=_headers(),
        json={"claim_token": registry.claim_token, "user_id": user.id},
    )
    assert response.status_code == 410
    assert response.json() == {"error": "claim_expired"}

    # No binding side effect should land.
    session.expire_all()
    refreshed_registry = session.get(AnonymousExtraction, registry.id)
    assert refreshed_registry is not None
    assert refreshed_registry.claimed_at is None
    assert refreshed_registry.status == "pending"


# ---------------------------------------------------------------------------
# Invalid token (404)
# ---------------------------------------------------------------------------


def test_unknown_claim_token_returns_404(
    client: TestClient, session: Session, pinned_internal_secret: None
) -> None:
    """A claim with no matching registry row returns 404 ``invalid_claim_token``."""
    user = _seed_user(session)
    response = client.post(
        CLAIM_PATH,
        headers=_headers(),
        json={"claim_token": "no-such-token-anywhere-in-the-db", "user_id": user.id},
    )
    assert response.status_code == 404
    assert response.json() == {"error": "invalid_claim_token"}


# ---------------------------------------------------------------------------
# Out-of-scope registry (extraction_id IS NULL) - 404 nothing_to_claim
# ---------------------------------------------------------------------------


def test_registry_without_extraction_returns_404_nothing_to_claim(
    client: TestClient, session: Session, pinned_internal_secret: None
) -> None:
    """A registry row that never produced an extraction returns 404 nothing_to_claim.

    Models the out-of-scope classification path: the visitor entered a
    Wix URL, got an "out_of_scope" surface, and somehow still tried to
    claim. The endpoint refuses cleanly.
    """
    user = _seed_user(session)
    registry, _ = _seed_anonymous_extraction(session, no_extraction_row=True)

    response = client.post(
        CLAIM_PATH,
        headers=_headers(),
        json={"claim_token": registry.claim_token, "user_id": user.id},
    )
    assert response.status_code == 404
    assert response.json() == {"error": "nothing_to_claim"}


# ---------------------------------------------------------------------------
# User validation (400)
# ---------------------------------------------------------------------------


def test_unknown_user_returns_400_user_not_found(
    client: TestClient, session: Session, pinned_internal_secret: None
) -> None:
    """A claim referencing a missing user_id returns 400 ``user_not_found``."""
    registry, _ = _seed_anonymous_extraction(session)
    response = client.post(
        CLAIM_PATH,
        headers=_headers(),
        json={"claim_token": registry.claim_token, "user_id": 99999},
    )
    assert response.status_code == 400
    assert response.json() == {"error": "user_not_found"}


# ---------------------------------------------------------------------------
# Internal-auth gate (401)
# ---------------------------------------------------------------------------


def test_missing_internal_secret_returns_401(
    client: TestClient, session: Session, pinned_internal_secret: None
) -> None:
    """Without the shared secret the route returns 401 and writes nothing."""
    user = _seed_user(session)
    registry, _ = _seed_anonymous_extraction(session)
    response = client.post(
        CLAIM_PATH,
        json={"claim_token": registry.claim_token, "user_id": user.id},
    )
    assert response.status_code == 401
    assert response.json() == {"error": "internal_auth_invalid"}

    session.expire_all()
    refreshed_registry = session.get(AnonymousExtraction, registry.id)
    assert refreshed_registry is not None
    assert refreshed_registry.claimed_at is None
