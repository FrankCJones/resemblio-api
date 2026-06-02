"""S5 hardening regression suite (R7).

Bundles four narrow regression tests for the items shipped in the
2026-06-02 S5 hardening pass. Each test is designed to fail BEFORE the
corresponding fix lands and pass AFTER, so the pass acts as a change-
detector going forward.

Items covered
-------------
1. ``Retry-After`` header on 429 rate-limit responses (``app/auth.py``).
2. ``X-Request-Id`` correlation echo on every response (``app/request_id.py``).
3. Magic-link per-email cooldown throttle (``app/routes/internal_auth.py``).
4. Unhandled-exception 500 response carries a request id
   (``app/main.py``).
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import reset_settings_cache
from app.constants import RATE_LIMIT_PER_MIN
from app.email import get_email_sender
from app.main import app
from app.models import MagicLinkToken
from app.rate_limit import InMemoryRateLimiter, TokenBucket, reset_rate_limiter
from app.request_id import REQUEST_ID_HEADER, _accept_incoming, new_request_id
from tests.conftest import auth_headers, seed_user


# Re-use the internal-auth test helpers; copying the small surface keeps the
# S5 file independent of cross-file import order.
INTERNAL_SECRET = "test-internal-auth-secret-for-tests"


class _FakeEmailSender:
    """In-process email sender stand-in (mirrors test_internal_auth)."""

    def __init__(self) -> None:
        """Start with an empty outbox."""
        self.sent: list[dict[str, str]] = []

    def send_topup_cleared(self, to_email: str, amount_cents: int, balance_cents: int) -> None:
        """Capture (unused here)."""
        self.sent.append({"kind": "topup", "to": to_email})

    def send_low_quality_auto_refund(self, to_email: str, amount_cents: int, source_url: str) -> None:
        """Capture (unused here)."""
        self.sent.append({"kind": "auto_refund", "to": to_email})

    def send_magic_link(self, to_email: str, link: str) -> None:
        """Capture a magic-link send."""
        self.sent.append({"kind": "magic_link", "to": to_email, "link": link})


@pytest.fixture
def fake_email_sender(monkeypatch: pytest.MonkeyPatch) -> _FakeEmailSender:
    """Install the in-process email sender and pin the internal secret."""
    sender = _FakeEmailSender()
    app.dependency_overrides[get_email_sender] = lambda: sender
    monkeypatch.setenv("RESEMBLIO_INTERNAL_AUTH_SECRET", INTERNAL_SECRET)
    reset_settings_cache()
    yield sender
    app.dependency_overrides.pop(get_email_sender, None)
    reset_settings_cache()


def _internal_headers() -> dict[str, str]:
    """Build the internal-auth header pair."""
    return {"X-Internal-Auth": INTERNAL_SECRET}


# ---------------------------------------------------------------------------
# Item 1: Retry-After on 429
# ---------------------------------------------------------------------------


def test_token_bucket_seconds_until_token_is_positive_integer() -> None:
    """The Retry-After helper returns >=1 whole seconds when bucket is empty."""
    bucket = TokenBucket(capacity=60, refill_seconds=60, tokens=0.0, updated_at=0.0)
    seconds = bucket.seconds_until_token(now=0.0)
    assert isinstance(seconds, int)
    assert seconds >= 1


def test_rate_limit_check_populates_retry_after() -> None:
    """An over-quota result on the InMemoryRateLimiter carries retry_after_seconds."""
    limiter = InMemoryRateLimiter()
    for _ in range(RATE_LIMIT_PER_MIN):
        assert limiter.check("hash-1", 1).allowed
    result = limiter.check("hash-1", 1)
    assert result.allowed is False
    assert result.error == "rate_limit_minute"
    assert result.retry_after_seconds is not None
    assert result.retry_after_seconds >= 1


def test_rate_limited_response_includes_retry_after_header(
    client: TestClient, session: Session
) -> None:
    """A 429 from the API surface carries the integer Retry-After header.

    Mints a user + key, exhausts the per-minute bucket via direct limiter
    calls (avoids issuing 60 real HTTP calls), then asserts the next
    authenticated request returns 429 + ``Retry-After``.
    """
    from app.rate_limit import rate_limiter

    _user, api_key, plaintext = seed_user(session)
    # Drain the bucket directly so the next /v1/account call trips the limit.
    for _ in range(RATE_LIMIT_PER_MIN):
        assert rate_limiter.check(api_key.key_hash, _user.id).allowed
    response = client.get("/v1/account", headers=auth_headers(plaintext))
    assert response.status_code == 429
    assert response.json()["error"] in {"rate_limit_minute", "rate_limit_day"}
    assert "retry-after" in {k.lower() for k in response.headers.keys()}
    # Must be a non-negative integer per RFC 9110.
    assert int(response.headers["retry-after"]) >= 1
    reset_rate_limiter()


# ---------------------------------------------------------------------------
# Item 2: X-Request-Id correlation
# ---------------------------------------------------------------------------


def test_request_id_minted_when_absent(client: TestClient) -> None:
    """The middleware mints an id if the caller did not supply one."""
    response = client.get("/v1/healthz")
    assert response.status_code == 200
    assert REQUEST_ID_HEADER in response.headers
    # 32-char uuid4 hex; the shape is locked here so log greps stay stable.
    assert len(response.headers[REQUEST_ID_HEADER]) == 32


def test_well_formed_incoming_request_id_is_honored(client: TestClient) -> None:
    """A well-formed caller-supplied id flows through unchanged for tracing."""
    incoming = "svc-corr-abc123-XYZ"
    response = client.get("/v1/healthz", headers={REQUEST_ID_HEADER: incoming})
    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == incoming


def test_malformed_incoming_request_id_is_replaced(client: TestClient) -> None:
    """Bad characters or oversize values are dropped (anti-log-injection)."""
    bad = "abc\r\ndef OK"  # contains whitespace + control chars
    response = client.get("/v1/healthz", headers={REQUEST_ID_HEADER: bad})
    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] != bad
    assert len(response.headers[REQUEST_ID_HEADER]) == 32


def test_accept_incoming_rejects_too_short_and_too_long() -> None:
    """The acceptance shape is bounded on both ends."""
    assert _accept_incoming("short") is None
    assert _accept_incoming("a" * 129) is None
    assert _accept_incoming(None) is None
    assert _accept_incoming("") is None
    # Valid example.
    assert _accept_incoming(new_request_id()) is not None


def test_auth_error_response_carries_request_id(client: TestClient) -> None:
    """A 401 from AuthMiddleware still includes the request_id echo.

    Validates that RequestIdMiddleware sits OUTSIDE AuthMiddleware so an
    unauthenticated probe still gets a correlation token in its response.
    """
    response = client.get("/v1/account")  # no Authorization header
    assert response.status_code == 401
    assert REQUEST_ID_HEADER in response.headers


# ---------------------------------------------------------------------------
# Item 3: Magic-link per-email cooldown
# ---------------------------------------------------------------------------


def test_magic_link_request_is_throttled_within_cooldown(
    client: TestClient, session: Session, fake_email_sender: _FakeEmailSender
) -> None:
    """A second request inside the cooldown window does not double-mint or double-send."""
    body = {"email": "cooldown@example.com"}
    first = client.post(
        "/v1/internal/auth/request_magic_link",
        headers=_internal_headers(),
        json=body,
    )
    assert first.status_code == 200
    assert first.json() == {"ok": True}
    # Second call IMMEDIATELY after the first lands within the cooldown.
    second = client.post(
        "/v1/internal/auth/request_magic_link",
        headers=_internal_headers(),
        json=body,
    )
    assert second.status_code == 200
    assert second.json() == {"ok": True}
    # Single mint, single send: the throttle suppressed the second mint+send.
    rows = session.query(MagicLinkToken).filter(
        MagicLinkToken.email == "cooldown@example.com"
    ).all()
    assert len(rows) == 1
    assert len(fake_email_sender.sent) == 1


def test_magic_link_throttle_is_per_email(
    client: TestClient, session: Session, fake_email_sender: _FakeEmailSender
) -> None:
    """The cooldown is keyed on the lowercase email, not global."""
    one = client.post(
        "/v1/internal/auth/request_magic_link",
        headers=_internal_headers(),
        json={"email": "alpha@example.com"},
    )
    two = client.post(
        "/v1/internal/auth/request_magic_link",
        headers=_internal_headers(),
        json={"email": "beta@example.com"},
    )
    assert one.status_code == 200
    assert two.status_code == 200
    rows = session.query(MagicLinkToken).all()
    assert len(rows) == 2
    assert len(fake_email_sender.sent) == 2


# ---------------------------------------------------------------------------
# Item 4: Unhandled-exception 500 carries request_id
# ---------------------------------------------------------------------------


def test_unhandled_exception_response_carries_request_id(
    client: TestClient, session: Session
) -> None:
    """A route that raises an unexpected exception returns 500 + request_id.

    Installs a one-off router with a handler that raises under the existing
    ``/v1/healthz`` AUTH_FREE_PATHS prefix variant, so AuthMiddleware does
    not short-circuit with a 401 before the exception handler can fire.
    The path is removed in a ``finally`` so the change is invisible to
    subsequent tests.
    """
    from app import auth as auth_module

    boom_router = APIRouter()

    @boom_router.get("/boom")
    def _boom() -> dict[str, Any]:
        raise RuntimeError("synthetic boom")

    app.include_router(boom_router, prefix="/v1", tags=["test_only"])
    original_paths = auth_module.AUTH_FREE_PATHS
    auth_module.AUTH_FREE_PATHS = frozenset(set(original_paths) | {"/v1/boom"})
    try:
        # TestClient surfaces the structured 500 from the exception handler
        # only when raise_server_exceptions is disabled.
        with TestClient(app, raise_server_exceptions=False) as raw_client:
            response = raw_client.get("/v1/boom")
        assert response.status_code == 500, response.text
        body = response.json()
        assert body["error"] == "internal_error"
        assert body["request_id"]
        assert response.headers.get(REQUEST_ID_HEADER) == body["request_id"]
    finally:
        auth_module.AUTH_FREE_PATHS = original_paths
        # Drop the test-only route so it does not leak into other tests.
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", None) != "/v1/boom"
        ]
