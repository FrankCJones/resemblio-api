"""Library v3 Phase 3: D2/D3 invariant pinned across corpus via full indexer pipeline.

TDD: these tests extend the unit-level D2/D3 coverage in
``test_library_manifest_integration.py`` to the full database write path. The
goal is to prove that what the indexer writes to ``library_pages`` satisfies
the D2/D3 contract, not merely that the underlying functions return the right
values.

D2 invariant (render-real-or-hide):
    A showcase category whose component-group geometry is absent from the brand
    token bag MUST produce an empty ``rendered_html`` in ``library_pages``. The
    web tier reads the empty string and surfaces MissingDataNotice instead of a
    fabricated generic component.

D3 invariant (acknowledge-every-gap):
    The same page row MUST carry ``metadata_json.missing_data_notice.missing_items``
    that names the showcase category slug. The API route reads this field to populate
    ``missing_groups`` in the response; the web tier forwards it to MissingDataNotice.
    A page whose ``rendered_html`` is empty but whose notice is absent (or empty) is
    silently empty - the D3-forbidden case.

Non-empty page invariant:
    The same brand that has empty showcase-category pages MUST have non-empty
    page-pattern category pages. A fully-empty brand is a data pipeline bug, not
    an honest gap.

Brand-level-notice invariant (pinned 2026-06-08 after the first run surfaced it):
    ``missing_data_notice`` is built once per brand (``build_missing_notice`` runs
    against the per-brand ``BrandCaptureManifest``) and written IDENTICALLY into
    every page row's ``metadata_json`` - page-pattern rows included. So a content-
    rich page like ``hero`` carries the same brand-level "these showcase groups are
    not captured" notice as an empty ``buttons`` page. This test pins that behavior
    deliberately: a future refactor that scoped the notice per-category (e.g. only
    attaching it to the empty showcase pages) would change what every page-pattern
    page renders, and that is a product decision that must be made on purpose, not
    drift in silently. The public-view UX implication of this brand-level placement
    is tracked separately for Frank; the code contract is pinned here.

Coverage:
    - All 5 indexer-enforced showcase categories (buttons, cards, badges, form-fields,
      inputs) for a DRL seed brand (no component geometry tokens).
    - A representative page-pattern category (hero) for the same brand.
    - The ``library`` showcase category is excluded: it requires button + card geometry
      and is gated by the same D2 gate, but its combined-geometry requirement means
      a DRL seed brand always produces empty for it; the gate is already proven for its
      component groups in the buttons/cards tests above.

Run command (from ``code/api/`` with the test venv active):
    python -m pytest tests/test_library_d2_d3_corpus_invariant.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.constants import SCHEMA_V1
from app.library_indexer import drain_pending, enqueue_for_asset_version
from app.models import AssetVersion, Extraction, LibraryPage
from tests.conftest import seed_user


# ---------------------------------------------------------------------------
# DRL seed token bag (no component geometry)
# ---------------------------------------------------------------------------

# This token bag matches the real DRL seed brands: color, typography, spacing,
# radius, layout, and section tokens are present; button/card/badge/input
# geometry is absent. This is the state of ALL 24 DRL seed brands at launch.
#
# The absence of ds-button-padding-y, ds-card-border-width, ds-badge-padding-y,
# and ds-input-border-width is the trigger for the D2 gate to return should_render=False.
DRL_SEED_TOKENS: dict[str, str] = {
    "ds-bg": "#ffffff",
    "ds-surface": "#f6f9fc",
    "ds-text": "#0a2540",
    "ds-accent": "#635bff",
    "ds-font-display": "Sohne, system-ui",
    "ds-font-body": "Sohne, system-ui",
    "ds-font-weight-display": "600",
    "ds-radius-sm": "6px",
    "ds-radius-md": "8px",
    "ds-space-4": "16px",
    "ds-page-pad-x": "32px",
    "ds-page-max-default": "880px",
    "ds-section-padding-x": "32px",
    "ds-section-padding-y": "96px",
}

# The 5 indexer-enforced showcase categories (excluding 'library' - see docstring).
SHOWCASE_CATEGORIES_UNDER_TEST: tuple[str, ...] = (
    "buttons",
    "cards",
    "badges",
    "form-fields",
    "inputs",
)

# A representative page-pattern category; must always produce non-empty HTML
# for a brand with core color/typography tokens.
PAGE_PATTERN_SAMPLE: str = "hero"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_drl_seed_brand(session: Session) -> AssetVersion:
    """Create an AssetVersion for a DRL seed brand and return it.

    The token bag deliberately lacks all component geometry so the D2 gate
    fires for every showcase category.
    """
    av = AssetVersion(
        url="resemblio://seed/drl_v1/d2d3-test/library/test-snapshot",
        content_hash="d2d3-test-fixture-no-geometry",
        dtcg_json={"tokens": dict(DRL_SEED_TOKENS)},
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label="d2d3-phase3-test",
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(av)
    session.flush()
    return av


def _attach_extraction(session: Session, av: AssetVersion, *, user_id: int) -> Extraction:
    """Attach a passing extraction so the indexer quality gate clears."""
    extraction = Extraction(
        user_id=user_id,
        api_key_id=None,
        url=av.url,
        url_normalized=av.url,
        status="ok",
        tokens_json=av.dtcg_json.get("tokens", {}),
        asset_version_id=av.id,
        schema_version=SCHEMA_V1,
        credit_cents=0,
        quality_score=0.95,
        quality_dimension_scores={"penalty_flags": []},
    )
    session.add(extraction)
    session.flush()
    return extraction


def _page_for(session: Session, av_id: int, category_slug: str) -> LibraryPage | None:
    """Look up the persisted LibraryPage row for (av_id, category_slug)."""
    return (
        session.query(LibraryPage)
        .filter_by(asset_version_id=av_id, category_slug=category_slug)
        .first()
    )


def _missing_items(metadata_json: Any) -> list[dict[str, Any]]:
    """Extract missing_data_notice.missing_items from metadata_json, or [].

    This mirrors the same extraction the API route performs in
    ``_extract_page_manifest_fields``.
    """
    if not isinstance(metadata_json, dict):
        return []
    notice = metadata_json.get("missing_data_notice")
    if not isinstance(notice, dict):
        return []
    items = notice.get("missing_items")
    return items if isinstance(items, list) else []


# ---------------------------------------------------------------------------
# Setup: drive the indexer once for all tests in this module
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def drl_seed_pages(session: Session) -> dict[str, LibraryPage]:
    """Drive the indexer for a DRL seed brand and return category->page map.

    Each test function gets a fresh database (function scope) so the results
    are isolated from other test modules.
    """
    user, _key, _ = seed_user(session)
    av = _insert_drl_seed_brand(session)
    _attach_extraction(session, av, user_id=user.id)
    job = enqueue_for_asset_version(session, av.id)
    assert job is not None, "enqueue returned None; precondition broken"
    session.commit()
    result = drain_pending(session)
    failed = [o for o in result.outcomes if o.status != "complete"]
    assert result.pages_written > 0, (
        f"drain wrote zero pages for DRL seed brand; "
        f"quality gate or compose failure "
        f"(failed_outcomes={[(o.status, o.reason) for o in failed]!r})"
    )
    pages = (
        session.query(LibraryPage)
        .filter_by(asset_version_id=av.id)
        .all()
    )
    return {p.category_slug: p for p in pages}


# ---------------------------------------------------------------------------
# D2 invariant: showcase categories produce empty rendered_html
# ---------------------------------------------------------------------------

class TestD2InvariantAcrossCorpus:
    """D2: uncaptured showcase categories have empty rendered_html in library_pages."""

    @pytest.mark.parametrize("category", SHOWCASE_CATEGORIES_UNDER_TEST)
    def test_showcase_category_rendered_html_is_empty(
        self, drl_seed_pages: dict[str, LibraryPage], category: str
    ) -> None:
        """D2 invariant: indexer writes '' for each uncaptured showcase category.

        If this fails, the D2 gate is not wired into _process_job or the gate
        decision for this category's required geometry group is incorrect.
        """
        page = drl_seed_pages.get(category)
        assert page is not None, (
            f"No library_pages row for category '{category}' after drain. "
            f"Available categories: {sorted(drl_seed_pages.keys())!r}"
        )
        assert page.rendered_html == "", (
            f"D2 violated for '{category}': rendered_html is non-empty "
            f"({len(page.rendered_html)} chars). The brand has no component geometry "
            f"but the indexer fabricated a generic component body. "
            f"Check _compose_with_gate wiring in library_indexer.py::_process_job."
        )


# ---------------------------------------------------------------------------
# D3 invariant: missing_data_notice names each uncaptured showcase category
# ---------------------------------------------------------------------------

class TestD3InvariantAcrossCorpus:
    """D3: missing_data_notice in metadata_json names every uncaptured showcase category."""

    @pytest.mark.parametrize("category", SHOWCASE_CATEGORIES_UNDER_TEST)
    def test_showcase_category_names_in_missing_notice(
        self, drl_seed_pages: dict[str, LibraryPage], category: str
    ) -> None:
        """D3 invariant: missing_data_notice.missing_items names the uncaptured category.

        The API route reads this field to populate ``missing_groups`` in the HTTP
        response; the web tier passes it to MissingDataNotice. An empty notice here
        means MissingDataNotice renders nothing -> silently empty page (D3 violation).
        """
        page = drl_seed_pages.get(category)
        assert page is not None, (
            f"No library_pages row for category '{category}' after drain. "
            f"Available: {sorted(drl_seed_pages.keys())!r}"
        )
        items = _missing_items(page.metadata_json)
        assert len(items) > 0, (
            f"D3 violated for '{category}': missing_data_notice.missing_items is empty. "
            f"The web would show a silently-empty page with no gap acknowledgment. "
            f"Check build_missing_notice wiring in library_indexer.py::_metadata_for."
        )
        category_slugs_in_notice = [
            item["category_slug"]
            for item in items
            if isinstance(item, dict) and "category_slug" in item
        ]
        assert category in category_slugs_in_notice, (
            f"D3 violated for '{category}': category slug is not in missing_items. "
            f"Missing items: {category_slugs_in_notice!r}. "
            f"Check _MissingItem building in missing_data_notice.py."
        )

    @pytest.mark.parametrize("category", SHOWCASE_CATEGORIES_UNDER_TEST)
    def test_showcase_category_page_is_not_silently_empty(
        self, drl_seed_pages: dict[str, LibraryPage], category: str
    ) -> None:
        """D3 + D2 combined: an empty rendered_html MUST be paired with a non-empty notice.

        The D3-forbidden case is a page where both rendered_html == "" AND
        missing_items == []. A visitor landing on that page sees nothing and
        has no explanation. This test catches the case where the D2 gate fires
        but the D3 notice is not written.
        """
        page = drl_seed_pages.get(category)
        assert page is not None
        if page.rendered_html == "":
            items = _missing_items(page.metadata_json)
            assert len(items) > 0, (
                f"D3 forbidden state for '{category}': rendered_html is empty AND "
                f"missing_data_notice.missing_items is absent/empty. "
                f"The page is silently empty with no explanation. "
                f"Both D2 and D3 must fire together."
            )


# ---------------------------------------------------------------------------
# Non-empty invariant: page-pattern categories must render for the same brand
# ---------------------------------------------------------------------------

class TestPagePatternNonEmpty:
    """Page-pattern categories render non-empty HTML even when showcase is empty."""

    def test_page_pattern_hero_has_non_empty_rendered_html(
        self, drl_seed_pages: dict[str, LibraryPage]
    ) -> None:
        """The brand has core tokens; hero MUST produce a non-empty page.

        If this fails, the indexer has a data pipeline problem independent of
        D2/D3 - the quality gate or compose pipeline is rejecting valid brands.
        """
        page = drl_seed_pages.get(PAGE_PATTERN_SAMPLE)
        assert page is not None, (
            f"No library_pages row for '{PAGE_PATTERN_SAMPLE}' after drain. "
            f"Available: {sorted(drl_seed_pages.keys())!r}"
        )
        assert page.rendered_html != "", (
            f"Page-pattern '{PAGE_PATTERN_SAMPLE}' produced empty rendered_html "
            f"even though the brand has core color/typography tokens. "
            f"This is a pipeline bug, not a D2 gate. "
            f"Check _compose_one_page for '{PAGE_PATTERN_SAMPLE}' template."
        )

    def test_page_pattern_carries_the_brand_level_missing_notice(
        self, drl_seed_pages: dict[str, LibraryPage]
    ) -> None:
        """Page-pattern pages carry the SAME brand-level notice as showcase pages.

        ``missing_data_notice`` is per-brand, not per-category (built once from
        the brand manifest, written into every page row). So the content-rich
        ``hero`` page carries the identical missing-items list naming the brand's
        5 uncaptured showcase groups.

        This is intentional and pinned here so a future refactor that scopes the
        notice per-category cannot land silently: it would strip the notice from
        every page-pattern page, which is a deliberate product change, not drift.

        The public-view UX question (should a fully-rendered hero page show a
        "5 component groups not captured" notice?) is a Frank decision tracked
        outside this test. The test asserts only the current code contract.
        """
        page = drl_seed_pages.get(PAGE_PATTERN_SAMPLE)
        assert page is not None
        items = _missing_items(page.metadata_json)
        slugs = sorted(
            item["category_slug"]
            for item in items
            if isinstance(item, dict) and "category_slug" in item
        )
        assert slugs == sorted(SHOWCASE_CATEGORIES_UNDER_TEST), (
            f"Page-pattern '{PAGE_PATTERN_SAMPLE}' must carry the brand-level notice "
            f"naming the 5 uncaptured showcase groups (notice is per-brand, written "
            f"into every page row). Got: {slugs!r}. If this list changed, the notice "
            f"scope changed - confirm it was on purpose."
        )

    def test_more_page_pattern_categories_are_non_empty(
        self, drl_seed_pages: dict[str, LibraryPage]
    ) -> None:
        """At least 8 of the 12 page-pattern categories produce non-empty HTML.

        This proves the DRL seed brand is broadly renderable, not a degenerate
        fixture that only passes because it only renders one category.
        """
        page_patterns = [
            "about-team", "alphabet", "article-layout", "cta-block",
            "feature-grid", "footer", "hero", "navigation",
            "news-list", "pricing-table", "process-steps", "testimonials",
        ]
        non_empty = [
            cat for cat in page_patterns
            if cat in drl_seed_pages and drl_seed_pages[cat].rendered_html != ""
        ]
        assert len(non_empty) >= 8, (
            f"Fewer than 8 page-pattern categories produced non-empty HTML "
            f"({len(non_empty)} of {len(page_patterns)}). "
            f"Non-empty: {non_empty!r}. "
            f"The DRL seed brand has core tokens; this indicates a pipeline problem."
        )
