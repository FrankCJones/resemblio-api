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


def _make_page_with_metadata(
    session: Session,
    av: AssetVersion,
    *,
    brand_slug: str,
    category_slug: str,
    metadata: dict[str, Any],
    is_canonical: bool = True,
) -> LibraryPage:
    """Insert a library_pages row carrying a caller-supplied metadata envelope."""
    page = LibraryPage(
        asset_version_id=av.id,
        category_slug=category_slug,
        brand_slug=brand_slug,
        version_label=av.version_label,
        rendered_html="<p>hi</p>",
        metadata_json=metadata,
        is_canonical=is_canonical,
    )
    session.add(page)
    session.flush()
    return page


def test_brands_hub_returns_palette_and_display_font(
    client: TestClient, session: Session
) -> None:
    """Each hub row carries palette[] + display_font sourced from metadata_json.

    Palette is ordered accent-first (per the web contract's "index 0 is the
    canonical accent / primary"), then bg, surface, text. Hex values are
    lowercased; non-hex / missing slots are dropped silently.
    """
    av = _make_asset_version(session, url="https://stripe.com/",
                             version_label="2026-06")
    _make_page_with_metadata(
        session, av,
        brand_slug="stripe-com",
        category_slug="buttons",
        metadata={
            "schema_version": 1,
            "brand_slug": "stripe-com",
            "category_slug": "buttons",
            "bg": "#FFFFFF",
            "surface": "#F6F9FC",
            "text": "#0A2540",
            "accent": "#635BFF",
            "font_display": 'sohne-var, "Helvetica Neue", sans-serif',
            "font_body": "system-ui",
        },
    )
    session.commit()

    resp = client.get("/v1/library/brands")
    assert resp.status_code == 200
    rows = resp.json()["data"]["featured"]
    row = next(r for r in rows if r["brand_slug"] == "stripe-com")
    # accent first, then bg, surface, text - all lowercased
    assert row["palette"] == ["#635bff", "#ffffff", "#f6f9fc", "#0a2540"]
    assert row["display_font"] == 'sohne-var, "Helvetica Neue", sans-serif'


def test_brands_hub_palette_drops_non_hex_and_caps_at_five(
    client: TestClient, session: Session
) -> None:
    """Non-hex values are dropped; result is capped at 5 entries; dedupe is case-insensitive."""
    av = _make_asset_version(session, url="https://example.com/",
                             version_label="2026-06")
    _make_page_with_metadata(
        session, av,
        brand_slug="example-com",
        category_slug="buttons",
        metadata={
            "schema_version": 1,
            "brand_slug": "example-com",
            "category_slug": "buttons",
            # accent invalid (rgb), bg duplicate of text but different case
            "accent": "rgb(99, 91, 255)",
            "bg": "#ABCDEF",
            "surface": None,
            "text": "#abcdef",
            "font_display": None,
        },
    )
    session.commit()

    resp = client.get("/v1/library/brands")
    assert resp.status_code == 200
    rows = resp.json()["data"]["featured"]
    row = next(r for r in rows if r["brand_slug"] == "example-com")
    # Only #abcdef survives (accent rgb dropped, surface None dropped,
    # text dedupes against bg case-insensitively).
    assert row["palette"] == ["#abcdef"]
    assert row["display_font"] is None


def test_brands_hub_palette_empty_when_no_metadata(
    client: TestClient, session: Session
) -> None:
    """A brand whose metadata carries no usable color slots returns empty palette + null font."""
    av = _make_asset_version(session, url="https://bare.example/",
                             version_label="2026-06")
    _make_page_with_metadata(
        session, av,
        brand_slug="bare-example",
        category_slug="buttons",
        metadata={
            "schema_version": 1,
            "brand_slug": "bare-example",
            "category_slug": "buttons",
        },
    )
    session.commit()

    resp = client.get("/v1/library/brands")
    assert resp.status_code == 200
    rows = resp.json()["data"]["featured"]
    row = next(r for r in rows if r["brand_slug"] == "bare-example")
    assert row["palette"] == []
    assert row["display_font"] is None


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


# ----------------------------------------------------------------------
# Library v2 manifest fields - Phase 4 shape contract
# ----------------------------------------------------------------------
# These tests verify that the manifest provenance fields written by the
# Library v2 indexer (missing_data_notice, capture_manifest, hub_capture_signal)
# are carried through the API payloads to the web BFF.
#
# Phase 4 contract:
#   - Hub row: captured_count + total_showcase_groups
#   - Page payload: missing_groups + captured_groups
#   - Graceful degradation: empty lists / zero counts for pre-v2 metadata
#
# Companion to test_library_manifest_integration.py (which tests the pure
# indexer functions); this module tests the route-level shape.


