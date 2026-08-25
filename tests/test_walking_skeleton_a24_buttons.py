"""Walking skeleton integration test: a24/buttons renders the real DRL button.

Issue #4 in the Resemblio Library v6 milestone. Proves the full seed-to-indexer
pipeline end-to-end against a real DRL asset for the first time:

  DRL asset.html -> seed extraction helpers -> asset_components row
  -> library_indexer compose -> LibraryPage.rendered_html

Target: a24/buttons (``a24-cinematic-001``). Chosen because a24 owns exactly ONE
button asset in the DRL corpus, eliminating canonical-selection ambiguity. The
multi-atom case (anthropic has 4 buttons, linear has 2) is tracked in issues
#5 and #6 and is out of scope here.

The asset exercises all five acceptance criteria (per the issue #4 Definition
of Done):
  1. ``.btn`` present (the primary button class from DRL markup + CSS).
  2. ``.btn:hover`` rule present (slab warm-step interaction).
  3. ``.btn:focus-visible`` rule present (ink offset focus ring).
  4. ``data-rs-source="drl-component"`` present (real-component contract marker
     set by ``_compose_real_component`` in library_indexer.py).
  5. ``.b-btn`` absent (the generic chiclet class; must never appear on a page
     served from a real DRL component).

DRL access policy
-----------------
The test prefers the real DRL file when the workspace DRL is present (dev).
For CI (API repo only, no DRL), it falls back to the vendored fixture at
``tests/fixtures/drl/a24_button/asset.html``, which is a verbatim copy of
``projects/Design Reference Library/assets/atoms/buttons/a24-cinematic-001/asset.html``.
The DRL is never written to: all access is ``read_text()``-only.

Depends on: issue #2 (seed stores component markup+CSS in asset_components) and
issue #3 (indexer serves the stored component instead of a generic template).
Both issues are merged.

Do this work at a level that would impress a senior developer.
Include documentation and code comments that make it easy for a future developer
to maintain this project.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.asset_versions import AssetComponentSpec, insert_asset_component
from app.constants import SCHEMA_V1
from app.library_indexer import drain_pending, enqueue_for_asset_version
from app.models import AssetVersion, LibraryPage
from scripts.seed_from_drl import (
    derive_states_present,
    extract_component_css,
    extract_component_html,
)


# ---------------------------------------------------------------------------
# Asset.html source paths
# ---------------------------------------------------------------------------

# Real DRL path: present on the dev workstation but absent in CI (which checks
# out only FrankCJones/resemblio-api). The parents chain from the test file:
#   parents[0] = tests/
#   parents[1] = code/api/
#   parents[2] = code/
#   parents[3] = Resemblio/
#   parents[4] = projects/
_REAL_DRL_BUTTON_PATH = (
    Path(__file__).resolve().parents[4]
    / "Design Reference Library"
    / "assets"
    / "atoms"
    / "buttons"
    / "a24-cinematic-001"
    / "asset.html"
)

# Vendored fallback: verbatim copy committed alongside this test for CI.
# See tests/fixtures/drl/a24_button/README.md for the sync policy.
_VENDORED_BUTTON_FIXTURE = (
    Path(__file__).parent / "fixtures" / "drl" / "a24_button" / "asset.html"
)


def _load_a24_asset_html() -> str:
    """Return the text of ``a24-cinematic-001/asset.html``.

    Prefers the real DRL copy when the workspace is present; falls back to the
    vendored fixture for CI environments that have only the API repo. Both
    paths return identical content. The DRL is never written to.
    """
    if _REAL_DRL_BUTTON_PATH.exists():
        return _REAL_DRL_BUTTON_PATH.read_text(encoding="utf-8")
    return _VENDORED_BUTTON_FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Minimal token set for the AssetVersion fixture
# ---------------------------------------------------------------------------

# Healthy token set sufficient to pass the quality gate (no Extraction row
# means the gate is bypassed automatically; see _is_quality_gated). These
# are the core keys the indexer uses for brand-root emission and font
# attribution. Using distinct display/body families avoids the
# display_equals_body quality penalty.
_A24_TOKENS: dict[str, str] = {
    "bg": "#FFFFFF",
    "surface": "#FAFAF7",
    "text": "#111111",
    "accent": "#000000",
    "font_body": '"GT America", "Söhne", Inter, system-ui, sans-serif',
    "font_display": '"GT America", "Söhne", Inter, system-ui, sans-serif',
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_a24_asset_version(session: Session, *, asset_class: str = "buttons") -> AssetVersion:
    """Insert an AssetVersion row representing the a24/buttons DRL asset.

    The ``dtcg_json["class"]`` value is the key that tells the
    indexer to use the real-component compose path when an asset_components
    row exists for this version. Tests can pass a corpus class alias such as
    ``"alphabets"`` to prove alias classes still hit the public template row.

    No Extraction row is created, so the quality gate is bypassed (seed
    rows with no scored extraction are treated as curatorially trusted).
    """
    dtcg: dict = {
        "schema_version": SCHEMA_V1,
        "slug": "a24",
        "class": asset_class,
        "tokens": dict(_A24_TOKENS),
    }
    av = AssetVersion(
        url="https://a24.example/",
        content_hash="test-hash-a24-cinematic-001-walking-skeleton",
        dtcg_json=dtcg,
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label="test-2026-06",
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(av)
    session.flush()
    return av


def _fetch_buttons_page(session: Session, asset_version_id: int) -> LibraryPage | None:
    """Return the library_pages row for (asset_version_id, 'buttons'), or None."""
    return session.execute(
        select(LibraryPage)
        .where(LibraryPage.asset_version_id == asset_version_id)
        .where(LibraryPage.category_slug == "buttons")
    ).scalar_one_or_none()



def _fetch_category_page(
    session: Session,
    asset_version_id: int,
    category_slug: str,
) -> LibraryPage | None:
    """Return the library_pages row for an asset/category pair, or None."""
    return session.execute(
        select(LibraryPage)
        .where(LibraryPage.asset_version_id == asset_version_id)
        .where(LibraryPage.category_slug == category_slug)
    ).scalar_one_or_none()

# ---------------------------------------------------------------------------
# Extraction-helper unit tests (validate the DRL helpers on the real asset)
# ---------------------------------------------------------------------------


def test_extract_css_contains_hover_and_focus_rules() -> None:
    """extract_component_css returns the a24 button CSS including :hover and :focus-visible.

    These are the interaction-state rules that prove the component carries real
    authored behaviour, not a stateless recolor. They must survive the comment-
    stripping pass applied by the helper.
    """
    asset_html = _load_a24_asset_html()
    css = extract_component_css(asset_html)

    assert ".btn:hover" in css, (
        ":hover rule missing from extracted CSS; "
        "check extract_component_css or the vendored fixture"
    )
    assert ".btn:focus-visible" in css, (
        ":focus-visible rule missing from extracted CSS"
    )
    # The provenance comment above the style block must be stripped.
    assert "/*" not in css, (
        "CSS comment bytes survived into extracted CSS; "
        "strip_provenance_comments must remove all /* */ blocks"
    )


def test_extract_html_contains_btn_buttons() -> None:
    """extract_component_html returns the a24 button body markup including .btn elements.

    The body carries the state-group layout (rest, hover, focus, disabled plus
    the inverse variant). At minimum the primary .btn class must survive.
    """
    asset_html = _load_a24_asset_html()
    html = extract_component_html(asset_html)

    assert 'class="btn"' in html, (
        ".btn button markup missing from extracted body; "
        "check extract_component_html or the vendored fixture"
    )
    # <style> content from <head> must not bleed into the body extract.
    assert "<style>" not in html, (
        "<style> tag appeared in extracted body; "
        "extract_component_html must return only the <body> inner HTML"
    )
    # HTML comments in the body (provenance annotations) must be stripped.
    assert "<!--" not in html, (
        "HTML comment bytes survived into extracted body"
    )


def test_derive_states_present_from_a24_css() -> None:
    """derive_states_present detects hover, focus, active, and disabled from the a24 CSS.

    The a24 asset explicitly renders all four interactive states. This test
    pins that none are dropped by the state-detection pass.
    """
    asset_html = _load_a24_asset_html()
    css = extract_component_css(asset_html)
    states = derive_states_present(css)

    assert "rest" in states, "rest state is always expected"
    assert "hover" in states, ":hover rule present in a24 CSS but 'hover' not detected"
    assert "focus" in states, ":focus-visible rule present in a24 CSS but 'focus' not detected"
    assert "active" in states, ":active rule present in a24 CSS but 'active' not detected"
    assert "disabled" in states, "[disabled] rule present in a24 CSS but 'disabled' not detected"


# ---------------------------------------------------------------------------
# End-to-end pipeline test (the walking skeleton)
# ---------------------------------------------------------------------------


def test_a24_buttons_end_to_end_pipeline(session: Session) -> None:
    """Full pipeline: a24 asset.html -> asset_components -> indexer -> LibraryPage.

    This is the walking-skeleton acceptance test for issue #4. It drives the
    real ``a24-cinematic-001`` asset through every stage of the Library v6
    pipeline on an in-memory SQLite database:

      1. Extract CSS and markup using the same helpers the seed script uses.
      2. Store the component in asset_components via insert_asset_component.
      3. Enqueue the asset_version for indexing and drain the queue.
      4. Assert the five acceptance criteria on the resulting LibraryPage.

    A passing run proves the full pipeline is wired end-to-end for one real
    brand/category. The fan-out to all brands/categories is issue #5.
    """
    asset_html = _load_a24_asset_html()

    # --- Stage 1: extract component code from the real DRL asset.html ----
    component_css = extract_component_css(asset_html)
    component_html = extract_component_html(asset_html)
    states = derive_states_present(component_css)

    # Verify the helpers produced something usable before touching the DB.
    assert ":hover" in component_css, "extraction produced no :hover rule from a24 asset.html"
    assert ":focus-visible" in component_css, "extraction produced no :focus-visible rule"
    assert 'class="btn"' in component_html, "extraction produced no .btn markup"

    # --- Stage 2: persist the component to asset_components ---------------
    av = _make_a24_asset_version(session)
    spec = AssetComponentSpec(
        fragment_key="default",
        component_html=component_html,
        component_css=component_css,
        # DRL-relative path (no absolute OS prefix); matches seed script convention.
        source_asset_path="assets/atoms/buttons/a24-cinematic-001",
        states_present=states,
    )
    insert_asset_component(session, av.id, spec)
    session.commit()

    # --- Stage 3: enqueue and drain the indexer ---------------------------
    job = enqueue_for_asset_version(session, av.id)
    assert job is not None, (
        "enqueue_for_asset_version returned None; "
        "check is_public=True on the AssetVersion and that no live job exists"
    )
    session.commit()
    drain_pending(session)

    # --- Stage 4: assert the five acceptance criteria ---------------------
    page = _fetch_buttons_page(session, av.id)
    assert page is not None, (
        "No buttons LibraryPage row after indexer drain; "
        "the indexer may have quality-gated the asset or failed silently"
    )
    html = page.rendered_html

    # Criterion 1: .btn class present (from the real DRL markup and CSS).
    assert ".btn" in html, (
        "'.btn' not found in rendered_html; "
        "the real-component path may not have fired (check asset_components row and indexer log)"
    )

    # Criterion 2: :hover rule present (proves interaction CSS survived the pipeline).
    assert ":hover" in html, (
        "':hover' not found in rendered_html; "
        "the component CSS with interaction states may not have been stored or composed"
    )

    # Criterion 3: :focus-visible rule present (accessibility interaction state).
    assert ":focus-visible" in html, (
        "':focus-visible' not found in rendered_html; "
        "check extract_component_css and insert_asset_component stored the full CSS"
    )

    # Criterion 4: real-component contract marker (set by _compose_real_component).
    assert 'data-rs-source="drl-component"' in html, (
        "'data-rs-source=\"drl-component\"' not found in rendered_html; "
        "the generic template path fired instead of the real-component path - "
        "verify the asset_components row exists and dtcg_json['class'] == 'buttons'"
    )

    # Criterion 5: generic chiclet class must be absent.
    assert ".b-btn" not in html, (
        "'.b-btn' found in rendered_html; "
        "the generic template chiclet must not appear on a real-component page - "
        "check _compose_with_gate in library_indexer.py"
    )


def test_a24_buttons_pipeline_no_generic_chiclet_even_without_explicit_component(
    session: Session,
) -> None:
    """When no asset_components row exists, rendered_html is empty, NOT a .b-btn chiclet.

    The honest-gap policy (issue #11 tracks the web-layer side): the page row
    is written with empty rendered_html so the route returns 200 with a 'not
    captured' notice rather than fabricating generic content. This test confirms
    the a24 brand does not accidentally receive the old generic-chiclet path now
    that the real-component path is in place.
    """
    av = _make_a24_asset_version(session)
    # No insert_asset_component call - the component row is intentionally absent.
    session.commit()

    job = enqueue_for_asset_version(session, av.id)
    assert job is not None
    session.commit()
    drain_pending(session)

    page = _fetch_buttons_page(session, av.id)
    assert page is not None, (
        "buttons LibraryPage row must exist even when no component is stored "
        "(the route returns 200 + empty body, not 404)"
    )
    assert page.rendered_html == "", (
        "expected empty rendered_html (honest-gap path) when no asset_components row exists, "
        f"but got content starting with: {page.rendered_html[:200]!r}"
    )
    assert ".b-btn" not in page.rendered_html, (
        "generic .b-btn chiclet appeared on a page with no asset_components row; "
        "the honest-gap path must return empty HTML, not a template stub"
    )


def test_drl_class_alias_uses_real_component_path(session: Session) -> None:
    """A plural DRL class such as ``alphabets`` renders through ``alphabet``."""
    asset_html = _load_a24_asset_html()
    component_css = extract_component_css(asset_html)
    component_html = extract_component_html(asset_html)
    states = derive_states_present(component_css)
    av = _make_a24_asset_version(session, asset_class="alphabets")
    insert_asset_component(
        session,
        av.id,
        AssetComponentSpec(
            fragment_key="default",
            component_html=component_html,
            component_css=component_css,
            source_asset_path="assets/alphabets/a24",
            states_present=states,
        ),
    )
    session.commit()

    job = enqueue_for_asset_version(session, av.id)
    assert job is not None
    session.commit()
    drain_pending(session)

    page = _fetch_category_page(session, av.id, "alphabet")
    assert page is not None
    html = page.rendered_html
    assert 'data-rs-source="drl-component"' in html
    assert ".b-btn" not in html


def test_reconcile_prefers_real_component_over_newer_generic_page(
    session: Session,
) -> None:
    """Canonical selection keeps a marker page ahead of newer generic HTML."""
    old_real = _make_a24_asset_version(session)
    old_real.fetched_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    real_page = LibraryPage(
        asset_version_id=old_real.id,
        category_slug="buttons",
        brand_slug="a24-example",
        version_label=old_real.version_label,
        rendered_html='<article data-rs-source="drl-component">real</article>',
        metadata_json={},
        is_canonical=False,
    )
    newer_generic = _make_a24_asset_version(session)
    newer_generic.fetched_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    generic_page = LibraryPage(
        asset_version_id=newer_generic.id,
        category_slug="buttons",
        brand_slug="a24-example",
        version_label=newer_generic.version_label,
        rendered_html='<article class="rs-library-page">generic</article>',
        metadata_json={},
        is_canonical=False,
    )
    session.add_all([real_page, generic_page])
    session.commit()

    from app.library_indexer import _reconcile_canonical

    _reconcile_canonical(session, newer_generic)
    session.flush()

    assert real_page.is_canonical is True
    assert generic_page.is_canonical is False
