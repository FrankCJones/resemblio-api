"""Contract tests for the v1.1 R2 response-shape reconciliation.

Covers the additive fields landed in the R2 dispatch (per
`projects/OptSus Team/drafts/2026-05-28-resemblio-next-steps.md`):

  - `schema_version` on the response is bumped to `SCHEMA_V1_1` (=2)
  - `tokens_url` is a presigned URL to the sibling tokens.json object
  - `manifest` envelope carries id, status, source_url, created_at_utc,
    schema_version, quality_score, tokens_url, download_url
  - tokens.json is uploaded under the canonical key on POST success
  - GET on a cached row returns the same shape without re-charging
  - Backward compat: legacy `tokens`, `dtcg`, `download_url`, `schema_version`
    fields remain populated

All tests run against the in-memory SQLite DB + FakeStorage fixtures defined
in `tests/conftest.py`. No network. Pure-data assertions plus one route round
trip per behavior.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import SCHEMA_V1_1, TOKENS_URL_TTL_SECONDS
from app.models import Extraction
from tests.conftest import FakeStorage, auth_headers, seed_user


def test_post_response_carries_v1_1_schema_version(
    client: TestClient,
    session: Session,
) -> None:
    """POST /v1/extractions returns `schema_version=2` on the v1.1 shape."""
    _, _, plaintext = seed_user(session)
    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == SCHEMA_V1_1


def test_post_response_includes_signed_tokens_url(
    client: TestClient,
    session: Session,
    fake_storage: FakeStorage,
) -> None:
    """`tokens_url` is signed and points to the canonical tokens key."""
    _, _, plaintext = seed_user(session)
    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    body = response.json()
    extraction = session.query(Extraction).one()
    expected_key = f"tokens/{extraction.user_id}/{extraction.id}.json"
    assert body["tokens_url"] is not None
    assert expected_key in body["tokens_url"]
    # Signed URL should advertise the long TTL constant; FakeStorage encodes
    # this verbatim in the URL string for assertion.
    assert str(TOKENS_URL_TTL_SECONDS) in body["tokens_url"]
    # The tokens.json object actually landed in storage so the signed URL
    # would resolve in production.
    assert expected_key in fake_storage.objects
    stored = json.loads(fake_storage.objects[expected_key].decode("utf-8"))
    assert stored == extraction.tokens_json


def test_post_response_includes_manifest_envelope(
    client: TestClient,
    session: Session,
) -> None:
    """`manifest` carries the canonical pointer fields per the v1.1 brief."""
    _, _, plaintext = seed_user(session)
    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    body = response.json()
    manifest = body["manifest"]
    assert manifest is not None
    extraction = session.query(Extraction).one()
    assert manifest["id"] == extraction.id
    assert manifest["status"] == "ok"
    # Pydantic's AnyHttpUrl appends a trailing slash for bare-host URLs; the
    # row's `url` column carries the normalized value verbatim. We assert on
    # the persisted shape rather than the request literal.
    assert manifest["source_url"] == extraction.url
    assert manifest["created_at_utc"]  # ISO-serialized datetime
    assert manifest["schema_version"] == SCHEMA_V1_1
    assert manifest["tokens_url"] == body["tokens_url"]
    assert manifest["download_url"] == body["download_url"]


def test_post_response_preserves_legacy_v1_fields(
    client: TestClient,
    session: Session,
) -> None:
    """Backward compat: pre-v1.1 fields remain on the response."""
    _, _, plaintext = seed_user(session)
    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    body = response.json()
    # Legacy clients pinned to v1 keep working.
    assert "tokens" in body and body["tokens"]
    assert "dtcg" in body and body["dtcg"]
    assert body["download_url"] is not None
    assert body["download_url"].startswith("https://r2.test/extractions/")


def test_get_cached_extraction_returns_same_v1_1_shape(
    client: TestClient,
    session: Session,
) -> None:
    """GET on a cached row returns the v1.1 envelope without re-charging."""
    _, _, plaintext = seed_user(session)
    created = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    extraction_id = created.json()["id"]
    fetched = client.get(
        f"/v1/extractions/{extraction_id}",
        headers=auth_headers(plaintext),
    )
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["schema_version"] == SCHEMA_V1_1
    assert body["tokens_url"] is not None
    assert body["manifest"] is not None
    assert body["manifest"]["id"] == extraction_id
    assert body["manifest"]["schema_version"] == SCHEMA_V1_1


def test_storage_sign_tokens_url_uses_long_ttl_by_default() -> None:
    """`sign_tokens_url` defaults to the 24h TTL per the v1.1 brief."""
    from app.storage import R2Storage  # imported here to avoid moto import at module load

    # We assert on the constant, not on a live S3 client, because TTL choice
    # is the contract; the signing call itself is covered by `test_storage`.
    import inspect

    signature = inspect.signature(R2Storage.sign_tokens_url)
    default = signature.parameters["expires_in"].default
    assert default == TOKENS_URL_TTL_SECONDS
    assert default == 24 * 60 * 60
