"""Tests for API key lifecycle routes.

S3b Wave 3 adds coverage for:
  - ``kind`` filtering: BFF/service rows are invisible to the public surface
  - per-key audit endpoint + cursor pagination
  - user isolation: user A cannot list/rotate/revoke/audit user B's keys
  - masked-preview field (the ``key_prefix`` already serves this; the test
    locks the contract so a future refactor cannot collapse it)
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import API_KEY_KIND_INTERNAL_BFF, API_KEY_KIND_SERVICE, DEFAULT_API_SCOPE
from app.crypto import generate_api_key
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


def test_list_excludes_internal_bff_and_service_kinds(client: TestClient, session: Session) -> None:
    """Listing api_keys hides BFF and service rows even when owned by the caller."""
    user, user_key, plaintext = seed_user(session)
    # Plant a BFF key owned by the same user. The list response must NOT show it.
    _, bff_digest, bff_prefix = generate_api_key("live")
    bff_key = ApiKey(
        user_id=user.id,
        key_hash=bff_digest,
        key_prefix=bff_prefix,
        label="bff_session",
        scopes=[DEFAULT_API_SCOPE],
        kind=API_KEY_KIND_INTERNAL_BFF,
        is_visible_to_user=False,
    )
    session.add(bff_key)
    # Plant a service key owned by the same user. Also hidden.
    _, svc_digest, svc_prefix = generate_api_key("live")
    svc_key = ApiKey(
        user_id=user.id,
        key_hash=svc_digest,
        key_prefix=svc_prefix,
        label="service",
        scopes=[DEFAULT_API_SCOPE],
        kind=API_KEY_KIND_SERVICE,
        is_visible_to_user=False,
    )
    session.add(svc_key)
    session.commit()
    listed = client.get("/v1/api_keys", headers=auth_headers(plaintext)).json()
    prefixes = {item["key_prefix"] for item in listed["items"]}
    assert user_key.key_prefix in prefixes
    assert bff_prefix not in prefixes
    assert svc_prefix not in prefixes


def test_rotate_revoke_audit_reject_non_user_kind(client: TestClient, session: Session) -> None:
    """BFF-kind keys are not addressable by id from the public api_keys surface."""
    user, _user_key, plaintext = seed_user(session)
    _, bff_digest, bff_prefix = generate_api_key("live")
    bff_key = ApiKey(
        user_id=user.id,
        key_hash=bff_digest,
        key_prefix=bff_prefix,
        label="bff_session",
        scopes=[DEFAULT_API_SCOPE],
        kind=API_KEY_KIND_INTERNAL_BFF,
        is_visible_to_user=False,
    )
    session.add(bff_key)
    session.commit()
    session.refresh(bff_key)
    rotate = client.post(f"/v1/api_keys/{bff_key.id}/rotate", headers=auth_headers(plaintext))
    revoke = client.post(
        f"/v1/api_keys/{bff_key.id}/revoke",
        headers=auth_headers(plaintext),
        json={"reason": "lost"},
    )
    audit = client.get(f"/v1/api_keys/{bff_key.id}/audit", headers=auth_headers(plaintext))
    assert rotate.status_code == 404
    assert revoke.status_code == 404
    assert audit.status_code == 404


def test_user_isolation_cannot_touch_other_users_keys(client: TestClient, session: Session) -> None:
    """User A cannot list, rotate, revoke, or audit user B's keys."""
    _user_a, _key_a, plaintext_a = seed_user(session, email="a@example.com")
    _user_b, key_b, _plaintext_b = seed_user(session, email="b@example.com")
    # A's list must not contain B's key.
    listed = client.get("/v1/api_keys", headers=auth_headers(plaintext_a)).json()
    prefixes = {item["key_prefix"] for item in listed["items"]}
    assert key_b.key_prefix not in prefixes
    # A tries to rotate / revoke / audit B's key by id. All 404.
    assert client.post(f"/v1/api_keys/{key_b.id}/rotate", headers=auth_headers(plaintext_a)).status_code == 404
    assert (
        client.post(
            f"/v1/api_keys/{key_b.id}/revoke",
            headers=auth_headers(plaintext_a),
            json={"reason": "lost"},
        ).status_code
        == 404
    )
    assert client.get(f"/v1/api_keys/{key_b.id}/audit", headers=auth_headers(plaintext_a)).status_code == 404


