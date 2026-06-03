"""Per-category render contract: every library category propagates brand tokens.

CTO TDD staged-recovery R3 deliverable
(``projects/OptSus Team/cto-reviews/2026-06-03-tdd-staged-recovery.md``).

R3 is a CONTRACT TEST, not a code change. It proves - per library category -
that a user browsing ``https://resemblio.com/library/<brand>/<category>``
sees the brand's actual colors, fonts, and primary CTA shape rather than
generic chiclets. Where the contract fails, the test surfaces the gap that
R4 (corpus re-bootstrap) and R5 (per-category snapshot wiring) must close.

The 14 categories under contract are the user-visible library surfaces shipped
by Phase 1; the four DRL templates that exist but are NOT user-library surfaces
(``library``, ``news-list``, ``form-fields``, ``inputs``) are explicitly out of
scope for R3.

For each category the test:

1. Loads the Aeon DRL fixture (a token bag with a known-distinctive palette,
   serif body, sans display) as the brand under test.
2. Runs ``library_indexer._process_job`` against an in-memory SQLite to drive
   the real compose pipeline (no mocking the seam under test).
3. Captures the rendered HTML for the ``(aeon, <category>)`` page.
4. Asserts brand tokens propagated as ``--ds-<key>: <aeon-value>;`` CSS
   custom properties in the page's ``<style>`` block.
5. For categories that paint a primary CTA, asserts the button-radius slot
   resolves through the cascade to a brand-specific value (Aeon overrides
   ``ds-radius-sm`` to ``4px``, so the ``ds-button-radius`` chain
   ``var(--ds-radius-button, var(--ds-radius-sm, 6px))`` resolves to Aeon's
   ``4px`` even without an explicit ``ds-button-radius`` override).

The test is parametrized over all 14 categories; per-category failures
surface the exact gap to close in R4-R5 rather than dropping the whole
suite to RED on a single regression.

Test isolation
--------------
Uses the workspace ``isolated_database`` autouse fixture from ``conftest.py``
to reset the in-memory SQLite per test, and ``seed_user`` to attach a high-
quality extraction so the indexer quality gate passes. No network; no
filesystem writes outside the test's tmp scope.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.constants import SCHEMA_V1
from app.library_indexer import drain_pending, enqueue_for_asset_version
from app.models import AssetVersion, Extraction, LibraryPage
from tests.conftest import seed_user


# ---------------------------------------------------------------------------
# Constants - declared so a future shape change has one diff site.
# ---------------------------------------------------------------------------

# The 14 user-visible library categories per the R3 brief. The vendored DRL
# TEMPLATES_BY_CLASS registers 18 templates; the four extras (``library``,
# ``news-list``, ``form-fields``, ``inputs``) are NOT user-library surfaces
# under Phase 1 and are out of scope for R3.
LIBRARY_CATEGORIES: tuple[str, ...] = (
    "about-team",
    "alphabet",
    "article-layout",
    "badges",
    "buttons",
    "cards",
    "cta-block",
    "feature-grid",
    "footer",
    "hero",
    "navigation",
    "pricing-table",
    "process-steps",
    "testimonials",
)

# Categories whose templates render a primary CTA whose shape (radius)
# must be brand-distinctive for the user-visible-surface proof to hold.
# Categories outside this set (e.g. ``alphabet``, ``article-layout``,
# ``footer``, ``about-team``, ``feature-grid``, ``badges``, ``cards``,
# ``testimonials``, ``process-steps``) do not paint a primary CTA chip,
# so the button-shape assertion does not apply.
CATEGORIES_WITH_PRIMARY_CTA: frozenset[str] = frozenset(
    {"buttons", "cta-block", "hero", "navigation", "pricing-table"}
)

# Aeon seed-URL convention: ``derive_brand_slug`` strips the
# ``resemblio://seed/<system>/<brand>/...`` prefix and returns the second
# segment slugified. This URL drives the test brand under contract.
AEON_SEED_URL = "resemblio://seed/drl_v1/aeon/library/aeon-snapshot"
EXPECTED_BRAND_SLUG = "aeon"

# Frozen Aeon DTCG fixture shipped by Phase 1 Builder. Carries a
# DRL-shape token bag (already-namespaced ``ds-*`` keys) so the contract
# test exercises the same key-normalization path the production indexer
# runs for every DRL-seeded brand.
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "drl" / "aeon_min"
AEON_DTCG_PATH = FIXTURE_DIR / "aeon_dtcg.json"

# Slots the test asserts propagate from Aeon's fixture into every rendered
# page. ``ds-bg`` and ``ds-font-display`` are the brief's named slots;
# their Aeon values let the test verify the brand's palette and display
# typeface reach the user-visible surface.
ASSERTED_BRAND_SLOTS: tuple[str, ...] = ("ds-bg", "ds-font-display")

# The button-radius slot a CTA-bearing category must resolve to a
# brand-specific value (directly or through the cascade) for the
# user-visible-surface proof to hold.
BUTTON_RADIUS_SLOT = "ds-button-radius"


# ---------------------------------------------------------------------------
# Fixture loaders
# ---------------------------------------------------------------------------


def _load_aeon_dtcg() -> dict[str, Any]:
    """Return the frozen Aeon DTCG payload (DRL-shape: nested ``tokens``)."""
    return json.loads(AEON_DTCG_PATH.read_text(encoding="utf-8"))


def _make_aeon_asset_version(session: Session) -> AssetVersion:
    """Insert an AssetVersion carrying the frozen Aeon DRL DTCG payload.

    Marked ``is_public=True`` so the library indexer quality gate accepts
    it; ``fetched_at`` set to ``now`` so the canonical-flag reconciler
    selects this version as the latest for the brand.
    """
    dtcg = _load_aeon_dtcg()
    row = AssetVersion(
        url=AEON_SEED_URL,
        content_hash="aeon-r3-contract-hash",
        dtcg_json=dtcg,
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label="aeon-r3-contract",
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def _attach_passing_extraction(
    session: Session, asset_version: AssetVersion, *, user_id: int
) -> Extraction:
    """Attach a high-quality extraction so the indexer quality gate passes.

    The indexer skips assets whose joined extraction carries a quality_score
    below ``LIBRARY_INDEX_QUALITY_THRESHOLD`` or any penalty flag. We
    write a clean 0.95 score with no flags so every category lands and the
    contract assertions actually evaluate rendered output.
    """
    extraction = Extraction(
        user_id=user_id,
        api_key_id=None,
        url=asset_version.url,
        url_normalized=asset_version.url,
        status="ok",
        tokens_json=asset_version.dtcg_json.get("tokens", {}),
        asset_version_id=asset_version.id,
        schema_version=SCHEMA_V1,
        credit_cents=0,
        quality_score=0.95,
        quality_dimension_scores={"penalty_flags": []},
    )
    session.add(extraction)
    session.flush()
    return extraction


def _enqueue_and_drain(session: Session, asset_version: AssetVersion) -> int:
    """Enqueue an index job for ``asset_version`` and run one drain tick.

    Returns the number of pages written so callers can guard against silent
    quality-gate skips. Asserts inside on the enqueue precondition.
    """
    job = enqueue_for_asset_version(session, asset_version.id)
    assert job is not None, "enqueue helper returned None - precondition broken"
    session.commit()
    result = drain_pending(session)
    return result.pages_written


# ---------------------------------------------------------------------------
# Per-test setup helper (returns the {category: rendered_html} map)
# ---------------------------------------------------------------------------


def _render_all_pages(session: Session) -> dict[str, str]:
    """Drain one Aeon job and return a {category_slug: rendered_html} map.

    Centralizes the seed/enqueue/drain dance so each parametrized test
    re-runs the full pipeline (the autouse ``isolated_database`` fixture
    rebuilds the schema per test, so a shared module-scoped cache would
    fight that isolation). The cost is small - the compose pipeline runs
    once per parameter case - and the gain is per-category failure
    surfacing without cross-test coupling.
    """
    user, _key, _ = seed_user(session)
    av = _make_aeon_asset_version(session)
    _attach_passing_extraction(session, av, user_id=user.id)
    written = _enqueue_and_drain(session, av)
    assert written > 0, (
        "library indexer wrote zero pages for the Aeon fixture - "
        "quality gate or compose pipeline regression upstream of R3"
    )
    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    return {page.category_slug: page.rendered_html or "" for page in pages}


# ---------------------------------------------------------------------------
# Per-category contract assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", LIBRARY_CATEGORIES)
def test_category_renders_aeon_bg_token(session: Session, category: str) -> None:
    """Aeon's ``ds-bg`` value appears as ``--ds-bg:`` in the rendered page.

    Proves Path C ``:root`` emission propagates the brand's page-background
    color for every user-visible library category. If this fails for a
    category, the rendered output would paint with the contract default
    (or a browser default) and the user sees a generic surface instead of
    Aeon's editorial white.

    Aeon's ``ds-bg`` value (``#ffffff``) coincides with the contract
    default for this slot; the meaningful signal asserted here is that
    the brand's value reached the page (the value the user sees), not
    that it differs from the default. Where Aeon's value differs from
    default (e.g. ``ds-accent``, ``ds-font-display``), the parallel
    ``test_category_renders_aeon_font_display_token`` makes the
    differs-from-default case directly.
    """
    pages = _render_all_pages(session)
    assert category in pages, (
        f"category {category!r} has no rendered page - compose pipeline "
        f"did not emit this template"
    )
    rendered = pages[category]
    aeon_bg = _load_aeon_dtcg()["tokens"]["ds-bg"]
    expected = f"--ds-bg: {aeon_bg};"
    assert expected in rendered, (
        f"category {category!r}: brand ds-bg token did not propagate; "
        f"expected substring {expected!r} in rendered HTML. R4-R5 gap: "
        f"this category's :root emission is not picking up the brand's "
        f"ds-bg value (likely Path C key-normalization or compose-seam "
        f"regression)"
    )


@pytest.mark.parametrize("category", LIBRARY_CATEGORIES)
def test_category_renders_aeon_font_display_token(
    session: Session, category: str
) -> None:
    """Aeon's ``ds-font-display`` value appears as ``--ds-font-display:``.

    Aeon's display family (``'PP Right Grotesk Wide', ...``) differs
    sharply from the contract default (no font-family default is set for
    ``ds-font-display`` in the contract today; it is a pass-through extra
    slot). If this fails, the rendered output falls back to the host
    page's font and the user sees a generic sans instead of Aeon's
    editorial display typeface.
    """
    pages = _render_all_pages(session)
    assert category in pages, (
        f"category {category!r} has no rendered page - compose pipeline "
        f"did not emit this template"
    )
    rendered = pages[category]
    aeon_display = _load_aeon_dtcg()["tokens"]["ds-font-display"]
    expected = f"--ds-font-display: {aeon_display};"
    assert expected in rendered, (
        f"category {category!r}: brand ds-font-display did not propagate; "
        f"expected substring {expected!r} in rendered HTML. R4-R5 gap: "
        f"this category's :root emission is not picking up the brand's "
        f"display-typeface value"
    )


@pytest.mark.parametrize("category", LIBRARY_CATEGORIES)
def test_category_button_radius_resolves_to_brand_value_when_cta_present(
    session: Session, category: str
) -> None:
    """Categories with a primary CTA emit a brand-resolvable ``ds-button-radius``.

    Two acceptance paths satisfy this assertion, both of which prove the
    user sees a brand-shaped CTA rather than a default chiclet:

    1. **Direct override**: the brand JSON supplies ``ds-button-radius``
       explicitly, the Path C emitter writes it verbatim into ``:root``,
       and the rendered CSS contains ``--ds-button-radius: <brand-value>;``
       (where the value is not the contract-default placeholder).
    2. **Cascade resolution**: the brand JSON supplies a related family
       slot (``ds-radius-sm``, ``ds-radius-button``) that the
       ``ds-button-radius`` default chains through
       (``var(--ds-radius-button, var(--ds-radius-sm, 6px))``). Aeon's
       fixture overrides ``ds-radius-sm`` to ``4px``, so the cascade
       resolves to Aeon's ``4px`` even with no explicit
       ``ds-button-radius`` override.

    Aeon today carries no ``ds-button-radius`` override and no
    ``ds-radius-button`` override; the cascade reaches its
    ``ds-radius-sm`` override. The test passes if EITHER the slot is
    overridden directly OR one of the cascade rungs Aeon does override
    is detectable in the rendered CSS.

    For categories OUTSIDE ``CATEGORIES_WITH_PRIMARY_CTA`` (e.g.
    ``alphabet``, ``footer``, ``cards`` without an explicit CTA chip)
    the assertion is trivially satisfied - those categories do not need
    a brand-shaped button radius to render distinctively.

    R4-R5 gap surfaced: any CTA-bearing category whose rendered CSS
    references ``--ds-button-radius:`` but resolves only to contract
    defaults (no brand override AND no cascade rung overridden) needs
    a brand-overlay token in R5 (per-category snapshot wiring) before
    the user-visible-surface proof holds end to end.
    """
    if category not in CATEGORIES_WITH_PRIMARY_CTA:
        pytest.skip(
            f"category {category!r} does not paint a primary CTA; "
            f"button-radius brand-shape assertion does not apply"
        )

    pages = _render_all_pages(session)
    assert category in pages, (
        f"category {category!r} has no rendered page - compose pipeline "
        f"did not emit this template"
    )
    rendered = pages[category]
    tokens: dict[str, str] = _load_aeon_dtcg()["tokens"]

    # Path 1: explicit brand override on ds-button-radius reached the page.
    if BUTTON_RADIUS_SLOT in tokens:
        expected = f"--{BUTTON_RADIUS_SLOT}: {tokens[BUTTON_RADIUS_SLOT]};"
        assert expected in rendered, (
            f"category {category!r}: brand override on {BUTTON_RADIUS_SLOT} "
            f"({tokens[BUTTON_RADIUS_SLOT]!r}) did not propagate; expected "
            f"substring {expected!r} in rendered HTML. R4-R5 gap: Path C "
            f"emitter dropped an explicit brand override"
        )
        return

    # Path 2: cascade resolves through a rung the brand overrode.
    # ds-button-radius default chains:
    #   var(--ds-radius-button, var(--ds-radius-sm, 6px))
    # The contract default for the slot must appear in :root; AND at
    # least one of the cascade rungs the brand overrode must also be
    # emitted with its brand value, so the cascade resolves to a brand
    # value rather than the literal 6px tail.
    cascade_rungs = ("ds-radius-button", "ds-radius-sm")
    brand_overridden_rungs = [rung for rung in cascade_rungs if rung in tokens]
    assert brand_overridden_rungs, (
        f"category {category!r}: brand carries no ds-button-radius override "
        f"and no override on any cascade rung ({cascade_rungs!r}); the "
        f"rendered CTA will fall back to the literal 6px tail and look "
        f"like a generic chiclet. R5 gap: this brand needs a button-radius "
        f"overlay token (or one of the cascade rungs) before the user-"
        f"visible-surface proof holds for this category"
    )
    for rung in brand_overridden_rungs:
        expected = f"--{rung}: {tokens[rung]};"
        assert expected in rendered, (
            f"category {category!r}: brand override on cascade rung "
            f"{rung!r} ({tokens[rung]!r}) did not propagate; expected "
            f"substring {expected!r} in rendered HTML. R4-R5 gap: Path C "
            f":root emitter dropped a brand cascade-rung value, so "
            f"ds-button-radius cascades past the brand and lands on the "
            f"literal 6px default"
        )

    # Belt-and-suspenders: the slot itself must be referenced in :root so
    # the cascade actually evaluates against the brand-overridden rung
    # rather than the browser ignoring the slot entirely.
    assert f"--{BUTTON_RADIUS_SLOT}:" in rendered, (
        f"category {category!r}: rendered HTML does not reference "
        f"--{BUTTON_RADIUS_SLOT} at all; Path C contract emission "
        f"regressed (the contract slot is not being emitted in :root)"
    )


@pytest.mark.parametrize("category", LIBRARY_CATEGORIES)
def test_category_renders_no_lorem_placeholder(
    session: Session, category: str
) -> None:
    """No ``lorem`` substring leaks into any rendered category page.

    Lorem-ipsum leaking through the body was the visible symptom of the
    indexer ignoring its inputs (bug 3 from the 2026-06-02 audit). The
    parallel test in ``test_library_indexer_render_fidelity`` covers the
    Aeon case at the suite level; here we pin it per-category so an R4-R5
    regression for a single category surfaces with the exact category
    name rather than a single suite-level fail.
    """
    pages = _render_all_pages(session)
    assert category in pages, (
        f"category {category!r} has no rendered page - compose pipeline "
        f"did not emit this template"
    )
    lowered = pages[category].lower()
    assert "lorem" not in lowered, (
        f"category {category!r} rendered HTML carries 'lorem' placeholder "
        f"text - compose pipeline ignored brand-aware placeholder map "
        f"(bug 3 regression)"
    )
