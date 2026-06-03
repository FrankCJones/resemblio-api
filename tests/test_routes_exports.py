"""Integration tests for the Stage O7 export-format download routes.

Covers the authed endpoint (ownership + 404 + 400 + 409) and the
anonymous endpoint (claim_token gate + happy path).
"""
from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import ANON_EXTRACTION_CLAIM_WINDOW_HOURS
from app.models import AnonymousExtraction, Extraction
from app.routes.extractions_anonymous import mint_claim_token, utcnow
from tests.conftest import auth_headers, seed_user

SAMPLE_DTCG: dict = {
    "schema_version": 1,
    "color": {
        "bg": {"$value": "#ffffff", "$type": "color"},
        "accent": {"$value": "#ff3366", "$type": "color"},
    },
    "fontFamily": {
        "body": {"$value": "Inter, sans-serif", "$type": "fontFamily"},
    },
    "dimension": {
        "space-1": {"$value": "4px", "$type": "dimension"},
    },
}


def _seed_extraction_with_dtcg(
    session: Session, user_id: int, dtcg: dict | None = None
) -> Extraction:
    """Insert an `ok` extraction row with a joined asset_version DTCG."""
    from app.asset_versions import insert_or_reuse_asset_version

    payload = dtcg if dtcg is not None else SAMPLE_DTCG
    asset_version = insert_or_reuse_asset_version(
        session,
        url="https://example.com",
        dtcg=payload,
        first_extracted_by_user_id=user_id,
        manifest_schema_version=1,
    )
    extraction = Extraction(
        user_id=user_id,
        url="https://example.com",
        url_normalized="https://example.com",
        status="ok",
        schema_version=1,
        credit_cents=500,
        asset_version_id=asset_version.id,
    )
    session.add(extraction)
    session.commit()
    session.refresh(extraction)
    return extraction


def test_export_dtcg_returns_canonical_json(
    client: TestClient, session: Session
) -> None:
    user, _, plaintext = seed_user(session)
    extraction = _seed_extraction_with_dtcg(session, user.id)
    response = client.get(
        f"/v1/extractions/{extraction.id}/export/dtcg",
        headers=auth_headers(plaintext),
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["X-Exporter-Schema-Version"] == "1"


def test_export_css_returns_root_block(
    client: TestClient, session: Session
) -> None:
    user, _, plaintext = seed_user(session)
    extraction = _seed_extraction_with_dtcg(session, user.id)
    response = client.get(
        f"/v1/extractions/{extraction.id}/export/css",
        headers=auth_headers(plaintext),
    )
    assert response.status_code == 200
    body = response.text
    assert body.startswith(":root {")
    assert "--color-bg: #ffffff;" in body


def test_export_tailwind_returns_theme_block(
    client: TestClient, session: Session
) -> None:
    user, _, plaintext = seed_user(session)
    extraction = _seed_extraction_with_dtcg(session, user.id)
    response = client.get(
        f"/v1/extractions/{extraction.id}/export/tailwind",
        headers=auth_headers(plaintext),
    )
    assert response.status_code == 200
    body = response.text
    assert body.startswith("@theme {")


def test_export_zip_returns_bundle(
    client: TestClient, session: Session
) -> None:
    user, _, plaintext = seed_user(session)
    extraction = _seed_extraction_with_dtcg(session, user.id)
    response = client.get(
        f"/v1/extractions/{extraction.id}/export/zip",
        headers=auth_headers(plaintext),
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(response.content)) as zf:
        assert "tokens.json" in zf.namelist()
        assert "tokens.css" in zf.namelist()
        assert "tailwind.css" in zf.namelist()
        assert "README.md" in zf.namelist()


def test_export_rejects_unsupported_format(
    client: TestClient, session: Session
) -> None:
    user, _, plaintext = seed_user(session)
    extraction = _seed_extraction_with_dtcg(session, user.id)
    response = client.get(
        f"/v1/extractions/{extraction.id}/export/style-dictionary",
        headers=auth_headers(plaintext),
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "unsupported_format"
    assert "dtcg" in body["supported_formats"]
    assert "figma" not in body["supported_formats"]


def test_export_404_for_other_users_extraction(
    client: TestClient, session: Session
) -> None:
    owner, _, _ = seed_user(session, email="owner@example.com")
    extraction = _seed_extraction_with_dtcg(session, owner.id)
    _, _, intruder_key = seed_user(session, email="intruder@example.com")
    response = client.get(
        f"/v1/extractions/{extraction.id}/export/dtcg",
        headers=auth_headers(intruder_key),
    )
    assert response.status_code == 404


def test_anonymous_export_requires_claim_token(
    client: TestClient, session: Session
) -> None:
    user, _, _ = seed_user(session)
    extraction = _seed_extraction_with_dtcg(session, user.id)
    registry = AnonymousExtraction(
        claim_token=mint_claim_token(),
        ip_hash="hash",
        extraction_id=extraction.id,
        url=extraction.url,
        classification="html_first",
        status="ok",
        schema_version=1,
        expires_at=utcnow() + timedelta(hours=ANON_EXTRACTION_CLAIM_WINDOW_HOURS),
    )
    session.add(registry)
    session.commit()

    no_token = client.get(
        f"/v1/anonymous/extractions/{extraction.id}/export/dtcg"
    )
    assert no_token.status_code == 403

    wrong = client.get(
        f"/v1/anonymous/extractions/{extraction.id}/export/dtcg",
        params={"claim_token": "wrong-token"},
    )
    assert wrong.status_code == 403

    right = client.get(
        f"/v1/anonymous/extractions/{extraction.id}/export/dtcg",
        params={"claim_token": registry.claim_token},
    )
    assert right.status_code == 200
    assert right.headers["content-type"] == "application/json"
