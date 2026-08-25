"""Tests for scripts.reconcile_library_alias_canonicals."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.constants import SCHEMA_V1
from app.models import AssetVersion, LibraryPage
from scripts.reconcile_library_alias_canonicals import (
    DRL_COMPONENT_MARKER,
    apply_promotions,
    build_report,
)


def _asset_version(
    session: Session,
    *,
    url: str,
    fetched_at: datetime,
) -> AssetVersion:
    """Create a minimal public asset version for script tests."""
    asset_version = AssetVersion(
        url=url,
        content_hash=f"hash-{url}-{fetched_at.isoformat()}",
        dtcg_json={"tokens": {"bg": "#fff"}},
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label="2026-06",
        fetched_at=fetched_at,
    )
    session.add(asset_version)
    session.flush()
    return asset_version


def _page(
    session: Session,
    asset_version: AssetVersion,
    *,
    brand_slug: str,
    category_slug: str,
    rendered_html: str,
    is_canonical: bool,
) -> LibraryPage:
    """Create a library page for script tests."""
    page = LibraryPage(
        asset_version_id=asset_version.id,
        category_slug=category_slug,
        brand_slug=brand_slug,
        version_label=asset_version.version_label,
        rendered_html=rendered_html,
        metadata_json={"schema_version": 1},
        is_canonical=is_canonical,
    )
    session.add(page)
    session.flush()
    return page


def _marker(label: str) -> str:
    """Return marker-backed HTML for a synthetic real component."""
    return f"<article {DRL_COMPONENT_MARKER}>{label}</article>"


def test_build_report_finds_alias_backed_promotion(session: Session) -> None:
    """A plural generic canonical can promote a singular marker-backed row."""
    now = datetime.now(timezone.utc)
    generic_version = _asset_version(session, url="https://a24.example/heroes", fetched_at=now)
    marker_version = _asset_version(
        session,
        url="https://a24.example/hero",
        fetched_at=now + timedelta(minutes=1),
    )
    generic_page = _page(
        session,
        generic_version,
        brand_slug="a24",
        category_slug="heroes",
        rendered_html="<article>generic</article>",
        is_canonical=True,
    )
    marker_page = _page(
        session,
        marker_version,
        brand_slug="a24",
        category_slug="hero",
        rendered_html=_marker("Hero"),
        is_canonical=False,
    )
    session.commit()

    report = build_report(session, environment_label="test", mode="dry-run")

    assert report["counts"]["promotion_groups"] == 1
    promotion = report["promotions"][0]
    assert promotion["brand_slug"] == "a24"
    assert promotion["public_category_slug"] == "hero"
    assert promotion["promote_id"] == marker_page.id
    assert promotion["demote_ids"] == [generic_page.id]
    assert promotion["category_slugs_seen"] == ["hero", "heroes"]


def test_apply_promotions_flips_only_canonical_flags(session: Session) -> None:
    """Apply promotes the marker-backed row and demotes stale canonicals."""
    now = datetime.now(timezone.utc)
    generic_version = _asset_version(session, url="https://a24.example/heroes", fetched_at=now)
    marker_version = _asset_version(
        session,
        url="https://a24.example/hero",
        fetched_at=now + timedelta(minutes=1),
    )
    generic_page = _page(
        session,
        generic_version,
        brand_slug="a24",
        category_slug="heroes",
        rendered_html="<article>generic</article>",
        is_canonical=True,
    )
    marker_page = _page(
        session,
        marker_version,
        brand_slug="a24",
        category_slug="hero",
        rendered_html=_marker("Hero"),
        is_canonical=False,
    )
    session.commit()
    report = build_report(session, environment_label="test", mode="dry-run")

    apply_promotions(session, report["promotions"])
    session.refresh(generic_page)
    session.refresh(marker_page)

    assert generic_page.is_canonical is False
    assert marker_page.is_canonical is True


def test_build_report_finds_same_category_stale_canonical(session: Session) -> None:
    """A same-category marker-backed row can replace a stale generic canonical."""
    now = datetime.now(timezone.utc)
    generic_version = _asset_version(session, url="https://a24.example/buttons-old", fetched_at=now)
    marker_version = _asset_version(
        session,
        url="https://a24.example/buttons-new",
        fetched_at=now + timedelta(minutes=1),
    )
    generic_page = _page(
        session,
        generic_version,
        brand_slug="a24",
        category_slug="buttons",
        rendered_html="<article>generic</article>",
        is_canonical=True,
    )
    marker_page = _page(
        session,
        marker_version,
        brand_slug="a24",
        category_slug="buttons",
        rendered_html=_marker("Buttons"),
        is_canonical=False,
    )
    session.commit()

    report = build_report(session, environment_label="test", mode="dry-run")

    assert report["counts"]["promotion_groups"] == 1
    promotion = report["promotions"][0]
    assert promotion["public_category_slug"] == "buttons"
    assert promotion["promote_id"] == marker_page.id
    assert promotion["demote_ids"] == [generic_page.id]


def test_build_report_classifies_no_marker_competitor(session: Session) -> None:
    """Generic canonicals without marker competitors are listed separately."""
    now = datetime.now(timezone.utc)
    generic_version = _asset_version(session, url="https://a24.example/footer", fetched_at=now)
    generic_page = _page(
        session,
        generic_version,
        brand_slug="a24",
        category_slug="footer",
        rendered_html="<article>generic</article>",
        is_canonical=True,
    )
    session.commit()

    report = build_report(session, environment_label="test", mode="dry-run")

    assert report["counts"]["promotion_groups"] == 0
    assert report["counts"]["no_marker_groups"] == 1
    no_marker = report["no_marker_competitor"][0]
    assert no_marker["public_category_slug"] == "footer"
    assert no_marker["current_canonical_ids"] == [generic_page.id]