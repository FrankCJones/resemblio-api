"""Tests for the GET /v1/credit/ledger endpoint (S3b Wave 2b prereq).

Covers the contract documented in the Wave 2 architecture pass
(`projects/Resemblio/02-prd/2026-06-01-S3b-Wave2-architecture.md`):
offset pagination, newest-first ordering, per-user isolation, the
`schema_version=2` envelope, and the deliberate exclusion of the
internal-only `stripe_payment_intent_id` / `api_key_id` fields.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import CreditLedger
from app.routes.account import CREDIT_LEDGER_MAX_LIMIT
from tests.conftest import auth_headers, seed_user


def _add_ledger_entries(
    session: Session,
    user_id: int,
    count: int,
    *,
    base_balance: int = 1000,
    started_at: datetime | None = None,
) -> None:
    """Insert ``count`` synthetic ledger entries with increasing timestamps.

    Default `started_at` is a future point so synthetic topups are always newer
    than the seed_user fixture's onboarding_grant (which is created at utcnow()).
    Using a fixed past date made `topup` lose the newest-first ordering once the
    real clock advanced past it.
    """
    started_at = started_at or (datetime.now(timezone.utc) + timedelta(hours=1))
    running_balance = base_balance
    for i in range(count):
        running_balance += 100
        session.add(
            CreditLedger(
                user_id=user_id,
                entry_type="topup",
                amount_cents=100,
                balance_after_cents=running_balance,
                stripe_payment_intent_id=f"pi_test_{i}",
                note=f"entry-{i}",
                created_at=started_at + timedelta(minutes=i + 1),
            )
        )
    session.commit()


def test_credit_ledger_happy_path(client: TestClient, session: Session) -> None:
    """Authed user gets their ledger newest-first with the seeded grant."""
    user, _, plaintext = seed_user(session)
    _add_ledger_entries(session, user.id, 3)

    response = client.get("/v1/credit/ledger", headers=auth_headers(plaintext))

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 2
    assert body["total"] == 4  # 1 onboarding_grant + 3 topups
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert len(body["items"]) == 4
    # Newest first.
    assert body["items"][0]["entry_type"] == "topup"
    assert body["items"][0]["note"] == "entry-2"
    assert body["items"][-1]["entry_type"] == "onboarding_grant"


def test_credit_ledger_pagination(client: TestClient, session: Session) -> None:
    """limit + offset clamp + paginate; rows do not overlap across pages."""
    user, _, plaintext = seed_user(session, balance=0)
    _add_ledger_entries(session, user.id, 25, base_balance=0)

    page_one = client.get(
        "/v1/credit/ledger?limit=10&offset=0",
        headers=auth_headers(plaintext),
    )
    page_two = client.get(
        "/v1/credit/ledger?limit=10&offset=10",
        headers=auth_headers(plaintext),
    )
    tail = client.get(
        "/v1/credit/ledger?limit=10&offset=20",
        headers=auth_headers(plaintext),
    )
    over_max = client.get(
        f"/v1/credit/ledger?limit={CREDIT_LEDGER_MAX_LIMIT + 50}",
        headers=auth_headers(plaintext),
    )

    assert page_one.status_code == 200
    assert page_two.status_code == 200
    assert tail.status_code == 200
    one_body = page_one.json()
    two_body = page_two.json()
    tail_body = tail.json()
    assert one_body["total"] == two_body["total"] == 25
    assert one_body["limit"] == 10
    assert one_body["offset"] == 0
    assert two_body["offset"] == 10
    assert len(one_body["items"]) == 10
    assert len(two_body["items"]) == 10
    assert len(tail_body["items"]) == 5
    one_ids = {row["id"] for row in one_body["items"]}
    two_ids = {row["id"] for row in two_body["items"]}
    assert one_ids.isdisjoint(two_ids)
    assert over_max.json()["limit"] == CREDIT_LEDGER_MAX_LIMIT


def test_credit_ledger_isolates_users(client: TestClient, session: Session) -> None:
    """User A cannot see user B's entries; counts and ids are user-scoped."""
    user_a, _, plaintext_a = seed_user(session, email="a@example.com")
    user_b, _, plaintext_b = seed_user(session, email="b@example.com")
    _add_ledger_entries(session, user_a.id, 4)
    _add_ledger_entries(session, user_b.id, 2)

    resp_a = client.get("/v1/credit/ledger", headers=auth_headers(plaintext_a))
    resp_b = client.get("/v1/credit/ledger", headers=auth_headers(plaintext_b))

    body_a = resp_a.json()
    body_b = resp_b.json()
    assert body_a["total"] == 5  # 1 grant + 4 topups
    assert body_b["total"] == 3  # 1 grant + 2 topups
    a_ids = {row["id"] for row in body_a["items"]}
    b_ids = {row["id"] for row in body_b["items"]}
    assert a_ids.isdisjoint(b_ids)


def test_credit_ledger_empty(client: TestClient, session: Session) -> None:
    """A user with no ledger entries gets an empty items array and total=0."""
    _, _, plaintext = seed_user(session, balance=0)

    response = client.get("/v1/credit/ledger", headers=auth_headers(plaintext))

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["schema_version"] == 2
    assert body["limit"] == 20
    assert body["offset"] == 0


def test_credit_ledger_response_shape(client: TestClient, session: Session) -> None:
    """Response carries the v1.1 envelope; internal-only fields are absent."""
    user, _, plaintext = seed_user(session)
    _add_ledger_entries(session, user.id, 1)

    response = client.get("/v1/credit/ledger", headers=auth_headers(plaintext))

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"items", "total", "limit", "offset", "schema_version"}
    assert body["schema_version"] == 2
    first = body["items"][0]
    assert set(first.keys()) == {
        "id",
        "entry_type",
        "amount_cents",
        "balance_after_cents",
        "extraction_id",
        "note",
        "created_at",
    }
    assert "stripe_payment_intent_id" not in first
    assert "api_key_id" not in first


def test_credit_ledger_requires_auth(client: TestClient, session: Session) -> None:
    """An unauthenticated request is rejected; ledger is not public."""
    _, _, _ = seed_user(session)
    response = client.get("/v1/credit/ledger")
    assert response.status_code in (401, 403)
