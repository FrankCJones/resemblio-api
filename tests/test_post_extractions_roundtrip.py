"""Gated live integration test for POST /v1/extractions."""
from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import extractor_bridge
from app.main import app
from app.models import CreditLedger, Extraction
from app.storage import get_storage
from tests.conftest import FakeStorage, auth_headers, seed_user

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RESEMBLIO_INTEGRATION_TESTS") != "1",
        reason="set RESEMBLIO_INTEGRATION_TESTS=1 to run live extraction integration",
    ),
]


def test_post_extractions_roundtrip_live(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    fake_storage: FakeStorage,
) -> None:
    """Run the real extractor through the FastAPI route against SQLite."""
    monkeypatch.setattr(extractor_bridge, "_load_extractor", extractor_bridge._load_real_extractor)
    app.dependency_overrides[get_storage] = lambda: fake_storage
    _user, _api_key, plaintext = seed_user(session)

    with TestClient(app) as client:
        response = client.post(
            "/v1/extractions",
            headers=auth_headers(plaintext),
            json={"url": "https://posthog.com"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"]
    assert body["status"] == "ok"
    assert isinstance(body["tokens"], dict) and body["tokens"]
    assert _has_dtcg_leaf(body["dtcg"])
    assert isinstance(body["download_url"], str) and body["download_url"].startswith("https://r2.test/")

    session.expire_all()
    charge = session.query(CreditLedger).filter(CreditLedger.entry_type == "extraction_charge").one()
    assert charge.amount_cents == -500
    extraction = session.query(Extraction).one()
    assert extraction.status == "ok"
    assert extraction.tokens_json
    assert extraction.dtcg_json
    assert extraction.r2_zip_key


def _has_dtcg_leaf(payload: dict[str, Any]) -> bool:
    """Return whether the response has a color or dimension token leaf."""
    for group_name in ("color", "dimension"):
        group = payload.get(group_name)
        if not isinstance(group, dict):
            continue
        for leaf in group.values():
            if isinstance(leaf, dict) and "$value" in leaf and "$type" in leaf:
                return True
    return False
