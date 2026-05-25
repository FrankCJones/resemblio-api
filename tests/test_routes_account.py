"""Tests for account and credit balance routes."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import auth_headers, seed_user


def test_account_and_credit_balance(client: TestClient, session: Session) -> None:
    """Account routes return current user metadata and computed balance."""
    _, _, plaintext = seed_user(session)
    account = client.get("/v1/account", headers=auth_headers(plaintext))
    balance = client.get("/v1/credit/balance", headers=auth_headers(plaintext))
    assert account.status_code == 200
    assert account.json()["email"] == "frank@optsus.com"
    assert balance.json()["balance_cents"] == 1000
    assert balance.json()["last_entry_at"] is not None

