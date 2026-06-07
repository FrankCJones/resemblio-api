"""Render-fidelity tests for the library indexer compose pipeline.

CTO TDD recovery plan Phase 1 deliverable 2
(``projects/OptSus Team/cto-reviews/2026-06-02-resemblio-library-tdd-recovery.md``)
plus the Item 4 metadata-envelope extension from
``projects/OptSus Team/cto-reviews/2026-06-02-resemblio-library-phase1-fixture-signoff.md``.

The library historically accepted brand tokens and then composed HTML that
did not project them - Lorem placeholders survived and ``_metadata_for``
read the wrong key shape. These tests load a frozen Aeon DRL fixture,
drive ``_process_job`` against an in-memory SQLite (the same pattern
``tests/test_library_indexer.py`` uses), and assert the contract at the
compose seam:

(a) every brand-token key from the fixture appears as a ``--ds-<key>:``
    CSS variable in the rendered HTML, exactly once, in the namespaced
    shape (no ``--ds-ds-bg``)
(b) no ``lorem`` substring leaks into the body
(c) no ``<!doctype`` or ``<html`` substring (body fragments only)
(d) ``metadata_json`` carries the v1 envelope with the right
    ``schema_version`` / ``brand_slug`` / ``category_slug`` / six
    color+font fields (extension Item 4 from the CTO sign-off)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.constants import LIBRARY_PAGE_METADATA_SCHEMA_VERSION, SCHEMA_V1
from app import library_indexer as library_indexer_mod
from app.library_indexer import (
    _metadata_for,
    drain_pending,
    enqueue_for_asset_version,
)
from app.models import AssetVersion, Extraction, LibraryIndexJob, LibraryPage
from tests.conftest import seed_user


# ---------------------------------------------------------------------------
# Constants - keep declarative so a future shape change has one diff site.
# ---------------------------------------------------------------------------

# The six envelope fields ``_metadata_for`` projects today. Listed by their
# bare-key spelling; the test asserts every one survives bare-vs-namespaced
# normalization regardless of input shape.
METADATA_ENVELOPE_FIELDS: tuple[str, ...] = (
    "bg",
    "surface",
    "text",
    "accent",
    "font_display",
    "font_body",
)

# Aeon seed URL convention: derive_brand_slug strips the
# resemblio://seed/<system>/<brand>/... prefix and returns the second
# segment slugified.
AEON_SEED_URL = "resemblio://seed/drl_v1/aeon/library/aeon-snapshot"
EXPECTED_BRAND_SLUG = "aeon"

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "drl" / "aeon_min"


# ---------------------------------------------------------------------------
# Fixture loaders
# ---------------------------------------------------------------------------


def _load_aeon_dtcg() -> dict[str, Any]:
    """Return the frozen Aeon DTCG payload (DRL-shape: nested ``tokens``)."""
    return json.loads((FIXTURE_DIR / "aeon_dtcg.json").read_text(encoding="utf-8"))


def _load_mixed_keys() -> dict[str, dict[str, str]]:
    """Return the three parallel token bags from ``mixed_keys.json``.

    Keys: ``bare_keys``, ``ds_prefixed_keys``, ``mixed_keys``. Each maps to
    a token dict carrying the same source values for the six envelope
    fields, differing only in key shape.
    """
    raw = json.loads((FIXTURE_DIR / "mixed_keys.json").read_text(encoding="utf-8"))
    return {
        "bare_keys": raw["bare_keys"],
        "ds_prefixed_keys": raw["ds_prefixed_keys"],
        "mixed_keys": raw["mixed_keys"],
    }


def _make_aeon_asset_version(session: Session) -> AssetVersion:
    """Insert an AssetVersion carrying the frozen Aeon DRL DTCG payload."""
    dtcg = _load_aeon_dtcg()
    row = AssetVersion(
        url=AEON_SEED_URL,
        content_hash="aeon-min-fixture-hash",
        dtcg_json=dtcg,
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label="aeon-min-fixture",
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def _attach_passing_extraction(
    session: Session, asset_version: AssetVersion, *, user_id: int
) -> Extraction:
    """Attach a high-quality extraction so the indexer quality gate passes."""
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
    """Enqueue an index job for ``asset_version`` and run one drain tick."""
    job = enqueue_for_asset_version(session, asset_version.id)
    assert job is not None, "enqueue helper returned None - precondition broken"
    session.commit()
    result = drain_pending(session)
    return result.pages_written


# ---------------------------------------------------------------------------
# Render-fidelity assertions (CTO plan items a, b, c)
# ---------------------------------------------------------------------------


def test_every_brand_token_key_renders_as_namespaced_css_variable(session: Session) -> None:
    """Every key in the Aeon fixture appears as ``--ds-<key>:`` exactly once.

    Pins items (a) + the no-double-prefix corollary from the CTO plan: a
    DRL-shape token bag (already-namespaced ``ds-*`` keys) must collapse to
    a single ``--ds-*`` form per key, with no ``--ds-ds-*`` double prefix.
    """
    user, _key, _ = seed_user(session)
    av = _make_aeon_asset_version(session)
    _attach_passing_extraction(session, av, user_id=user.id)
    written = _enqueue_and_drain(session, av)
    assert written > 0, "drain wrote zero pages - quality gate or compose failure"

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    assert pages, "no library_pages rows persisted for the Aeon asset version"

    source_tokens: dict[str, str] = _load_aeon_dtcg()["tokens"]

    for page in pages:
        rendered = page.rendered_html
        assert isinstance(rendered, str) and rendered, (
            f"page {page.category_slug} has empty rendered_html"
        )
        for raw_key in source_tokens:
            # The fixture is all already-namespaced (ds-*); after
            # normalization through _ds_var_name the var name must be the
            # raw key prefixed with --, never --ds-<raw_key>.
            expected_var = f"--{raw_key}:" if raw_key.startswith("ds-") else f"--ds-{raw_key}:"
            count = rendered.count(expected_var)
            # Font slots are intentionally emitted twice under the v2
            # inspirado-no-copiado contract: once by the brand :root
            # block (declarative truth) and once by the
            # build_font_alternative_root_block override that points
            # the variable at the loaded free alternative. Every other
            # key still appears exactly once.
            expected_count = (
                2 if raw_key in {"ds-font-display", "ds-font-body", "ds-font-mono"} else 1
            )
            assert count == expected_count, (
                f"page {page.category_slug}: expected CSS var {expected_var!r} "
                f"to appear {expected_count} times in rendered_html, found {count}"
            )
            # No double-prefix variant ever leaks through.
            assert f"--ds-{raw_key}:" not in rendered or not raw_key.startswith("ds-"), (
                f"page {page.category_slug}: double-prefixed CSS var "
                f"--ds-{raw_key} leaked into rendered_html (bug 10 regression)"
            )


def test_rendered_html_contains_no_lorem_placeholder(session: Session) -> None:
    """No ``lorem`` substring leaks through compose (item b).

    Lorem ipsum surviving in the body was the visible symptom of the
    indexer ignoring its inputs (bug 3).
    """
    user, _key, _ = seed_user(session)
    av = _make_aeon_asset_version(session)
    _attach_passing_extraction(session, av, user_id=user.id)
    _enqueue_and_drain(session, av)

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    for page in pages:
        lowered = page.rendered_html.lower()
        assert "lorem" not in lowered, (
            f"page {page.category_slug} carries 'lorem' placeholder text"
        )


def test_rendered_html_is_a_body_fragment_only(session: Session) -> None:
    """``rendered_html`` is a body fragment (no doctype / no nested document).

    Pins item (c). The web shell injects the fragment into a Next.js page;
    a nested ``<html>`` or doctype would break the parent DOM.
    """
    user, _key, _ = seed_user(session)
    av = _make_aeon_asset_version(session)
    _attach_passing_extraction(session, av, user_id=user.id)
    _enqueue_and_drain(session, av)

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    for page in pages:
        lowered = page.rendered_html.lower()
        assert "<!doctype" not in lowered, (
            f"page {page.category_slug} carries a doctype declaration"
        )
        assert "<html" not in lowered, (
            f"page {page.category_slug} carries a nested <html> tag"
        )


# ---------------------------------------------------------------------------
# Metadata-envelope assertions (CTO Item 4 extension, points 1-4)
# ---------------------------------------------------------------------------


def test_metadata_envelope_carries_v1_shape_for_every_page(session: Session) -> None:
    """Every composed page's ``metadata_json`` carries the v1 OG envelope.

    Pins Item 4 points 1-4 from the CTO sign-off:

    1. ``schema_version`` equals ``LIBRARY_PAGE_METADATA_SCHEMA_VERSION``
       (string-equality assertion, no tolerance).
    2. ``brand_slug`` matches the slug derived from the seed URL.
    3. ``category_slug`` equals the class the page was composed for.
    4. Every one of the six envelope fields is present, non-None, str.
    """
    user, _key, _ = seed_user(session)
    av = _make_aeon_asset_version(session)
    _attach_passing_extraction(session, av, user_id=user.id)
    _enqueue_and_drain(session, av)

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    assert pages, "no library_pages rows persisted for the Aeon asset version"

    for page in pages:
        envelope = page.metadata_json
        assert isinstance(envelope, dict), (
            f"page {page.category_slug} metadata_json is not a dict"
        )
        # Point 1: schema_version equals the constant, exactly.
        assert envelope["schema_version"] == LIBRARY_PAGE_METADATA_SCHEMA_VERSION
        # Point 2: brand_slug derived from the seed URL.
        assert envelope["brand_slug"] == EXPECTED_BRAND_SLUG
        # Point 3: category_slug is the class name the page was composed for.
        assert envelope["category_slug"] == page.category_slug
        # Point 4: every envelope field is present, non-None, and a string.
        for field_name in METADATA_ENVELOPE_FIELDS:
            value = envelope.get(field_name)
            assert value is not None, (
                f"page {page.category_slug} envelope field {field_name!r} is None - "
                f"bug 11 (OG metadata reading wrong key shape) regression"
            )
            assert isinstance(value, str), (
                f"page {page.category_slug} envelope field {field_name!r} is "
                f"{type(value).__name__}, expected str"
            )


# ---------------------------------------------------------------------------
# Cross-shape parity (CTO Item 4 extension, point 5)
# ---------------------------------------------------------------------------


def test_metadata_for_returns_byte_identical_envelope_across_key_shapes() -> None:
    """``_metadata_for`` envelope is byte-identical across bare / ds- / mixed inputs.

    Pins Item 4 point 5 from the CTO sign-off. The three token bags in
    ``mixed_keys.json`` carry the same source values for the six envelope
    fields; only the key shape varies. If ``_metadata_for`` reads only one
    shape (the bug 11 cause), one variant returns the right values and the
    others return null. After normalization the six envelope fields must
    be byte-identical across all three variants.
    """
    variants = _load_mixed_keys()
    envelopes = {
        name: _metadata_for("navigation", brand_slug="aeon", tokens=tokens)
        for name, tokens in variants.items()
    }
    bare = envelopes["bare_keys"]
    for variant_name in ("ds_prefixed_keys", "mixed_keys"):
        other = envelopes[variant_name]
        for field_name in METADATA_ENVELOPE_FIELDS:
            assert other[field_name] == bare[field_name], (
                f"variant {variant_name} field {field_name!r} differs from "
                f"bare_keys: {other[field_name]!r} vs {bare[field_name]!r} "
                f"(bug 11 regression: _metadata_for fails to normalize key shape)"
            )


# ---------------------------------------------------------------------------
# Render-fidelity pill assertion (Hybrid Path B button override)
# CTO packet: projects/OptSus Team/cto-reviews/2026-06-02-resemblio-button-fidelity-fix.md
# ---------------------------------------------------------------------------


# Reuse the Apple computed-styles fixture authored for the Layer 1 tests.
APPLE_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "button_fidelity"
APPLE_SEED_URL = "resemblio://seed/drl_v1/apple/library/apple-snapshot"
APPLE_EXPECTED_BRAND_SLUG = "apple"


def _seed_apple_button_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the indexer's snapshot loader at a tmp dir holding apple.json.

    The production loader looks for ``{brand_slug}.json`` under the
    vendored DRL ``_data/computed_styles`` directory. For tests we
    override the directory constant to a tmp_path and copy the Apple
    fixture into ``apple.json`` so ``_load_button_tokens("apple")`` finds
    it without touching the real vendored tree.
    """
    snapshot_dir = tmp_path / "computed_styles"
    snapshot_dir.mkdir()
    src = APPLE_FIXTURE_DIR / "apple_computed.json"
    (snapshot_dir / "apple.json").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(library_indexer_mod, "_BUTTON_SNAPSHOT_DIR", snapshot_dir)


