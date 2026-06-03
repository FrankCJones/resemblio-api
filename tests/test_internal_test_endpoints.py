"""Tests for the test-only internal endpoints that gate the O9 Playwright E2E suite.

Two routes are covered:

* ``GET  /v1/internal/auth/test_get_latest_magic_link`` returns the latest
  unconsumed plaintext magic-link token for the requested email so the
  Playwright suite can synthesize a click without scraping a real inbox.
* ``POST /v1/internal/test/teardown_user`` deletes a user and every child
  row a Playwright run could have left behind (magic-link tokens,
  web-session keys, anonymous-extraction registry rows for extractions
  the user owns, owned extractions, api keys).

Both routes are dark by default: they refuse to respond unless BOTH
``RESEMBLIO_TEST_AUTH_ENABLED=1`` and ``RESEMBLIO_TEST_AUTH_TOKEN`` are set,
AND the request carries an ``X-Test-Auth`` header equal to the env value.
The plaintext-token return path is intentional and ONLY safe under those
two stacked gates; enabling them on a prod box is a critical safety
violation and a forbidden action per the runbook.

The fixture pins both env vars via ``monkeypatch.setenv`` so the values are
restored after each test even if an assertion fails mid-run.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import utcnow
from app.config import reset_settings_cache
from app.constants import DEFAULT_API_SCOPE
from app.crypto import generate_api_key, hash_password
from app.models import (
    AnonymousExtraction,
    ApiKey,
    Extraction,
    MagicLinkToken,
    User,
    WebSessionKey,
)


TEST_AUTH_TOKEN = "test-only-magic-link-readback-token-do-not-deploy"


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the test-auth surface for the duration of a single test."""
    monkeypatch.setenv("RESEMBLIO_TEST_AUTH_ENABLED", "1")
    monkeypatch.setenv("RESEMBLIO_TEST_AUTH_TOKEN", TEST_AUTH_TOKEN)
    reset_settings_cache()
    yield
    reset_settings_cache()