def test_audit_returns_events_newest_first_and_paginates(client: TestClient, session: Session) -> None:
    """Audit endpoint returns events newest-first and supports `before` cursor."""
    _user, api_key, plaintext = seed_user(session)
    # Generate audit events by calling the API (rotation creates two events).
    client.post(f"/v1/api_keys/{api_key.id}/rotate", headers=auth_headers(plaintext))
    audit = client.get(f"/v1/api_keys/{api_key.id}/audit", headers=auth_headers(plaintext))
    assert audit.status_code == 200
    body = audit.json()
    assert body["schema_version"] == 1
    assert len(body["items"]) >= 1
    # Newest-first: ids are descending.
    ids = [item["id"] for item in body["items"]]
    assert ids == sorted(ids, reverse=True)
    # Cursor pagination: ask for events strictly before the smallest id.
    smallest = ids[-1]
    paged = client.get(
        f"/v1/api_keys/{api_key.id}/audit",
        headers=auth_headers(plaintext),
        params={"before": smallest, "limit": 5},
    )
    assert paged.status_code == 200
    for item in paged.json()["items"]:
        assert item["id"] < smallest


def test_audit_endpoint_handles_ipaddress_row_value(client: TestClient, session: Session) -> None:
    """Regression: route must handle ``IPv4Address`` row values that Postgres INET returns.

    Production hit a 500 because ``ApiKeyAuditEvent.ip: str | None`` rejected
    the ``IPv4Address`` returned by the INET column. SQLite returns a plain
    string and masked the bug. To exercise the failure path the route
    construction takes, we monkeypatch ``ApiKeyEvent.ip`` so the route sees an
    ``IPv4Address`` for the audit row exactly as it would on Postgres. Without
    the route's ``str(row.ip)`` guard this test reproduces the 500.
    """
    from ipaddress import IPv4Address

    _user, api_key, plaintext = seed_user(session)
    event = ApiKeyEvent(
        api_key_id=api_key.id,
        event_type="used",
        ip="45.86.210.121",
        metadata_json={"path": "/v1/account"},
    )
    session.add(event)
    session.commit()
    target_event_id = event.id

    # Promote the loaded row's ``ip`` string to an ``IPv4Address`` after the
    # ORM hydrates it, mirroring how postgresql.INET deserializes the column.
    from sqlalchemy import event as sa_event

    def _promote_ip(target, _context):  # type: ignore[no-untyped-def]
        if target.id == target_event_id and isinstance(target.ip, str):
            target.ip = IPv4Address(target.ip)

    sa_event.listen(ApiKeyEvent, "load", _promote_ip)
    # Drop the cached instance so the next query re-hydrates and fires `load`.
    session.expire_all()
    try:
        response = client.get(f"/v1/api_keys/{api_key.id}/audit", headers=auth_headers(plaintext))
    finally:
        sa_event.remove(ApiKeyEvent, "load", _promote_ip)

    assert response.status_code == 200
    body = response.json()
    ips = [item["ip"] for item in body["items"] if item["ip"] is not None]
    assert "45.86.210.121" in ips


def test_create_records_kind_user_visible(client: TestClient, session: Session) -> None:
    """Newly minted keys via /v1/api_keys carry kind='user' and is_visible_to_user."""
    _user, _key, plaintext = seed_user(session)
    created = client.post("/v1/api_keys", headers=auth_headers(plaintext), json={"label": "laptop2"})
    assert created.status_code == 200
    new_id = created.json()["id"]
    row = session.get(ApiKey, new_id)
    assert row is not None
    assert row.kind == "user"
    assert row.is_visible_to_user is True


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