def _make_apple_asset_version(session: Session) -> AssetVersion:
    """Insert a minimal Apple AssetVersion carrying just enough DTCG to pass quality gate."""
    # We reuse the Aeon DTCG token bag (any non-empty bag will do for compose);
    # the relevant assertion is about the button override block, not Aeon's tokens.
    dtcg = json.loads(
        (FIXTURE_DIR / "aeon_dtcg.json").read_text(encoding="utf-8")
    )
    row = AssetVersion(
        url=APPLE_SEED_URL,
        content_hash="apple-min-fixture-hash",
        dtcg_json=dtcg,
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label="apple-min-fixture",
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def test_apple_button_renders_as_pill_when_snapshot_present(
    session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Layer 3 render-fidelity gate: Apple's pill survives the compose pipeline.

    Given an Apple R3.1 computed-styles snapshot on disk, every composed
    page that contains a `.b-btn` block (in practice the `buttons` page)
    must end up with the override block writing a >=100px border-radius.
    Failing this is the headline button-fidelity regression.
    """
    _seed_apple_button_snapshot(monkeypatch, tmp_path)
    user, _key, _ = seed_user(session)
    av = _make_apple_asset_version(session)
    _attach_passing_extraction(session, av, user_id=user.id)
    written = _enqueue_and_drain(session, av)
    assert written > 0

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    # At least one page (the buttons page) carries `.b-btn`; that page
    # must show the override block with the >=100px radius. Pages
    # without `.b-btn` are no-op'd and skipped from this assertion.
    pages_with_btn = [p for p in pages if ".b-btn" in (p.rendered_html or "")]
    assert pages_with_btn, "no rendered page contains the `.b-btn` block to override"
    for page in pages_with_btn:
        assert "resemblio-button-override" in page.rendered_html, (
            f"page {page.category_slug}: override marker missing - "
            f"button-fidelity seam not engaged"
        )
        assert "border-radius: 980px !important;" in page.rendered_html, (
            f"page {page.category_slug}: Apple pill radius did not propagate "
            f"through the override - headline button-fidelity regression"
        )
        assert "font-size: 17px !important;" in page.rendered_html, (
            f"page {page.category_slug}: Apple 17px font-size did not propagate"
        )
        assert "font-weight: 400 !important;" in page.rendered_html, (
            f"page {page.category_slug}: Apple 400 weight did not propagate"
        )


def test_aeon_renders_with_free_alternative_google_fonts_link_tag(session: Session) -> None:
    """Phase 1 inspirado-no-copiado: Aeon emits a free-alternative <link> tag.

    Pre-correction (v1 L-20 fix): Aeon's stack (``PP Right Grotesk Wide`` /
    ``Academica`` / ``Atlas Typewriter`` - all private licensed faces)
    silently dropped through the Google Fonts allowlist filter and the
    page rendered in system fallbacks. Same-font-across-brands was the
    failure mode that broke the library's promise.

    Post-correction (v2): the brand-font registry pairs Aeon's
    ``PP Right Grotesk Wide`` with Plus Jakarta Sans and ``Academica``
    with Lora, both of which are on Google Fonts. The rendered HTML
    must load both free alternatives and disclose the brand's actual
    fonts in the rs-font-attribution aside.
    """
    user, _key, _ = seed_user(session)
    av = _make_aeon_asset_version(session)
    _attach_passing_extraction(session, av, user_id=user.id)
    _enqueue_and_drain(session, av)

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    for page in pages:
        html = page.rendered_html
        assert "fonts.googleapis.com/css2" in html, (
            f"page {page.category_slug}: Aeon must load a Google Fonts link "
            f"tag for its free alternatives under the v2 contract."
        )
        assert 'class="rs-font-attribution"' in html, (
            f"page {page.category_slug}: missing disclosure aside"
        )


def test_brand_with_allowlisted_font_renders_google_fonts_link_tag(session: Session) -> None:
    """L-20 happy path: an allowlisted-font brand emits the <link> tag.

    Constructs a synthetic asset version whose tokens reference Inter
    (always-allowlisted) and asserts the rendered HTML carries the
    Google Fonts link. Pins the L-20 contract end-to-end through the
    compose pipeline rather than just at the helper boundary.
    """
    user, _key, _ = seed_user(session)
    # Build a minimal token bag that pins the family stack the indexer
    # actually walks. Other DTCG fields are not required for the compose
    # path; the indexer only reads the ``tokens`` sub-dict.
    dtcg = {
        "schema_version": SCHEMA_V1,
        "tokens": {
            "ds-font-display": "Inter, sans-serif",
            "ds-font-body": "Lora, serif",
            "ds-font-mono": "JetBrains Mono, monospace",
        },
    }
    av = AssetVersion(
        url="resemblio://seed/drl_v1/synthetic-inter/library/inter-snapshot",
        content_hash="synthetic-inter-fixture-hash",
        dtcg_json=dtcg,
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label="synthetic-inter-fixture",
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(av)
    session.flush()
    _attach_passing_extraction(session, av, user_id=user.id)
    _enqueue_and_drain(session, av)

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    assert pages, "no library_pages rows persisted"
    # D2-gated showcase categories (badges, buttons, cards, etc.) return
    # empty rendered_html when the brand lacks geometry tokens. This fixture
    # intentionally carries only font tokens to isolate the font-injection
    # path. Skip empty pages; non-showcase categories always render and
    # are the surface this test covers.
    rendered_pages = [p for p in pages if p.rendered_html]
    assert rendered_pages, "no rendered pages - all categories D2-gated empty"
    for page in rendered_pages:
        assert "fonts.googleapis.com/css2" in page.rendered_html, (
            f"page {page.category_slug}: missing Google Fonts <link> tag for "
            f"a brand whose stack carries allowlisted families. L-20 fix "
            f"regression: rendered HTML must load the actual web font."
        )
        assert "family=Inter" in page.rendered_html, (
            f"page {page.category_slug}: Inter family not requested in the "
            f"Google Fonts URL"
        )


def test_aeon_button_renders_default_when_no_snapshot(session: Session) -> None:
    """No-snapshot path: Aeon renders the DRL default with no override block.

    Pins the graceful-degrade contract from the CTO packet: brands
    without an R3.1 snapshot continue to ship today's output untouched.
    """
    user, _key, _ = seed_user(session)
    av = _make_aeon_asset_version(session)
    _attach_passing_extraction(session, av, user_id=user.id)
    _enqueue_and_drain(session, av)

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    for page in pages:
        assert "resemblio-button-override" not in page.rendered_html, (
            f"page {page.category_slug}: override block injected for a brand "
            f"with no on-disk snapshot - graceful-degrade contract broken"
        )