def _auth_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the ``X-Test-Auth`` header bundle with optional overrides."""
    base = {"X-Test-Auth": TEST_AUTH_TOKEN}
    if extra:
        base.update(extra)
    return base


def _seed_magic_link(session: Session, email: str, plaintext: str, *, consumed: bool = False) -> MagicLinkToken:
    """Insert a magic-link row for the test; ``consumed=True`` simulates a used token."""
    token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    now = utcnow()
    row = MagicLinkToken(
        email=email.lower(),
        token_hash=token_hash,
        expires_at=now + timedelta(minutes=15),
        consumed_at=now if consumed else None,
        plaintext_token=plaintext,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _seed_user_with_children(session: Session, email: str) -> User:
    """Create a user plus one row in every child table the teardown must reach."""
    user = User(email=email.lower(), password_hash=hash_password("password"), status="active")
    session.add(user)
    session.flush()
    plaintext, digest, prefix = generate_api_key("test")
    api_key = ApiKey(
        user_id=user.id,
        key_hash=digest,
        key_prefix=prefix,
        label="e2e",
        scopes=[DEFAULT_API_SCOPE],
    )
    session.add(api_key)
    session.flush()
    session.add(WebSessionKey(user_id=user.id, api_key_id=api_key.id))
    _seed_magic_link(session, email, "plaintext-for-teardown")
    extraction = Extraction(
        user_id=user.id,
        api_key_id=api_key.id,
        url="https://example.com",
        url_normalized="example.com",
        status="ok",
        schema_version=1,
    )
    session.add(extraction)
    session.flush()
    session.add(
        AnonymousExtraction(
            claim_token=secrets.token_urlsafe(24),
            ip_hash="0" * 64,
            extraction_id=extraction.id,
            url="https://example.com",
            classification="in_scope",
            status="claimed",
            expires_at=utcnow() + timedelta(hours=24),
        )
    )
    session.commit()
    return user


# --- Gate enforcement -----------------------------------------------------


def test_get_latest_magic_link_403_when_flag_unset(client: TestClient) -> None:
    """Endpoint returns 403 when the env enable flag is not set."""
    response = client.get(
        "/v1/internal/auth/test_get_latest_magic_link",
        params={"email": "ghost@example.com"},
        headers=_auth_headers(),
    )
    assert response.status_code == 403
    assert response.json() == {"error": "test_auth_disabled"}


def test_teardown_user_403_when_flag_unset(client: TestClient) -> None:
    """Teardown also refuses to respond when the env enable flag is unset."""
    response = client.post(
        "/v1/internal/test/teardown_user",
        json={"email": "ghost@example.com"},
        headers=_auth_headers(),
    )
    assert response.status_code == 403
    assert response.json() == {"error": "test_auth_disabled"}


def test_get_latest_magic_link_401_when_header_missing(client: TestClient, enabled: None) -> None:
    """Endpoint returns 401 when the X-Test-Auth header is absent."""
    response = client.get(
        "/v1/internal/auth/test_get_latest_magic_link",
        params={"email": "ghost@example.com"},
    )
    assert response.status_code == 401
    assert response.json() == {"error": "test_auth_invalid"}


def test_teardown_user_401_when_header_missing(client: TestClient, enabled: None) -> None:
    """Teardown returns 401 when the X-Test-Auth header is absent."""
    response = client.post(
        "/v1/internal/test/teardown_user",
        json={"email": "ghost@example.com"},
    )
    assert response.status_code == 401
    assert response.json() == {"error": "test_auth_invalid"}


def test_get_latest_magic_link_401_when_header_wrong(client: TestClient, enabled: None) -> None:
    """Endpoint returns 401 when the X-Test-Auth header value mismatches."""
    response = client.get(
        "/v1/internal/auth/test_get_latest_magic_link",
        params={"email": "ghost@example.com"},
        headers={"X-Test-Auth": "wrong"},
    )
    assert response.status_code == 401


# --- Magic-link readback --------------------------------------------------


def test_get_latest_magic_link_returns_latest_unconsumed(
    client: TestClient, session: Session, enabled: None
) -> None:
    """Latest unconsumed token is returned; consumed tokens are skipped."""
    email = "e2e@example.com"
    _seed_magic_link(session, email, "old-plaintext-token", consumed=True)
    fresh = _seed_magic_link(session, email, "fresh-plaintext-token")
    response = client.get(
        "/v1/internal/auth/test_get_latest_magic_link",
        params={"email": email},
        headers=_auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["token"] == "fresh-plaintext-token"
    assert body["email"] == email
    assert "expires_at" in body
    # The returned token must match the row we just inserted (sanity check).
    assert hashlib.sha256(body["token"].encode("utf-8")).hexdigest() == fresh.token_hash


def test_get_latest_magic_link_404_when_no_unconsumed(
    client: TestClient, session: Session, enabled: None
) -> None:
    """404 when the email has no unconsumed token (or none at all)."""
    email = "ghost@example.com"
    _seed_magic_link(session, email, "already-consumed", consumed=True)
    response = client.get(
        "/v1/internal/auth/test_get_latest_magic_link",
        params={"email": email},
        headers=_auth_headers(),
    )
    assert response.status_code == 404
    assert response.json() == {"error": "no_unconsumed_token"}


# --- Teardown -------------------------------------------------------------


def test_teardown_user_removes_all_child_rows(
    client: TestClient, session: Session, enabled: None
) -> None:
    """Teardown deletes user + every child row a Playwright run could have made."""
    email = "e2e-teardown@example.com"
    user = _seed_user_with_children(session, email)
    user_id = user.id

    response = client.post(
        "/v1/internal/test/teardown_user",
        json={"email": email},
        headers=_auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["ok"] is True
    assert body["deleted_rows"] >= 5

    session.expire_all()
    assert session.get(User, user_id) is None
    assert session.execute(select(MagicLinkToken).where(MagicLinkToken.email == email)).first() is None
    assert session.execute(select(ApiKey).where(ApiKey.user_id == user_id)).first() is None
    assert session.execute(select(WebSessionKey).where(WebSessionKey.user_id == user_id)).first() is None
    assert session.execute(select(Extraction).where(Extraction.user_id == user_id)).first() is None
    # Anonymous-extraction registry rows pointing at the deleted user's
    # extractions must be detached or deleted; the test asserts deletion to
    # match the contract.
    assert session.execute(select(AnonymousExtraction).where(AnonymousExtraction.url == "https://example.com")).first() is None


def test_teardown_user_idempotent(
    client: TestClient, session: Session, enabled: None
) -> None:
    """Second call against an already-deleted email returns ok=true, deleted_rows=0."""
    email = "e2e-idempotent@example.com"
    _seed_user_with_children(session, email)
    first = client.post(
        "/v1/internal/test/teardown_user",
        json={"email": email},
        headers=_auth_headers(),
    )
    assert first.status_code == 200
    assert first.json()["ok"] is True

    second = client.post(
        "/v1/internal/test/teardown_user",
        json={"email": email},
        headers=_auth_headers(),
    )
    assert second.status_code == 200
    body = second.json()
    assert body["schema_version"] == 1
    assert body["ok"] is True
    assert body["deleted_rows"] == 0