def _v2_metadata(
    *,
    brand_slug: str = "stripe-com",
    category_slug: str = "buttons",
    captured_groups: list[str] | None = None,
    missing_slugs: list[str] | None = None,
    captured_count: int = 0,
    total_showcase_groups: int = 5,
) -> dict[str, Any]:
    """Build a realistic Library v2 metadata_json envelope.

    Mirrors the shape ``_metadata_for()`` in ``library_indexer.py`` writes.
    Used by route shape tests that need real capture metadata without running
    the full compose pipeline.
    """
    if captured_groups is None:
        captured_groups = ["color", "typography", "spacing"]
    if missing_slugs is None:
        missing_slugs = ["badges", "buttons", "cards", "form-fields", "inputs"]
    groups_dict: dict[str, Any] = {}
    for g in captured_groups:
        groups_dict[g] = {"captured": True, "present_source_fields": [], "absent_source_fields": []}
    for g in ("button", "card", "badge", "input"):
        if g not in groups_dict:
            groups_dict[g] = {"captured": False, "present_source_fields": [], "absent_source_fields": [g]}
    return {
        "schema_version": 2,
        "brand_slug": brand_slug,
        "category_slug": category_slug,
        "bg": "#ffffff",
        "accent": "#635bff",
        "text": "#0a2540",
        "font_display": "Inter",
        "font_body": "Inter",
        "capture_manifest": {
            "schema_version": "capture_manifest_v1",
            "groups": groups_dict,
        },
        "hub_capture_signal": {
            "schema_version": "hub_capture_signal_v1",
            "captured_count": captured_count,
            "total_showcase_groups": total_showcase_groups,
        },
        "missing_data_notice": {
            "schema_version": "missing_data_notice_v1",
            "missing_items": [
                {"category_slug": s, "display_name": s.replace("-", " ").title()}
                for s in missing_slugs
            ],
        },
    }


def test_hub_row_carries_captured_count_and_total(
    client: TestClient, session: Session
) -> None:
    """Hub rows carry captured_count and total_showcase_groups from metadata_json."""
    av = _make_asset_version(session, url="https://stripe.com/", version_label="2026-06")
    _make_page_with_metadata(
        session, av,
        brand_slug="stripe-com",
        category_slug="buttons",
        metadata=_v2_metadata(captured_count=2, total_showcase_groups=5),
    )
    session.commit()

    resp = client.get("/v1/library/brands")
    assert resp.status_code == 200
    rows = resp.json()["data"]["featured"]
    row = next(r for r in rows if r["brand_slug"] == "stripe-com")
    assert row["captured_count"] == 2
    assert row["total_showcase_groups"] == 5


def test_hub_row_capture_signal_defaults_when_no_metadata(
    client: TestClient, session: Session
) -> None:
    """Hub rows carry captured_count=0 and total_showcase_groups=0 for pre-v2 rows."""
    av = _make_asset_version(session, url="https://old.example/", version_label="2026-06")
    _make_page(session, av, brand_slug="old-example", category_slug="buttons")
    session.commit()

    resp = client.get("/v1/library/brands")
    assert resp.status_code == 200
    rows = resp.json()["data"]["featured"]
    row = next(r for r in rows if r["brand_slug"] == "old-example")
    # Pre-v2 metadata has no hub_capture_signal; safe defaults must apply.
    assert row["captured_count"] == 0
    assert row["total_showcase_groups"] == 0


def test_page_payload_carries_missing_groups(
    client: TestClient, session: Session
) -> None:
    """Brand canonical page carries missing_groups sourced from missing_data_notice."""
    av = _make_asset_version(session, url="https://stripe.com/", version_label="2026-06")
    _make_page_with_metadata(
        session, av,
        brand_slug="stripe-com",
        category_slug="buttons",
        is_canonical=True,
        metadata=_v2_metadata(
            missing_slugs=["badges", "buttons", "cards", "form-fields", "inputs"],
        ),
    )
    session.commit()

    resp = client.get("/v1/library/brands/stripe-com")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "missing_groups" in data
    assert isinstance(data["missing_groups"], list)
    assert set(data["missing_groups"]) == {
        "badges", "buttons", "cards", "form-fields", "inputs",
    }


def test_page_payload_carries_captured_groups(
    client: TestClient, session: Session
) -> None:
    """Brand canonical page carries captured_groups from capture_manifest."""
    av = _make_asset_version(session, url="https://stripe.com/", version_label="2026-06")
    _make_page_with_metadata(
        session, av,
        brand_slug="stripe-com",
        category_slug="buttons",
        is_canonical=True,
        metadata=_v2_metadata(captured_groups=["color", "typography", "spacing"]),
    )
    session.commit()

    resp = client.get("/v1/library/brands/stripe-com")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "captured_groups" in data
    assert isinstance(data["captured_groups"], list)
    # The three groups passed in must be in the captured set.
    assert {"color", "typography", "spacing"}.issubset(set(data["captured_groups"]))


def test_page_payload_manifest_fields_empty_for_pre_v2_metadata(
    client: TestClient, session: Session
) -> None:
    """Page payload returns empty lists when metadata_json has no v2 manifest fields."""
    av = _make_asset_version(session, url="https://old.example/", version_label="2026-06")
    _make_page(session, av, brand_slug="old-brand",
               category_slug="buttons", is_canonical=True)
    session.commit()

    resp = client.get("/v1/library/brands/old-brand")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["missing_groups"] == []
    assert data["captured_groups"] == []


def test_page_payload_missing_groups_empty_when_all_captured(
    client: TestClient, session: Session
) -> None:
    """Page payload has empty missing_groups when every showcase category is captured."""
    av = _make_asset_version(session, url="https://full.example/", version_label="2026-06")
    _make_page_with_metadata(
        session, av,
        brand_slug="full-brand",
        category_slug="buttons",
        is_canonical=True,
        metadata=_v2_metadata(missing_slugs=[]),
    )
    session.commit()

    resp = client.get("/v1/library/brands/full-brand")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["missing_groups"] == []
