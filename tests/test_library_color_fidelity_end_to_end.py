"""End-to-end color-fidelity tests for the library pipeline.

Phase 2 of the Inspirado-no-copiado correction plan
(``projects/OptSus Team/cto-reviews/2026-06-04-resemblio-library-inspirado-no-copiado-correction-plan.md``).

The plan calls out N-6: end-to-end color propagation from DRL extraction
through ``library_pages.metadata_json`` into rendered HTML is not yet
pinned with a single contract test. The hub-card palette derivation is
already covered by ``test_library_endpoints.py`` and the BrandCard render
is covered by the web suite. This file closes the gap between the
indexer's compose seam and the database-persisted ``rendered_html``:

(a) End-to-end: a synthetic AssetVersion carrying a known brand palette
    composes to ``library_pages`` rows whose ``rendered_html`` carries the
    literal ``--ds-accent: #FF6B35;`` (etc.) declarations.
(b) End-to-end negative: when the AssetVersion supplies no color tokens
    at all, ``rendered_html`` still paints with the contract defaults
    (``--ds-bg: #ffffff;`` etc.) rather than crashing or rendering empty.
(c) Cross-seam: the same synthetic AssetVersion that produced the
    rendered hex declarations also drives the hub palette endpoint to
    return that brand's palette in canonical-accent-first order.

The test runs the indexer end-to-end against an in-memory SQLite, the
same shape ``test_library_indexer_render_fidelity.py`` uses, so the
contract holds at the place real production traffic exercises it.

schema_version stamped on the test-fixture token bag so any future shape
drift in the test inputs is caught at fixture parse time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import LIBRARY_PAGE_METADATA_SCHEMA_VERSION, SCHEMA_V1
from app.library_indexer import drain_pending, enqueue_for_asset_version
from app.models import AssetVersion, Extraction, LibraryPage
from extractor.token_contract import BRAND_TOKEN_CONTRACT
from tests.conftest import seed_user


# ---------------------------------------------------------------------------
# Synthetic palette fixtures
# ---------------------------------------------------------------------------

# Hex values intentionally chosen NOT to appear in the BRAND_TOKEN_CONTRACT
# defaults (#ffffff, #fafafa, #111111, #0070f3) so a test failure that
# silently falls through to defaults is unambiguous.
FIXTURE_SCHEMA_VERSION = "library_color_fidelity_fixture_v1"

SYNTHETIC_PALETTE: dict[str, str] = {
    "ds-bg": "#FAF7F2",
    "ds-surface": "#EFE9DF",
    "ds-text": "#1A1208",
    "ds-text-muted": "#6B5E4F",
    "ds-accent": "#FF6B35",
    "ds-accent-2": "#004E89",
    "ds-border": "#D9CFC0",
    "ds-hairline": "#D9CFC0",
}
"""Distinct synthetic palette used as the End-to-end contract input.

