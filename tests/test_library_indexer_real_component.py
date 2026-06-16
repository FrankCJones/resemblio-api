"""Tests for the real-component render path in the library indexer (issue #3).

The indexer previously rendered every page from a generic DRL template tinted
with brand tokens - "same kit recolored." Issue #2 stored real DRL component
markup + CSS in asset_components; issue #3 makes the indexer serve it.

Coverage:
- get_asset_component read helper: returns row by (asset_version_id, fragment_key);
  returns None when absent; returns None when fragment_key does not match.
- Matching class with a stored component: rendered_html carries the real CSS
  (hover and focus rules from the component) and data-rs-source="drl-component".
- Matching class without a stored component: rendered_html is empty (honest
  "not captured" path, not a fabricated generic template).
- Non-matching class: rendered_html does NOT carry data-rs-source="drl-component"
  (the real-component path fires only for the exact matching class).
- Re-index idempotency: indexing the same asset_version twice updates the
  library_pages row in place; no duplicate rows are created.

No network calls; no DRL file reads. All fixtures are synthetic.

Do this work at a level that would impress a senior developer.
Include documentation and code comments that make it easy for a future developer
to maintain this project.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.asset_versions import (
    AssetComponentSpec,
    get_asset_component,
    insert_asset_component,
    insert_or_reuse_asset_version,
)
from app.constants import SCHEMA_V1
from app.library_indexer import drain_pending, enqueue_for_asset_version
from app.models import AssetVersion, LibraryPage


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

# Real-looking button CSS with interaction states - exactly what a DRL
# asset.html would carry. These are the markers the tests assert on; they
# must NOT appear in the generic .b-btn template output.
_BUTTONS_COMPONENT_CSS = (
    ".btn { background: var(--ds-accent); border-radius: 4px; }\n"
    ".btn:hover { opacity: 0.85; }\n"
    ".btn:focus-visible { outline: 2px solid var(--ds-accent); }\n"
)
_BUTTONS_COMPONENT_HTML = (
    '<div class="btn-group">'
    '<button class="btn">Primary</button>'
    '<button class="btn" disabled>Disabled</button>'
    "</div>"
)

_HEALTHY_TOKENS: dict[str, str] = {
    "bg": "#0a0a0a",
    "surface": "#1a1a1a",
    "text": "#ffffff",
    "accent": "#ff3366",
    "font_body": "Inter, sans-serif",
    "font_display": "Playfair Display, serif",
}

# The generic DRL template renders its button using the .b-btn class.
# Real-component pages must NOT contain this string - it is the signal that
# the generic chiclet path fired instead of the real DRL component.
_GENERIC_CHICLET_MARKER = "b-btn"


def _make_buttons_asset_version(
    session: Session,
    *,
    brand_slug: str = "acme",
    is_public: bool = True,
    with_component: bool = True,
) -> AssetVersion:
    """Create a synthetic buttons asset_version row with an optional asset_component.

    The dtcg_json["class"] is set to "buttons" so the indexer's real-component
    routing recognises this as a buttons asset and serves the stored component
    on the buttons page.

    Args:
        session: active SQLAlchemy session.
        brand_slug: brand identifier; also controls the asset URL so each call
            produces a distinct (url, content_hash) pair.
        is_public: controls the quality gate; tests that need the indexer to
            process the asset must leave this True.
        with_component: when True, also insert an asset_components row with
            real CSS containing :hover and :focus-visible rules.
    """
    dtcg: dict = {
        "schema_version": SCHEMA_V1,
        "slug": brand_slug,
        "class": "buttons",  # The asset's real DRL class; drives indexer routing.
        "tokens": dict(_HEALTHY_TOKENS),
    }
    av = AssetVersion(
        url=f"https://{brand_slug}.example/",
        content_hash=f"test-hash-{brand_slug}-buttons",
        dtcg_json=dtcg,
        manifest_schema_version=SCHEMA_V1,
        is_public=is_public,
        version_label="test-2026-06",
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(av)
    session.flush()

    if with_component:
        spec = AssetComponentSpec(
            fragment_key="default",
            component_html=_BUTTONS_COMPONENT_HTML,
            component_css=_BUTTONS_COMPONENT_CSS,
            source_asset_path="assets/atoms/buttons/acme-001",
            states_present=["rest", "hover", "focus", "disabled"],
        )
        insert_asset_component(session, av.id, spec)
        session.flush()

    return av


def _run_indexer(session: Session, asset_version: AssetVersion) -> None:
    """Enqueue the asset_version for indexing, commit, and drain the queue."""
    job = enqueue_for_asset_version(session, asset_version.id)
    assert job is not None, (
        "enqueue_for_asset_version returned None; check is_public flag and "
        "whether a live job already exists for this asset_version"
    )
    session.commit()
    drain_pending(session)


def _fetch_page(session: Session, asset_version_id: int, category_slug: str) -> LibraryPage | None:
    """Return the library_pages row for (asset_version_id, category_slug), or None."""
    return session.execute(
        select(LibraryPage)
        .where(LibraryPage.asset_version_id == asset_version_id)
        .where(LibraryPage.category_slug == category_slug)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# get_asset_component read helper
# ---------------------------------------------------------------------------


def test_get_asset_component_returns_row_when_present(session: Session) -> None:
    """get_asset_component returns the stored AssetComponent for (asset_version_id, "default")."""
    av = _make_buttons_asset_version(session, with_component=True)

    component = get_asset_component(session, av.id)

    assert component is not None
    assert component.asset_version_id == av.id
    assert component.fragment_key == "default"
    # The stored CSS must survive the round-trip exactly.
    assert ":hover" in component.component_css
    assert ":focus-visible" in component.component_css


def test_get_asset_component_returns_none_when_absent(session: Session) -> None:
    """get_asset_component returns None when no asset_components row exists for the asset."""
    av = _make_buttons_asset_version(session, with_component=False)

    component = get_asset_component(session, av.id)

    assert component is None


def test_get_asset_component_returns_none_for_unknown_fragment_key(session: Session) -> None:
    """get_asset_component returns None when the requested fragment_key is not stored."""
    av = _make_buttons_asset_version(session, with_component=True)

    # Only "default" was stored; "inverse" should return None.
    component = get_asset_component(session, av.id, fragment_key="inverse")

    assert component is None


def test_get_asset_component_default_key_is_default(session: Session) -> None:
    """get_asset_component uses 'default' as the fragment_key when none is specified."""
    av = _make_buttons_asset_version(session, with_component=True)
    # Explicit call matches the implicit default.
    with_default = get_asset_component(session, av.id, fragment_key="default")
    without_kwarg = get_asset_component(session, av.id)
    assert with_default is not None
    assert without_kwarg is not None
    assert with_default.id == without_kwarg.id


# ---------------------------------------------------------------------------
# Indexer real-component routing
# ---------------------------------------------------------------------------


def test_matching_class_with_component_renders_real_html(session: Session) -> None:
    """When class_name == dtcg["class"] and a component row exists, rendered_html uses the real code.

    Assertions:
    - The page carries the DRL component's :hover rule.
    - The page carries the DRL component's :focus-visible rule.
    - The article wrapper has data-rs-source="drl-component" (the contract marker).
    - The page does NOT contain the generic .b-btn chiclet from the template path.
    """
    av = _make_buttons_asset_version(session, brand_slug="acme", with_component=True)
    session.commit()

    _run_indexer(session, av)

    page = _fetch_page(session, av.id, "buttons")
    assert page is not None, "buttons LibraryPage row was not written by the indexer"
    html = page.rendered_html

    assert ":hover" in html, (
        "real :hover rule missing from rendered_html; "
        "the generic template path may have fired instead of the real-component path"
    )
    assert ":focus-visible" in html, (
        "real :focus-visible rule missing from rendered_html"
    )
    assert 'data-rs-source="drl-component"' in html, (
        "data-rs-source marker missing; the real-component compose path did not fire"
    )
    assert _GENERIC_CHICLET_MARKER not in html, (
        "generic .b-btn chiclet appeared in a real-component page; "
        "the indexer served the template instead of the stored DRL component"
    )


def test_non_matching_class_does_not_carry_drl_component_marker(session: Session) -> None:
    """For a buttons asset_version, non-matching pages (e.g. hero) must NOT carry data-rs-source.

    The real-component path is gated on class_name == dtcg["class"]. A buttons
    asset_version's hero page should use the generic template path, not the
    real-component path.
    """
    av = _make_buttons_asset_version(session, brand_slug="acme", with_component=True)
    session.commit()

    _run_indexer(session, av)

    # 'hero' is a page-pattern class; the real component is for 'buttons' only.
    hero_page = _fetch_page(session, av.id, "hero")
    assert hero_page is not None, "hero LibraryPage row was not written"
    assert 'data-rs-source="drl-component"' not in hero_page.rendered_html, (
        "data-rs-source appeared on the hero page; real-component routing "
        "must fire only for the exact matching class (buttons)"
    )


def test_matching_class_without_component_gives_empty_body(session: Session) -> None:
    """When class_name == dtcg["class"] but no asset_components row exists, rendered_html is empty.

    The empty body signals to the web layer to surface an honest 'not captured'
    notice. The page row itself IS written so the route does not 404; only the
    body is absent. No fabricated generic template content is served.
    """
    av = _make_buttons_asset_version(session, brand_slug="acme", with_component=False)
    session.commit()

    _run_indexer(session, av)

    page = _fetch_page(session, av.id, "buttons")
    assert page is not None, (
        "buttons LibraryPage row missing; it should exist even when no component is stored, "
        "so the route returns 200 with an empty body rather than 404"
    )
    assert page.rendered_html == "", (
        "expected empty rendered_html (honest notice path) when no asset_component row "
        f"exists for the matching class, but got: {page.rendered_html[:200]!r}"
    )
    # The metadata always includes the missing_data_notice structure regardless
    # of whether items are missing; the web layer reads it to decide what to show.
    assert page.metadata_json is not None
    assert "missing_data_notice" in page.metadata_json


def test_reindex_updates_page_in_place(session: Session) -> None:
    """Indexing the same asset_version twice updates the page row in place; no duplicates created.

    This inherits from the existing idempotent upsert (SAVEPOINT guard) in the
    indexer loop. The real-component path must not break that invariant.
    """
    av = _make_buttons_asset_version(session, brand_slug="acme", with_component=True)
    session.commit()

    # First index run.
    _run_indexer(session, av)
    page_after_first = _fetch_page(session, av.id, "buttons")
    assert page_after_first is not None
    first_html = page_after_first.rendered_html
    assert 'data-rs-source="drl-component"' in first_html

    # Second index run: completed jobs do not block a new enqueue.
    job2 = enqueue_for_asset_version(session, av.id)
    assert job2 is not None
    session.commit()
    drain_pending(session)

    # Exactly one buttons page row must exist after two runs.
    all_buttons_pages = session.execute(
        select(LibraryPage)
        .where(LibraryPage.asset_version_id == av.id)
        .where(LibraryPage.category_slug == "buttons")
    ).scalars().all()
    assert len(all_buttons_pages) == 1, (
        f"Expected 1 buttons page after two index runs, got {len(all_buttons_pages)}"
    )
    # Content should be identical (deterministic compose).
    assert all_buttons_pages[0].rendered_html == first_html
