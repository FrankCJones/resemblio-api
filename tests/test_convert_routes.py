"""Tests for ``POST /v1/convert/{target}/{extraction_id}``.

Covers ownership-scoped 404, missing-token 401, and the happy-path conversion
for both shadcn and figma targets. Conversion is FREE in v1 (no ledger debit
assertion needed because there is no debit code path to exercise).
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import CONVERT_RESPONSE_SCHEMA_VERSION
from app.models import Extraction
from tests.conftest import auth_headers, seed_user


# A small but realistic DTCG manifest. The shadcn converter exercises the
# saturated-vs-neutral classifier (#3366cc + #ff3366 are saturated; #f5f5f5
# and #111111 are neutrals at opposite lightness ends); the figma converter
# routes the color group plus the radius dimension.
SAMPLE_DTCG: dict = {
    "schema_version": 1,
    "color": {
        "brand-primary": {"$value": "#3366cc", "$type": "color"},
        "brand-accent": {"$value": "#ff3366", "$type": "color"},
        "surface": {"$value": "#f5f5f5", "$type": "color"},
        "ink": {"$value": "#111111", "$type": "color"},
    },
    "fontFamily": {
        "body": {"$value": "Inter, sans-serif", "$type": "fontFamily"},
    },
    "dimension": {
        "radius-md": {"$value": "8px", "$type": "dimension"},
    },
}


def _seed_extraction_with_dtcg(
    session: Session, user_id: int, dtcg: dict | None = None
) -> Extraction:
    """Insert an ``ok``-status extraction row carrying a DTCG manifest.

    The convert routes only need ``user_id`` + ``dtcg_json`` on the row;
    every other column is bypassed by these endpoints, so we set the
    bare-minimum fields the model column NOT NULL constraints require.
    """
    extraction = Extraction(
        user_id=user_id,
        url="https://example.com",
        url_normalized="https://example.com",
        status="ok",
        schema_version=1,
        credit_cents=500,
        dtcg_json=dtcg if dtcg is not None else SAMPLE_DTCG,
    )
    session.add(extraction)
    session.commit()
    session.refresh(extraction)
    return extraction


def test_convert_shadcn_happy_path(client: TestClient, session: Session) -> None:
    """Auth'd owner gets 200 with shadcn theme keys + rendered artifacts."""
    user, _, plaintext = seed_user(session)
    extraction = _seed_extraction_with_dtcg(session, user.id)
    response = client.post(
        f"/v1/convert/shadcn/{extraction.id}", headers=auth_headers(plaintext)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == CONVERT_RESPONSE_SCHEMA_VERSION
    assert body["extraction_id"] == extraction.id
    assert body["target"] == "shadcn"
    payload = body["payload"]
    # Light and dark color blocks present plus the resolved typography/radius.
    assert "light" in payload and "dark" in payload
    assert "background" in payload["light"]
    assert "primary" in payload["dark"]
    assert isinstance(payload["radius_rem"], float)
    rendered = body["rendered"]
    assert ":root {" in rendered["globals_css"]
    assert ".dark {" in rendered["globals_css"]
    assert "tailwindcss" in rendered["tailwind_config_excerpt"]


def test_convert_shadcn_other_users_extraction_returns_404(
    client: TestClient, session: Session
) -> None:
    """Owner-scoped lookup hides another user's extraction id behind a 404."""
    other, _, _ = seed_user(session, email="other@resemblio.com")
    extraction = _seed_extraction_with_dtcg(session, other.id)
    _, _, plaintext = seed_user(session, email="caller@resemblio.com")
    response = client.post(
        f"/v1/convert/shadcn/{extraction.id}", headers=auth_headers(plaintext)
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_convert_shadcn_nonexistent_extraction_returns_404(
    client: TestClient, session: Session
) -> None:
    """Missing extraction id surfaces as a plain 404."""
    _, _, plaintext = seed_user(session)
    response = client.post(
        "/v1/convert/shadcn/9999999", headers=auth_headers(plaintext)
    )
    assert response.status_code == 404


def test_convert_figma_happy_path(client: TestClient, session: Session) -> None:
    """Auth'd owner gets 200 with Figma collections + variables in payload."""
    user, _, plaintext = seed_user(session)
    extraction = _seed_extraction_with_dtcg(session, user.id)
    response = client.post(
        f"/v1/convert/figma/{extraction.id}", headers=auth_headers(plaintext)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == CONVERT_RESPONSE_SCHEMA_VERSION
    assert body["extraction_id"] == extraction.id
    assert body["target"] == "figma"
    payload = body["payload"]
    assert isinstance(payload["collections"], list)
    assert isinstance(payload["variables"], list)
    # Color group plus dimension group should each produce a collection with
    # at least one variable. The sample manifest exercises both.
    collection_names = {c["name"] for c in payload["collections"]}
    assert "Colors" in collection_names
    assert len(payload["variables"]) >= len(SAMPLE_DTCG["color"])
    # Figma target carries no rendered block.
    assert body["rendered"] is None


def test_convert_figma_other_users_extraction_returns_404(
    client: TestClient, session: Session
) -> None:
    """Same ownership scoping as shadcn endpoint."""
    other, _, _ = seed_user(session, email="other@resemblio.com")
    extraction = _seed_extraction_with_dtcg(session, other.id)
    _, _, plaintext = seed_user(session, email="caller@resemblio.com")
    response = client.post(
        f"/v1/convert/figma/{extraction.id}", headers=auth_headers(plaintext)
    )
    assert response.status_code == 404


def test_convert_requires_auth(client: TestClient, session: Session) -> None:
    """Missing bearer token is rejected by AuthMiddleware before the route runs."""
    user, _, _ = seed_user(session)
    extraction = _seed_extraction_with_dtcg(session, user.id)
    response = client.post(f"/v1/convert/shadcn/{extraction.id}")
    assert response.status_code == 401