Schema-versioned via ``FIXTURE_SCHEMA_VERSION``; if a future test edit
changes the shape, downstream consumers of any shared fixture export
detect the drift.
"""

SYNTHETIC_BRAND_SEED_URL = "resemblio://seed/drl_v1/inspirado-fixture/library/inspirado-snapshot"
SYNTHETIC_BRAND_SLUG = "inspirado-fixture"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_asset_version_with_palette(
    session: Session,
    *,
    palette: dict[str, str],
    url: str = SYNTHETIC_BRAND_SEED_URL,
    version_label: str = "phase2-color-fidelity",
) -> AssetVersion:
    """Insert an AssetVersion whose DTCG ``tokens`` is exactly ``palette``.

    DRL-shape (nested ``tokens`` key) so the indexer's
    ``tokens_for_compose`` reads it through the same code path real
    extractions use. Quality-gate is bypassed by attaching a passing
    Extraction in ``_attach_passing_extraction``; this helper just
    persists the source row.
    """
    av = AssetVersion(
        url=url,
        content_hash=f"phase2-color-{version_label}",
        dtcg_json={"tokens": dict(palette)},
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label=version_label,
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(av)
    session.flush()
    return av


def _attach_passing_extraction(
    session: Session, asset_version: AssetVersion, *, user_id: int
) -> Extraction:
    """Attach an Extraction with a passing quality score so the gate clears."""
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
    """Enqueue an index job for ``asset_version`` and drain one tick."""
    job = enqueue_for_asset_version(session, asset_version.id)
    assert job is not None, "enqueue helper returned None - precondition broken"
    session.commit()
    result = drain_pending(session)
    return result.pages_written


# ---------------------------------------------------------------------------
# (a) End-to-end color fidelity
# ---------------------------------------------------------------------------


def test_brand_palette_propagates_as_literal_hex_into_rendered_html(
    session: Session,
) -> None:
    """Known palette hex values appear literally in every page's rendered_html.

    Drives an AssetVersion through the indexer with ``SYNTHETIC_PALETTE``
    and asserts each color slot is emitted as a literal CSS custom-property
    declaration (``--ds-accent: #FF6B35;``) in every composed page.

    This pins N-6 from the Phase 2 plan: a future regression that drops a
    brand color silently into the BRAND_TOKEN_CONTRACT default fails this
    test (the defaults do not contain any ``SYNTHETIC_PALETTE`` value).
    """
    user, _key, _ = seed_user(session)
    av = _make_asset_version_with_palette(session, palette=SYNTHETIC_PALETTE)
    _attach_passing_extraction(session, av, user_id=user.id)

    written = _enqueue_and_drain(session, av)
    assert written > 0, "drain wrote zero pages - quality gate or compose failure"

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    assert pages, "no library_pages rows persisted for the synthetic brand"

    # Every page's rendered_html must carry each palette slot's literal
    # hex declaration. The :root block ``_emit_brand_root`` writes uses
    # ``  --<slot>: <value>;`` with two-space indent and a trailing
    # semicolon; we assert against the full declaration so a regression
    # that mangles either side (e.g. drops the leading dashes or wraps
    # the value in quotes) fails the test loudly.
    for page in pages:
        rendered = page.rendered_html
        assert isinstance(rendered, str) and rendered, (
            f"page {page.category_slug} has empty rendered_html"
        )
        for slot_name, hex_value in SYNTHETIC_PALETTE.items():
            expected_decl = f"--{slot_name}: {hex_value};"
            assert expected_decl in rendered, (
                f"page {page.category_slug}: expected literal CSS "
                f"declaration {expected_decl!r} in rendered_html; "
                f"palette propagation broke for slot {slot_name!r}"
            )


def test_metadata_envelope_carries_synthetic_palette_hex_values(
    session: Session,
) -> None:
    """The persisted ``metadata_json`` projects the brand's color slots verbatim.

    The hub palette endpoint reads color slots out of
    ``library_pages.metadata_json``; this test pins the database-side
    contract that the indexer wrote those slots from the synthetic input.
    """
    user, _key, _ = seed_user(session)
    av = _make_asset_version_with_palette(session, palette=SYNTHETIC_PALETTE)
    _attach_passing_extraction(session, av, user_id=user.id)
    _enqueue_and_drain(session, av)

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    assert pages

    for page in pages:
        envelope = page.metadata_json
        assert isinstance(envelope, dict)
        assert envelope["schema_version"] == LIBRARY_PAGE_METADATA_SCHEMA_VERSION
        # The four envelope color fields are bare-key spellings; the
        # indexer normalizes the ds- prefixed input keys, so the values
        # we expect are the bare hex literals.
        assert envelope["bg"] == SYNTHETIC_PALETTE["ds-bg"]
        assert envelope["surface"] == SYNTHETIC_PALETTE["ds-surface"]
        assert envelope["text"] == SYNTHETIC_PALETTE["ds-text"]
        assert envelope["accent"] == SYNTHETIC_PALETTE["ds-accent"]


# ---------------------------------------------------------------------------
# (b) Negative: missing palette falls through to contract defaults
# ---------------------------------------------------------------------------


def test_missing_palette_renders_contract_defaults_without_crash(
    session: Session,
) -> None:
    """A brand whose tokens carry no color slots paints from contract defaults.

    The indexer's ``_emit_brand_root`` is contract-driven: when a slot is
    absent from the brand's token bag, the BRAND_TOKEN_CONTRACT default
    is emitted. This test asserts that contract holds for color slots so
    a "no palette" brand renders sensibly rather than producing an empty
    or invalid page.
    """
    # Non-color tokens only so the brand has SOMETHING composed but no
    # color overrides. Without at least one token the quality-gate /
    # downstream path would still resolve; this shape keeps the test
    # specific to the color-fallthrough behaviour.
    no_color_tokens: dict[str, str] = {
        "ds-text-base": "16px",
        "ds-leading-normal": "1.5",
    }
    user, _key, _ = seed_user(session)
    av = _make_asset_version_with_palette(
        session,
        palette=no_color_tokens,
        url="resemblio://seed/drl_v1/no-color-fixture/library/no-color-snapshot",
        version_label="phase2-no-color",
    )
    _attach_passing_extraction(session, av, user_id=user.id)
    written = _enqueue_and_drain(session, av)
    assert written > 0, "no-color brand failed to compose any pages"

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    assert pages

    default_bg = BRAND_TOKEN_CONTRACT["slots"]["ds-bg"]["default"]
    default_surface = BRAND_TOKEN_CONTRACT["slots"]["ds-surface"]["default"]
    default_text = BRAND_TOKEN_CONTRACT["slots"]["ds-text"]["default"]
    default_accent = BRAND_TOKEN_CONTRACT["slots"]["ds-accent"]["default"]

    for page in pages:
        rendered = page.rendered_html
        # Every color slot's contract default must appear as a literal
        # declaration, proving graceful fall-through.
        assert f"--ds-bg: {default_bg};" in rendered, (
            f"page {page.category_slug} missing default --ds-bg declaration"
        )
        assert f"--ds-surface: {default_surface};" in rendered, (
            f"page {page.category_slug} missing default --ds-surface declaration"
        )
        assert f"--ds-text: {default_text};" in rendered, (
            f"page {page.category_slug} missing default --ds-text declaration"
        )
        assert f"--ds-accent: {default_accent};" in rendered, (
            f"page {page.category_slug} missing default --ds-accent declaration"
        )


# ---------------------------------------------------------------------------
# (c) Cross-seam: indexer-written palette drives the hub endpoint
# ---------------------------------------------------------------------------


def test_synthetic_brand_palette_surfaces_in_hub_endpoint(
    client: TestClient, session: Session
) -> None:
    """The hub endpoint returns the same palette the indexer just wrote.

    Closes the loop between the indexer's persistence-side contract and
    the API's read-side contract. The brand the indexer just wrote with
    ``SYNTHETIC_PALETTE`` must surface in ``/v1/library/brands`` carrying
    the canonical-accent-first palette ordering documented in the
    web ``library-data.ts`` contract.
    """
    user, _key, _ = seed_user(session)
    av = _make_asset_version_with_palette(session, palette=SYNTHETIC_PALETTE)
    _attach_passing_extraction(session, av, user_id=user.id)
    _enqueue_and_drain(session, av)
    session.commit()

    resp = client.get("/v1/library/brands")
    assert resp.status_code == 200
    rows: list[dict[str, Any]] = resp.json()["data"]["featured"]
    row = next(
        (r for r in rows if r["brand_slug"] == SYNTHETIC_BRAND_SLUG), None
    )
    assert row is not None, (
        f"synthetic brand {SYNTHETIC_BRAND_SLUG!r} missing from hub featured "
        f"list (rows: {[r['brand_slug'] for r in rows]!r})"
    )
    # Hub palette is ordered accent, bg, surface, text per the
    # ``_HUB_PALETTE_SLOTS`` tuple in app/routes/library.py; hex values
    # are lowercased during normalization.
    assert row["palette"] == [
        SYNTHETIC_PALETTE["ds-accent"].lower(),
        SYNTHETIC_PALETTE["ds-bg"].lower(),
        SYNTHETIC_PALETTE["ds-surface"].lower(),
        SYNTHETIC_PALETTE["ds-text"].lower(),
    ]
