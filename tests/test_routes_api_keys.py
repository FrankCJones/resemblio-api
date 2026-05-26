"""Tests for API key lifecycle routes."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ApiKey, ApiKeyEvent
from tests.conftest import auth_headers, seed_user


def test_create_and_list_api_key(client: TestClient, session: Session) -> None:
    """Creating a key returns plaintext once and listing returns only prefixes."""
    _, _, plaintext = seed_user(session)
    created = client.post("/v1/api_keys", headers=auth_headers(plaintext), json={"label": "laptop"})
    assert created.status_code == 200
    body = created.json()
    assert body["api_key"].startswith("rsmb_live_")
    listed = client.get("/v1/api_keys", headers=auth_headers(plaintext)).json()
    assert len(listed["items"]) == 2
    assert "api_key" not in listed["items"][0]


def test_rotate_old_and_new_work_then_revoke(client: TestClient, session: Session) -> None:
    """Rotation returns a new key, old key works during grace, and revoke blocks use."""
    _, api_key, plaintext = seed_user(session)
    rotated = client.post(f"/v1/api_keys/{api_key.id}/rotate", headers=auth_headers(plaintext))
    assert rotated.status_code == 200
    new_plaintext = rotated.json()["api_key"]
    old_response = client.get("/v1/account", headers=auth_headers(plaintext))
    new_response = client.get("/v1/account", headers=auth_headers(new_plaintext))
    assert old_response.status_code == 200
    assert "X-API-Key-Rotation-Warning" in old_response.headers
    assert new_response.status_code == 200
    new_key = session.query(ApiKey).filter(ApiKey.key_prefix == rotated.json()["key_prefix"]).one()
    revoked = client.post(f"/v1/api_keys/{new_key.id}/revoke", headers=auth_headers(new_plaintext), json={"reason": "lost"})
    assert revoked.status_code == 200
    blocked = client.get("/v1/account", headers=auth_headers(new_plaintext))
    assert blocked.status_code == 401
    event_types = {event.event_type for event in session.query(ApiKeyEvent).all()}
    assert {"rotated_out", "rotated_in", "revoked"}.issubset(event_types)


def test_update_spend_cap_records_event(client: TestClient, session: Session) -> None:
    """Spend-cap updates mutate the key and append an audit event."""
    _user, api_key, plaintext = seed_user(session)
    response = client.patch(
        f"/v1/api_keys/{api_key.id}/spend_cap",
        headers=auth_headers(plaintext),
        json={"cap_cents": 2000},
    )
    assert response.status_code == 200
    assert response.json()["spend_cap_cents"] == 2000
    session.expire_all()
    key = session.get(ApiKey, api_key.id)
    assert key is not None
    assert key.spend_cap_cents == 2000
    event = session.query(ApiKeyEvent).filter(ApiKeyEvent.event_type == "spend_cap_changed").one()
    assert event.metadata_json == {"old_cap_cents": None, "new_cap_cents": 2000}
