"""Tests for API key authentication middleware."""
from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import utcnow
from app.models import ApiKeyEvent
from tests.conftest import auth_headers, seed_user


def test_missing_bad_and_valid_credentials(client: TestClient, session: Session) -> None:
    """Auth middleware rejects missing and malformed credentials, then accepts a valid key."""
    _, _, plaintext = seed_user(session)
    assert client.get("/v1/account").json() == {"error": "missing_credentials"}
    assert client.get("/v1/account", headers=auth_headers("not-a-key")).json() == {"error": "invalid_credentials"}
    response = client.get("/v1/account", headers=auth_headers(plaintext))
    assert response.status_code == 200
    assert response.json()["email"] == "frank@optsus.com"


def test_rotated_key_works_during_grace(client: TestClient, session: Session) -> None:
    """A rotated-out key still authenticates before grace expiry and returns a warning."""
    _, api_key, plaintext = seed_user(session)
    api_key.status = "rotated_out"
    api_key.grace_expires_at = utcnow() + timedelta(hours=1)
    session.commit()
    response = client.get("/v1/account", headers=auth_headers(plaintext))
    assert response.status_code == 200
    assert "X-API-Key-Rotation-Warning" in response.headers


def test_revoked_key_rejected_and_audited(client: TestClient, session: Session) -> None:
    """A revoked key returns 401 and appends attempted-after-revocation."""
    _, api_key, plaintext = seed_user(session)
    api_key.status = "revoked"
    session.commit()
    response = client.get("/v1/account", headers=auth_headers(plaintext))
    assert response.status_code == 401
    assert response.json()["error"] == "key_revoked"
    event = session.query(ApiKeyEvent).filter(ApiKeyEvent.event_type == "attempted_after_revocation").one()
    assert event.api_key_id == api_key.id


def test_rotated_key_expires_after_grace(client: TestClient, session: Session) -> None:
    """A rotated key past grace is marked expired and rejected."""
    _, api_key, plaintext = seed_user(session)
    api_key.status = "rotated_out"
    api_key.grace_expires_at = utcnow() - timedelta(seconds=1)
    session.commit()
    response = client.get("/v1/account", headers=auth_headers(plaintext))
    session.refresh(api_key)
    assert response.status_code == 401
    assert response.json()["error"] == "key_expired"
    assert api_key.status == "expired"

