"""Regression tests for Idempotency-Key support on POST /v1/extractions.

Each test demonstrates a contract the route handler must satisfy:

* Replay with the same key + same body returns the cached response and
  does NOT re-debit the credit ledger.
* Replay with the same key + different body returns HTTP 409.
* Malformed keys (too short, too long, bad chars) are rejected at the
  boundary with HTTP 400.
* Absence of the header is the no-replay path: two identical requests
  produce two separate charges.

Provenance: 2026-06-02 R7 follow-on dispatch
(``2026-06-02-r7-s5-hardening.md`` "Items scoped but NOT shipped" entry).
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    EXTRACTION_PUBLIC_CENTS,
    IDEMPOTENCY_HEADER_NAME,
    IDEMPOTENCY_REPLAYED_HEADER_NAME,
)
from app.models import CreditLedger, Extraction, IdempotencyKey
from tests.conftest import auth_headers, seed_user


_GOOD_KEY = "ulid-01HX5K9PSEED-DEMO-VALUE-A"
_GOOD_KEY_ALT = "ulid-01HX5K9PSEED-DEMO-VALUE-B"


def _charge_count(session: Session, user_id: int) -> int:
    """Return how many ``extraction_charge`` rows exist for ``user_id``."""
    return (
        session.query(CreditLedger)
        .filter(
            CreditLedger.user_id == user_id,
            CreditLedger.entry_type == "extraction_charge",
        )
        .count()
    )


def test_idempotent_replay_returns_same_response_and_does_not_recharge(
    client: TestClient, session: Session
) -> None:
    """Same key + same body: second call replays, ledger holds one charge."""
    user, _, plaintext = seed_user(session, balance=EXTRACTION_PUBLIC_CENTS * 2)
    headers = {**auth_headers(plaintext), IDEMPOTENCY_HEADER_NAME: _GOOD_KEY}

    first = client.post("/v1/extractions", headers=headers, json={"url": "https://example.com"})
    assert first.status_code == 200
    assert first.headers.get(IDEMPOTENCY_REPLAYED_HEADER_NAME) is None
    first_body = first.json()

    second = client.post("/v1/extractions", headers=headers, json={"url": "https://example.com"})
    assert second.status_code == 200
    assert second.headers.get(IDEMPOTENCY_REPLAYED_HEADER_NAME) == "true"
    # Body equality is the property a retry-safe client relies on.
    assert second.json() == first_body

    session.expire_all()
    assert _charge_count(session, user.id) == 1
    assert session.query(Extraction).count() == 1
    # Persisted cache row exists for replay accounting.
    assert session.query(IdempotencyKey).count() == 1


def test_idempotent_replay_with_different_body_returns_409(
    client: TestClient, session: Session
) -> None:
    """Same key + different body: 409. The first body's response is preserved."""
    user, _, plaintext = seed_user(session, balance=EXTRACTION_PUBLIC_CENTS * 2)
    headers = {**auth_headers(plaintext), IDEMPOTENCY_HEADER_NAME: _GOOD_KEY}

    first = client.post("/v1/extractions", headers=headers, json={"url": "https://example.com"})
    assert first.status_code == 200

    conflict = client.post(
        "/v1/extractions",
        headers=headers,
        json={"url": "https://different.example.com"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "idempotency_key_reused_with_different_body"

    session.expire_all()
    assert _charge_count(session, user.id) == 1


def test_idempotency_key_validation_rejects_too_short_and_too_long_and_bad_chars(
    client: TestClient, session: Session
) -> None:
    """All three malformed-key shapes return 400 with the correct reason."""
    _, _, plaintext = seed_user(session, balance=EXTRACTION_PUBLIC_CENTS * 2)

    short_resp = client.post(
        "/v1/extractions",
        headers={**auth_headers(plaintext), IDEMPOTENCY_HEADER_NAME: "abc"},
        json={"url": "https://example.com"},
    )
    assert short_resp.status_code == 400
    assert short_resp.json() == {"error": "idempotency_key_invalid", "reason": "too_short"}

    long_resp = client.post(
        "/v1/extractions",
        headers={**auth_headers(plaintext), IDEMPOTENCY_HEADER_NAME: "a" * 257},
        json={"url": "https://example.com"},
    )
    assert long_resp.status_code == 400
    assert long_resp.json()["reason"] == "too_long"

    bad_chars_resp = client.post(
        "/v1/extractions",
        headers={**auth_headers(plaintext), IDEMPOTENCY_HEADER_NAME: "has spaces here"},
        json={"url": "https://example.com"},
    )
    assert bad_chars_resp.status_code == 400
    assert bad_chars_resp.json()["reason"] == "bad_chars"

    # None of those should have charged anything.
    assert session.query(Extraction).count() == 0


def test_idempotency_no_replay_without_header(client: TestClient, session: Session) -> None:
    """Two requests without the header produce two separate charges (no replay)."""
    user, _, plaintext = seed_user(session, balance=EXTRACTION_PUBLIC_CENTS * 3)

    first = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    assert first.status_code == 200
    second = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    assert second.status_code == 200
    assert second.headers.get(IDEMPOTENCY_REPLAYED_HEADER_NAME) is None

    session.expire_all()
    assert _charge_count(session, user.id) == 2
    assert session.query(IdempotencyKey).count() == 0


def test_idempotent_replay_uses_different_keys_independently(
    client: TestClient, session: Session
) -> None:
    """Two different keys + same body: both succeed; ledger holds two charges."""
    user, _, plaintext = seed_user(session, balance=EXTRACTION_PUBLIC_CENTS * 3)

    first = client.post(
        "/v1/extractions",
        headers={**auth_headers(plaintext), IDEMPOTENCY_HEADER_NAME: _GOOD_KEY},
        json={"url": "https://example.com"},
    )
    assert first.status_code == 200

    second = client.post(
        "/v1/extractions",
        headers={**auth_headers(plaintext), IDEMPOTENCY_HEADER_NAME: _GOOD_KEY_ALT},
        json={"url": "https://example.com"},
    )
    assert second.status_code == 200
    assert second.headers.get(IDEMPOTENCY_REPLAYED_HEADER_NAME) is None

    session.expire_all()
    assert _charge_count(session, user.id) == 2
