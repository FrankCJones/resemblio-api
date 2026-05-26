"""Tests for API key authentication middleware."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import _client_ip, utcnow
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



def _fake_request(peer_host: str | None, forwarded: str | None = None) -> MagicMock:
    """Build a minimal request stub for ``_client_ip`` unit tests."""
    request = MagicMock()
    request.client = MagicMock(host=peer_host) if peer_host is not None else None
    headers: dict[str, str] = {}
    if forwarded is not None:
        headers["x-forwarded-for"] = forwarded
    # Headers behave like a case-insensitive mapping; emulate the .get() the
    # middleware uses against the real Starlette Headers object.
    request.headers.get = headers.get
    return request


def test_client_ip_ignores_forwarded_from_untrusted_peer() -> None:
    """Forwarded headers from a non-trusted peer are ignored to prevent IP spoofing."""
    request = _fake_request(peer_host="203.0.113.7", forwarded="1.2.3.4")
    assert _client_ip(request) == "203.0.113.7"


def test_client_ip_honors_forwarded_from_trusted_proxy() -> None:
    """Forwarded headers from localhost (Caddy) yield the real client hop."""
    request = _fake_request(peer_host="127.0.0.1", forwarded="198.51.100.9")
    assert _client_ip(request) == "198.51.100.9"


def test_client_ip_walks_past_trusted_hops_in_chain() -> None:
    """Leading trusted-proxy hops in the chain are skipped to find the real client."""
    request = _fake_request(peer_host="127.0.0.1", forwarded="127.0.0.1, 198.51.100.9")
    assert _client_ip(request) == "198.51.100.9"


def test_client_ip_falls_back_to_peer_when_no_forwarded_header() -> None:
    """With no forwarded header the immediate peer is returned."""
    request = _fake_request(peer_host="127.0.0.1")
    assert _client_ip(request) == "127.0.0.1"


def test_client_ip_returns_none_when_no_client() -> None:
    """A missing request.client yields None rather than raising."""
    request = _fake_request(peer_host=None)
    assert _client_ip(request) is None
