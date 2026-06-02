"""Tests for the asset_versions library helper.

Covers the dedup contract (same content_hash collapses, distinct urls do
not), the audit field set on a fresh insert, and the dual-write path on
``POST /v1/extractions`` (a fresh extraction row populates
``asset_version_id`` against a row whose dtcg_json matches the bundle).
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.asset_versions import (
    canonicalize_dtcg,
    content_hash_for,
    dtcg_for_extraction,
    insert_or_reuse_asset_version,
)
from app.models import AssetVersion, Extraction
from tests.conftest import auth_headers, seed_user


SAMPLE_DTCG: dict = {
    "schema_version": 1,
    "color": {"brand": {"$value": "#3366cc", "$type": "color"}},
    "fontFamily": {"body": {"$value": "Inter, sans-serif", "$type": "fontFamily"}},
}


def test_canonicalize_dtcg_is_key_order_stable() -> None:
    """Two dicts with the same content but different key insertion order hash equal."""
    a = {"b": 1, "a": 2, "nested": {"y": 3, "x": 4}}
    b = {"nested": {"x": 4, "y": 3}, "a": 2, "b": 1}
    assert canonicalize_dtcg(a) == canonicalize_dtcg(b)
    assert content_hash_for(a) == content_hash_for(b)


def test_content_hash_for_distinguishes_distinct_payloads() -> None:
    """Different DTCG payloads produce different content hashes."""
    a = {"color": {"brand": {"$value": "#000000", "$type": "color"}}}
    b = {"color": {"brand": {"$value": "#ffffff", "$type": "color"}}}
    assert content_hash_for(a) != content_hash_for(b)


def test_asset_version_dedup_by_content_hash(session: Session) -> None:
    """Same ``(url, content_hash)`` reuses the existing asset_versions row."""
    first = insert_or_reuse_asset_version(
        session,
        url="https://example.com",
        dtcg=SAMPLE_DTCG,
        first_extracted_by_user_id=None,
        manifest_schema_version=2,
    )
    second = insert_or_reuse_asset_version(
        session,
        url="https://example.com",
        dtcg=SAMPLE_DTCG,
        first_extracted_by_user_id=None,
        manifest_schema_version=2,
    )
    session.commit()
    assert first.id == second.id
    rows = session.execute(select(AssetVersion)).scalars().all()
    assert len(rows) == 1


def test_asset_version_different_url_same_hash_distinct_rows(session: Session) -> None:
    """``url`` participates in the dedup key; same payload, two urls -> two rows."""
    first = insert_or_reuse_asset_version(
        session,
        url="https://example.com",
        dtcg=SAMPLE_DTCG,
        first_extracted_by_user_id=None,
        manifest_schema_version=2,
    )
    second = insert_or_reuse_asset_version(
        session,
        url="https://other.example.com",
        dtcg=SAMPLE_DTCG,
        first_extracted_by_user_id=None,
        manifest_schema_version=2,
    )
    session.commit()
    assert first.id != second.id
    assert first.content_hash == second.content_hash


def test_first_extracted_by_user_id_audit_set_on_insert(session: Session) -> None:
    """``first_extracted_by_user_id`` records the first writer; subsequent reuse does not overwrite."""
    user, _, _ = seed_user(session, email="first@resemblio.test")
    other, _, _ = seed_user(session, email="second@resemblio.test")
    first = insert_or_reuse_asset_version(
        session,
        url="https://audit.example.com",
        dtcg=SAMPLE_DTCG,
        first_extracted_by_user_id=user.id,
        manifest_schema_version=2,
    )
    session.commit()
    assert first.first_extracted_by_user_id == user.id
    # Second writer reuses the row and the audit field is NOT rewritten.
    second = insert_or_reuse_asset_version(
        session,
        url="https://audit.example.com",
        dtcg=SAMPLE_DTCG,
        first_extracted_by_user_id=other.id,
        manifest_schema_version=2,
    )
    session.commit()
    assert second.id == first.id
    session.refresh(second)
    assert second.first_extracted_by_user_id == user.id


def test_asset_version_defaults_is_public_false(session: Session) -> None:
    """Newly inserted asset_versions rows must be private by default (v1.1 corpus rule)."""
    row = insert_or_reuse_asset_version(
        session,
        url="https://default.example.com",
        dtcg=SAMPLE_DTCG,
        first_extracted_by_user_id=None,
        manifest_schema_version=2,
    )
    session.commit()
    assert row.is_public is False


def test_dtcg_for_extraction_returns_joined_payload(session: Session) -> None:
    """The read helper returns the joined asset_versions payload."""
    user, _, _ = seed_user(session, email="join@resemblio.test")
    asset = insert_or_reuse_asset_version(
        session,
        url="https://join.example.com",
        dtcg=SAMPLE_DTCG,
        first_extracted_by_user_id=user.id,
        manifest_schema_version=2,
    )
    extraction = Extraction(
        user_id=user.id,
        url="https://join.example.com",
        url_normalized="https://join.example.com",
        status="ok",
        schema_version=1,
        credit_cents=500,
        asset_version_id=asset.id,
    )
    session.add(extraction)
    session.commit()
    session.refresh(extraction)
    assert dtcg_for_extraction(extraction) == SAMPLE_DTCG


def test_dtcg_for_extraction_returns_none_when_unlinked(session: Session) -> None:
    """Rows with no asset_version FK (failed or unbackfilled) resolve to None."""
    user, _, _ = seed_user(session, email="legacy@resemblio.test")
    extraction = Extraction(
        user_id=user.id,
        url="https://legacy.example.com",
        url_normalized="https://legacy.example.com",
        status="ok",
        schema_version=1,
        credit_cents=500,
        asset_version_id=None,
    )
    session.add(extraction)
    session.commit()
    session.refresh(extraction)
    assert dtcg_for_extraction(extraction) is None


def test_post_extractions_creates_asset_version_row(client: TestClient, session: Session) -> None:
    """A fresh extraction POST inserts an asset_versions row and points the FK at it."""
    _, _, plaintext = seed_user(session)
    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    assert response.status_code == 200, response.text
    session.expire_all()
    extraction = session.execute(select(Extraction)).scalar_one()
    assert extraction.asset_version_id is not None
    asset = session.execute(
        select(AssetVersion).where(AssetVersion.id == extraction.asset_version_id)
    ).scalar_one()
    # The stored content_hash matches the canonical hash of the persisted DTCG.
    assert asset.content_hash == content_hash_for(asset.dtcg_json)


def test_post_extractions_response_shape_unchanged(client: TestClient, session: Session) -> None:
    """The library refactor must not change the public response envelope."""
    _, _, plaintext = seed_user(session)
    response = client.post(
        "/v1/extractions",
        headers=auth_headers(plaintext),
        json={"url": "https://example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    for field in ("id", "status", "tokens", "dtcg", "schema_version", "tokens_url", "manifest"):
        assert field in body
    assert body["status"] == "ok"
    assert isinstance(body["dtcg"], dict) and body["dtcg"]


def test_two_posts_same_url_collapse_to_one_asset_version(
    client: TestClient, session: Session
) -> None:
    """Two extractions of the same URL with the same DTCG share one asset_versions row."""
    _, _, plaintext = seed_user(session, balance=2000)
    for _ in range(2):
        response = client.post(
            "/v1/extractions",
            headers=auth_headers(plaintext),
            json={"url": "https://dedup.example.com"},
        )
        assert response.status_code == 200, response.text
    session.expire_all()
    extractions = session.execute(select(Extraction)).scalars().all()
    assert len(extractions) == 2
    asset_ids = {e.asset_version_id for e in extractions}
    assert len(asset_ids) == 1
    asset_rows = session.execute(select(AssetVersion)).scalars().all()
    assert len(asset_rows) == 1
