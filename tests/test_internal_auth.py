"""Tests for the internal BFF auth surface (S3 magic-link + session keys).

The four endpoints under ``/v1/internal/auth/*`` are exercised against the
in-memory SQLite fixture from ``conftest.py``. Resend is replaced with an
in-process fake so no network calls happen during the suite.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import utcnow
from app.config import reset_settings_cache
from app.constants import API_KEY_KIND_INTERNAL_BFF, MAGIC_LINK_EXPIRY_MINUTES
from app.email import get_email_sender
from app.main import app
from app.models import ApiKey, MagicLinkToken, User, WebSessionKey
from app.routes import internal_auth


INTERNAL_SECRET = "test-internal-auth-secret-for-tests"


class _FakeEmailSender:
    """In-process replacement for ``ResendEmailSender``.

    Captures every send call so the tests can assert the recipient and the
    rendered link without hitting the network.
    """

    def __init__(self) -> None:
        """Start with an empty outbox."""
        self.sent: list[dict[str, str]] = []

    def send_topup_cleared(self, to_email: str, amount_cents: int, balance_cents: int) -> None:
        """Capture a top-up email send (unused by these tests)."""
        self.sent.append({"kind": "topup", "to": to_email})

    def send_low_quality_auto_refund(self, to_email: str, amount_cents: int, source_url: str) -> None:
        """Capture an auto-refund email send (unused by these tests)."""
        self.sent.append({"kind": "auto_refund", "to": to_email})

    def send_magic_link(self, to_email: str, link: str) -> None:
        """Capture a magic-link email send."""
        self.sent.append({"kind": "magic_link", "to": to_email, "link": link})


@pytest.fixture
def fake_email_sender(monkeypatch: pytest.MonkeyPatch) -> _FakeEmailSender:
    """Install the in-process email sender and pin the internal secret.

    Also overrides ``RESEMBLIO_INTERNAL_AUTH_SECRET`` in the environment so
    the internal-auth routes accept calls from the test client; the
    settings cache is reset to pick up the new value.
    """
    sender = _FakeEmailSender()
    app.dependency_overrides[get_email_sender] = lambda: sender
    monkeypatch.setenv("RESEMBLIO_INTERNAL_AUTH_SECRET", INTERNAL_SECRET)
    reset_settings_cache()
    yield sender
    app.dependency_overrides.pop(get_email_sender, None)
    reset_settings_cache()


def _internal_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build internal-auth headers; merge extras for per-test additions."""
    base = {"X-Internal-Auth": INTERNAL_SECRET}
    if extra:
        base.update(extra)
    return base


def _extract_token_from_link(link: str) -> str:
    """Return the ``?token=...`` query parameter from a magic-link URL."""
    return link.split("token=", 1)[1]


def _request_link(client: TestClient, email: str) -> str:
    """Send a magic-link request and return the captured plaintext token."""
    response = client.post(
        "/v1/internal/auth/request_magic_link",
        headers=_internal_headers(),
        json={"email": email, "ip": "127.0.0.1", "user_agent": "pytest"},
    )
    assert response.status_code == 200, response.text
    # The sender is the dependency-overridden fake; pull from its outbox.
    sender: _FakeEmailSender = app.dependency_overrides[get_email_sender]()  # type: ignore[assignment]
    last = sender.sent[-1]
    assert last["kind"] == "magic_link"
    assert last["to"] == email
    return _extract_token_from_link(last["link"])


