"""Tests for the public-corpus library read API.

Covers the 7 endpoints in ``app/routes/library.py`` that back the Next.js
``/library/...`` route tree. The fixtures build ``library_pages`` rows
directly (rather than running the indexer) so tests stay fast and decoupled
from the DRL compose pipeline; the indexer's own tests in
``test_library_indexer.py`` cover the write path.

Coverage:

- happy-path response shape per endpoint (envelope + data)
- quality gate enforced (is_public=False rows hidden)
- 404 on unknown brand, unknown category, unknown version, unknown asset
- 404 on malformed slug / reserved brand slug
- 404 on malformed version label
- pagination on hub list (page/page_size/total)
- pagination ceiling rejected with 422
- sitemap emits every public route
- auth-free (no Authorization header required)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import SCHEMA_V1, SCHEMA_V1_1
from app.models import AssetVersion, LibraryPage


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _make_asset_version(
    session: Session,
    *,
    url: str = "https://stripe.com/",
    is_public: bool = True,
    version_label: str | None = "2026-06",
    fetched_at: datetime | None = None,
) -> AssetVersion:
    """Insert an asset_versions row with a minimal DTCG payload."""
    av = AssetVersion(
        url=url,
        content_hash=f"hash-{url}-{version_label}-{fetched_at!s}",
        dtcg_json={"tokens": {"bg": "#fff", "accent": "#f00"}},
        manifest_schema_version=SCHEMA_V1,
        is_public=is_public,
        version_label=version_label,
        fetched_at=fetched_at or datetime.now(timezone.utc),
    )
    session.add(av)
    session.flush()
    return av


def _make_page(
    session: Session,
    av: AssetVersion,
    *,
    brand_slug: str,
    category_slug: str,
    is_canonical: bool = True,
    rendered_html: str = "<p>hi</p>",
) -> LibraryPage:
    """Insert one library_pages row for the given asset_version."""
    page = LibraryPage(
        asset_version_id=av.id,
        category_slug=category_slug,
        brand_slug=brand_slug,
        version_label=av.version_label,
        rendered_html=rendered_html,
        metadata_json={"schema_version": 1, "brand_slug": brand_slug,
                       "category_slug": category_slug},
        is_canonical=is_canonical,
    )
    session.add(page)
    session.flush()
    return page


def _seed_basic_corpus(session: Session) -> None:
    """Two brands, two categories each, one version. Useful for most tests."""
    av_stripe = _make_asset_version(session, url="https://stripe.com/",
                                    version_label="2026-06")
    _make_page(session, av_stripe, brand_slug="stripe-com",
               category_slug="buttons", is_canonical=True)
    _make_page(session, av_stripe, brand_slug="stripe-com",
               category_slug="palette", is_canonical=True)
    av_linear = _make_asset_version(session, url="https://linear.app/",
                                    version_label="2026-06",
                                    fetched_at=datetime.now(timezone.utc) - timedelta(hours=1))
    _make_page(session, av_linear, brand_slug="linear-app",
               category_slug="buttons", is_canonical=True)
    _make_page(session, av_linear, brand_slug="linear-app",
               category_slug="navigation", is_canonical=True)
    session.commit()


# ----------------------------------------------------------------------
# Hub list
# ----------------------------------------------------------------------


def test_brands_hub_returns_envelope_and_paginates(
    client: TestClient, session: Session
) -> None:
    """Hub list returns the standard envelope plus pagination metadata."""
    _seed_basic_corpus(session)
    resp = client.get("/v1/library/brands")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control", "").startswith("public")
    body = resp.json()
    assert body["schema_version"] == SCHEMA_V1_1
    data = body["data"]
    assert data["schema_version"] == "library_data_v1"
    assert data["page"] == 1
    assert data["page_size"] == 25
    assert data["total"] == 2
    slugs = sorted(row["brand_slug"] for row in data["featured"])
    assert slugs == ["linear-app", "stripe-com"]
    counts = {row["brand_slug"]: row["category_count"] for row in data["featured"]}
    assert counts == {"linear-app": 2, "stripe-com": 2}
    for row in data["featured"]:
        assert row["source_url"].startswith("https://")


def test_brands_hub_page_size_clamped(client: TestClient, session: Session) -> None:
    """Pagination ceiling: page_size > 100 is rejected with 422."""
    _seed_basic_corpus(session)
    resp = client.get("/v1/library/brands?page_size=101")
    assert resp.status_code == 422


def test_brands_hub_pagination_walks_pages(
    client: TestClient, session: Session
) -> None:
    """Page 2 with page_size=1 returns the second brand."""
    _seed_basic_corpus(session)
    resp = client.get("/v1/library/brands?page=2&page_size=1")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["page"] == 2
    assert data["page_size"] == 1
    assert data["total"] == 2
    assert len(data["featured"]) == 1


def test_brands_hub_quality_gate_hides_non_public_rows(
    client: TestClient, session: Session
) -> None:
    """asset_versions.is_public=False removes the brand from the hub list."""
    av = _make_asset_version(session, url="https://hidden.example/",
                             is_public=False, version_label="2026-06")
    _make_page(session, av, brand_slug="hidden-example",
               category_slug="buttons", is_canonical=True)
    session.commit()
    resp = client.get("/v1/library/brands")
    assert resp.status_code == 200
    slugs = [row["brand_slug"] for row in resp.json()["data"]["featured"]]
    assert "hidden-example" not in slugs


# ----------------------------------------------------------------------
# Brand canonical
# ----------------------------------------------------------------------


def test_brand_canonical_returns_page(client: TestClient, session: Session) -> None:
    """Canonical brand page returns the page payload with is_canonical=True."""
    _seed_basic_corpus(session)
    resp = client.get("/v1/library/brands/stripe-com")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["brand_slug"] == "stripe-com"
    assert data["is_canonical"] is True
    assert data["is_version_snapshot"] is False
    assert data["category_slug"] is None
    assert data["rendered_html"]
    assert isinstance(data["related"], list)


def test_brand_canonical_404_on_unknown_brand(
    client: TestClient, session: Session
) -> None:
    """Unknown brand_slug returns 404."""
    _seed_basic_corpus(session)
    resp = client.get("/v1/library/brands/does-not-exist")
    assert resp.status_code == 404


def test_brand_canonical_404_on_reserved_slug(client: TestClient) -> None:
    """Reserved top-level slugs (e.g. 'admin') return 404 without a DB query."""
    resp = client.get("/v1/library/brands/admin")
    assert resp.status_code == 404


def test_brand_canonical_hides_non_public(
    client: TestClient, session: Session
) -> None:
    """Quality gate: is_public=False rows are not returned."""
    av = _make_asset_version(session, url="https://hidden.example/",
                             is_public=False, version_label="2026-06")
    _make_page(session, av, brand_slug="hidden-example",
               category_slug="buttons", is_canonical=True)
    session.commit()
    resp = client.get("/v1/library/brands/hidden-example")
    assert resp.status_code == 404


# ----------------------------------------------------------------------
# Brand version snapshot
# ----------------------------------------------------------------------


def test_brand_version_snapshot_happy_path(
    client: TestClient, session: Session
) -> None:
    """Version snapshot returns is_version_snapshot=True for known version."""
    _seed_basic_corpus(session)
    resp = client.get("/v1/library/brands/stripe-com/versions/2026-06")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["version_label"] == "2026-06"
    assert data["is_version_snapshot"] is True


def test_brand_version_snapshot_404_on_unknown_version(
    client: TestClient, session: Session
) -> None:
    """Unknown version label returns 404."""
    _seed_basic_corpus(session)
    resp = client.get("/v1/library/brands/stripe-com/versions/2025-01")
    assert resp.status_code == 404


def test_brand_version_snapshot_404_on_malformed_version(
    client: TestClient, session: Session
) -> None:
    """Malformed version label (not YYYY-MM) returns 404 without a DB hit."""
    _seed_basic_corpus(session)
    resp = client.get("/v1/library/brands/stripe-com/versions/latest")
    assert resp.status_code == 404


# ----------------------------------------------------------------------
# Category routes
# ----------------------------------------------------------------------


def test_brand_category_canonical_happy_path(
    client: TestClient, session: Session
) -> None:
    """Category canonical returns the matching category page."""
    _seed_basic_corpus(session)
    resp = client.get("/v1/library/brands/stripe-com/categories/buttons")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["category_slug"] == "buttons"
    assert data["category_label"] == "Buttons"
    assert data["category_kind"] == "components"
    assert data["is_canonical"] is True


def test_brand_category_canonical_404_on_unknown_category(
    client: TestClient, session: Session
) -> None:
    """Unknown category returns 404."""
    _seed_basic_corpus(session)
    resp = client.get("/v1/library/brands/stripe-com/categories/hero")
    assert resp.status_code == 404


def test_brand_category_version_happy_path(
    client: TestClient, session: Session
) -> None:
    """Scoped category+version returns is_version_snapshot=True."""
    _seed_basic_corpus(session)
    resp = client.get(
        "/v1/library/brands/stripe-com/categories/buttons/2026-06"
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["category_slug"] == "buttons"
    assert data["version_label"] == "2026-06"
    assert data["is_version_snapshot"] is True


def test_brand_category_version_asset_happy_path(
    client: TestClient, session: Session
) -> None:
    """Asset-instance route returns the page payload with asset_id populated."""
    _seed_basic_corpus(session)
    resp = client.get(
        "/v1/library/brands/stripe-com/categories/buttons/2026-06/primary-default"
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["asset_id"] == "primary-default"
    assert data["category_slug"] == "buttons"
    assert data["version_label"] == "2026-06"


def test_brand_category_version_asset_404_on_malformed_asset_id(
    client: TestClient, session: Session
) -> None:
    """Malformed asset id (contains an underscore) returns 404."""
    _seed_basic_corpus(session)
    resp = client.get(
        "/v1/library/brands/stripe-com/categories/buttons/2026-06/BAD ID!"
    )
    # Starlette will percent-decode the space; the asset-id regex rejects it.
    assert resp.status_code == 404


# ----------------------------------------------------------------------
# Sitemap
# ----------------------------------------------------------------------


def test_sitemap_returns_every_public_path(
    client: TestClient, session: Session
) -> None:
    """Sitemap emits the hub root + per-brand + per-category + per-version paths."""
    _seed_basic_corpus(session)
    resp = client.get("/v1/library/sitemap")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == SCHEMA_V1_1
    paths = {row["path"] for row in body["data"]["entries"]}
    assert "/library/" in paths
    assert "/library/stripe-com/" in paths
    assert "/library/stripe-com/buttons/" in paths
    assert "/library/stripe-com/2026-06/" in paths
    assert "/library/stripe-com/buttons/2026-06/" in paths
    assert "/library/linear-app/" in paths
    assert body["data"]["total"] == len(body["data"]["entries"])


def test_sitemap_omits_non_public_rows(
    client: TestClient, session: Session
) -> None:
    """asset_versions.is_public=False rows are excluded from the sitemap."""
    av = _make_asset_version(session, url="https://hidden.example/",
                             is_public=False, version_label="2026-06")
    _make_page(session, av, brand_slug="hidden-example",
               category_slug="buttons", is_canonical=True)
    session.commit()
    resp = client.get("/v1/library/sitemap")
    assert resp.status_code == 200
    paths = {row["path"] for row in resp.json()["data"]["entries"]}
    assert not any("hidden-example" in p for p in paths)


# ----------------------------------------------------------------------
# Auth posture
# ----------------------------------------------------------------------


def test_library_endpoints_are_auth_free(
    client: TestClient, session: Session
) -> None:
    """No Authorization header should still return 200 on any library route."""
    _seed_basic_corpus(session)
    for path in (
        "/v1/library/brands",
        "/v1/library/brands/stripe-com",
        "/v1/library/brands/stripe-com/versions/2026-06",
        "/v1/library/brands/stripe-com/categories/buttons",
        "/v1/library/brands/stripe-com/categories/buttons/2026-06",
        "/v1/library/brands/stripe-com/categories/buttons/2026-06/primary",
        "/v1/library/sitemap",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} expected 200, got {resp.status_code}"


# ----------------------------------------------------------------------
# Related-link population (regression: prior code emitted [None]*N)
# ----------------------------------------------------------------------


def _seed_corpus_for_related(session: Session) -> None:
    """One brand, 5+ categories across 2 versions so related has enough rows."""
    av_v1 = _make_asset_version(
        session, url="https://stripe.com/", version_label="2026-05",
        fetched_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    av_v2 = _make_asset_version(
        session, url="https://stripe.com/", version_label="2026-06",
        fetched_at=datetime.now(timezone.utc),
    )
    for cat in ("buttons", "palette", "typography", "cards", "hero"):
        _make_page(session, av_v2, brand_slug="stripe-com",
                   category_slug=cat, is_canonical=True)
    # v1 rows are non-canonical (the reconciler would have flipped them).
    for cat in ("buttons", "palette"):
        _make_page(session, av_v1, brand_slug="stripe-com",
                   category_slug=cat, is_canonical=False)
    session.commit()


def test_related_populates_no_nones_on_brand_canonical(
    client: TestClient, session: Session
) -> None:
    """``related`` on the brand-canonical page is a list of dicts, never None.

    Regression: prior implementation produced ``[None, None, None, None, None]``
    when the canonical-flag filter eliminated all rows (e.g. for DRL-seeded
    corpora before reconciliation). The fix relaxes that filter and adds a
    defensive None-strip on the projection.
    """
    _seed_corpus_for_related(session)
    resp = client.get("/v1/library/brands/stripe-com")
    assert resp.status_code == 200
    related = resp.json()["data"]["related"]
    assert isinstance(related, list)
    assert len(related) >= 3, f"expected 3+ related rows, got {len(related)}"
    for item in related:
        assert item is not None
        assert isinstance(item, dict)
        assert isinstance(item.get("label"), str) and item["label"]
        assert isinstance(item.get("href"), str) and item["href"].startswith("/library/")


def test_related_excludes_current_category(
    client: TestClient, session: Session
) -> None:
    """On a category-scoped page, ``related`` does not list the current category."""
    _seed_corpus_for_related(session)
    resp = client.get("/v1/library/brands/stripe-com/categories/buttons")
    assert resp.status_code == 200
    related = resp.json()["data"]["related"]
    hrefs = [r["href"] for r in related]
    assert not any(h == "/library/stripe-com/buttons/" for h in hrefs)
    # Sibling categories should be present.
    assert any("/palette/" in h for h in hrefs) or any("/cards/" in h for h in hrefs)
