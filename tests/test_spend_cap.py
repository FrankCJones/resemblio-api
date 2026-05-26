"""Tests for per-key spend-cap enforcement."""
from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import utcnow
from app.models import CreditLedger
from tests.conftest import auth_headers, seed_user


def test_spend_cap_allows_charge_at_cap(client: TestClient, session: Session) -> None:
    """The cap blocks only when spent plus required cents exceeds the cap."""
    user, api_key, plaintext = seed_user(session, balance=3000)
    api_key.spend_cap_cents = 1000
    _add_charge(session, user.id, api_key.id, -500)
    session.commit()
    response = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    assert response.status_code == 200


def test_spend_cap_blocks_when_next_charge_exceeds_cap(client: TestClient, session: Session) -> None:
    """A charge that would push trailing spend over the cap returns 402."""
    user, api_key, plaintext = seed_user(session, balance=3000)
    api_key.spend_cap_cents = 999
    _add_charge(session, user.id, api_key.id, -500)
    session.commit()
    response = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    assert response.status_code == 402
    assert response.json() == {"error": "spend_cap_exceeded", "cap_cents": 999, "spent_cents": 500, "window_days": 30}


def test_null_spend_cap_allows_charge(client: TestClient, session: Session) -> None:
    """A null spend cap means no per-key cap is enforced."""
    user, api_key, plaintext = seed_user(session, balance=3000)
    _add_charge(session, user.id, api_key.id, -2500)
    session.commit()
    response = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    assert response.status_code == 200


def test_spend_cap_ignores_entries_outside_rolling_window(client: TestClient, session: Session) -> None:
    """Old debits outside the trailing 30-day window do not count."""
    user, api_key, plaintext = seed_user(session, balance=3000)
    api_key.spend_cap_cents = 500
    _add_charge(session, user.id, api_key.id, -1000, created_days_ago=31)
    session.commit()
    response = client.post("/v1/extractions", headers=auth_headers(plaintext), json={"url": "https://example.com"})
    assert response.status_code == 200


def _add_charge(session: Session, user_id: int, api_key_id: int, amount_cents: int, created_days_ago: int = 0) -> None:
    """Append a synthetic charge for spend-cap tests."""
    session.add(
        CreditLedger(
            user_id=user_id,
            entry_type="extraction_charge",
            amount_cents=amount_cents,
            balance_after_cents=0,
            api_key_id=api_key_id,
            note="Synthetic cap spend",
            created_at=utcnow() - timedelta(days=created_days_ago),
        )
    )