def test_request_magic_link_writes_token_and_sends_email(
    client: TestClient, session: Session, fake_email_sender: _FakeEmailSender
) -> None:
    """A successful request writes the hashed token and dispatches Resend."""
    response = client.post(
        "/v1/internal/auth/request_magic_link",
        headers=_internal_headers(),
        json={"email": "new@example.com"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    rows = session.query(MagicLinkToken).all()
    assert len(rows) == 1
    assert rows[0].email == "new@example.com"
    assert rows[0].consumed_at is None
    assert len(fake_email_sender.sent) == 1
    assert fake_email_sender.sent[0]["to"] == "new@example.com"
    assert "token=" in fake_email_sender.sent[0]["link"]


def test_request_magic_link_rejects_missing_internal_secret(
    client: TestClient, fake_email_sender: _FakeEmailSender
) -> None:
    """Without the shared secret the route returns 401 and does NOT send."""
    response = client.post(
        "/v1/internal/auth/request_magic_link",
        json={"email": "x@example.com"},
    )
    assert response.status_code == 401
    assert fake_email_sender.sent == []


def test_redeem_magic_link_rejects_expired_tokens(
    client: TestClient, session: Session, fake_email_sender: _FakeEmailSender
) -> None:
    """An expired token returns 400 token_expired and is NOT consumed."""
    token = _request_link(client, "expired@example.com")
    row = session.query(MagicLinkToken).one()
    row.expires_at = utcnow() - timedelta(minutes=1)
    session.commit()
    response = client.post(
        "/v1/internal/auth/redeem_magic_link",
        headers=_internal_headers(),
        json={"token": token},
    )
    assert response.status_code == 400
    assert response.json() == {"error": "token_expired"}
    session.expire_all()
    refreshed = session.query(MagicLinkToken).one()
    assert refreshed.consumed_at is None


def test_redeem_magic_link_rejects_consumed_tokens(
    client: TestClient, session: Session, fake_email_sender: _FakeEmailSender
) -> None:
    """A token that has already been redeemed cannot be redeemed again."""
    token = _request_link(client, "twice@example.com")
    first = client.post(
        "/v1/internal/auth/redeem_magic_link",
        headers=_internal_headers(),
        json={"token": token},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/v1/internal/auth/redeem_magic_link",
        headers=_internal_headers(),
        json={"token": token},
    )
    assert second.status_code == 400
    assert second.json() == {"error": "token_consumed"}


def test_redeem_magic_link_creates_new_user(
    client: TestClient, session: Session, fake_email_sender: _FakeEmailSender
) -> None:
    """First redemption for an email creates the user row and a BFF key."""
    assert session.query(User).count() == 0
    token = _request_link(client, "first@example.com")
    response = client.post(
        "/v1/internal/auth/redeem_magic_link",
        headers=_internal_headers(),
        json={"token": token},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_new_user"] is True
    assert body["email"] == "first@example.com"
    assert body["api_key"].startswith("rsmb_")
    user = session.query(User).one()
    assert user.email == "first@example.com"
    keys = session.query(ApiKey).filter(ApiKey.user_id == user.id).all()
    assert len(keys) == 1
    assert keys[0].kind == API_KEY_KIND_INTERNAL_BFF
    assert keys[0].is_visible_to_user is False
    assert session.query(WebSessionKey).filter(WebSessionKey.user_id == user.id).count() == 1


def test_redeem_magic_link_finds_existing_user(
    client: TestClient, session: Session, fake_email_sender: _FakeEmailSender
) -> None:
    """Second redemption with the same email reuses the existing user row."""
    first_token = _request_link(client, "repeat@example.com")
    first = client.post(
        "/v1/internal/auth/redeem_magic_link",
        headers=_internal_headers(),
        json={"token": first_token},
    )
    assert first.status_code == 200
    assert first.json()["is_new_user"] is True
    user_id_one = first.json()["user_id"]

    second_token = _request_link(client, "repeat@example.com")
    second = client.post(
        "/v1/internal/auth/redeem_magic_link",
        headers=_internal_headers(),
        json={"token": second_token},
    )
    assert second.status_code == 200
    assert second.json()["is_new_user"] is False
    assert second.json()["user_id"] == user_id_one
    assert session.query(User).count() == 1


def test_redeem_rotates_bff_key_revoking_old(
    client: TestClient, session: Session, fake_email_sender: _FakeEmailSender
) -> None:
    """A second login revokes the prior BFF key and mints a fresh one."""
    token_one = _request_link(client, "rotate@example.com")
    first = client.post(
        "/v1/internal/auth/redeem_magic_link",
        headers=_internal_headers(),
        json={"token": token_one},
    )
    old_key_plain = first.json()["api_key"]
    token_two = _request_link(client, "rotate@example.com")
    second = client.post(
        "/v1/internal/auth/redeem_magic_link",
        headers=_internal_headers(),
        json={"token": token_two},
    )
    new_key_plain = second.json()["api_key"]
    assert old_key_plain != new_key_plain

    # Old key fails whoami; new key succeeds.
    old_resp = client.get(
        "/v1/internal/auth/whoami",
        headers=_internal_headers({"X-Bff-Key": old_key_plain}),
    )
    new_resp = client.get(
        "/v1/internal/auth/whoami",
        headers=_internal_headers({"X-Bff-Key": new_key_plain}),
    )
    assert old_resp.status_code == 401
    assert new_resp.status_code == 200
    # Only one active session row, pointing at the new key.
    sessions = session.query(WebSessionKey).all()
    assert len(sessions) == 1
    bff_key_id = sessions[0].api_key_id
    bff_key = session.get(ApiKey, bff_key_id)
    assert bff_key is not None
    assert bff_key.status == "active"


def test_whoami_returns_401_for_wrong_key(
    client: TestClient, fake_email_sender: _FakeEmailSender
) -> None:
    """An unknown BFF key is rejected with 401 invalid_bff_key."""
    response = client.get(
        "/v1/internal/auth/whoami",
        headers=_internal_headers({"X-Bff-Key": "rsmb_live_doesnotexist0000000000000000000000000"}),
    )
    assert response.status_code == 401
    assert response.json()["error"] in {"invalid_bff_key", "missing_bff_key"}


def test_logout_revokes_bff_key(
    client: TestClient, session: Session, fake_email_sender: _FakeEmailSender
) -> None:
    """Logout flips the key to revoked and clears the session row."""
    token = _request_link(client, "logout@example.com")
    redeem = client.post(
        "/v1/internal/auth/redeem_magic_link",
        headers=_internal_headers(),
        json={"token": token},
    )
    assert redeem.status_code == 200
    bff_key = redeem.json()["api_key"]

    logout = client.post(
        "/v1/internal/auth/logout",
        headers=_internal_headers({"X-Bff-Key": bff_key}),
    )
    assert logout.status_code == 200
    assert logout.json() == {"ok": True}

    # Subsequent whoami with the same key returns 401.
    again = client.get(
        "/v1/internal/auth/whoami",
        headers=_internal_headers({"X-Bff-Key": bff_key}),
    )
    assert again.status_code == 401
    # Session row removed.
    assert session.query(WebSessionKey).count() == 0


def test_whoami_returns_credit_balance(
    client: TestClient, session: Session, fake_email_sender: _FakeEmailSender
) -> None:
    """The whoami payload includes the live credit balance from the ledger."""
    from app.models import CreditLedger  # local import keeps the module surface tight

    token = _request_link(client, "balance@example.com")
    redeem = client.post(
        "/v1/internal/auth/redeem_magic_link",
        headers=_internal_headers(),
        json={"token": token},
    )
    user_id = redeem.json()["user_id"]
    session.add(
        CreditLedger(
            user_id=user_id,
            entry_type="signup_grant",
            amount_cents=1000,
            balance_after_cents=1000,
            note="test signup",
        )
    )
    session.commit()
    response = client.get(
        "/v1/internal/auth/whoami",
        headers=_internal_headers({"X-Bff-Key": redeem.json()["api_key"]}),
    )
    assert response.status_code == 200
    assert response.json()["credit_balance_cents"] == 1000
    assert response.json()["email"] == "balance@example.com"
