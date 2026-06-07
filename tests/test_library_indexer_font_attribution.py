"""Disclosure-injection tests for the library indexer compose pipeline.

Phase 1 of the inspirado-no-copiado correction (Frank, 2026-06-04
02:35 UTC; plan at
``projects/OptSus Team/cto-reviews/2026-06-04-resemblio-library-inspirado-no-copiado-correction-plan.md``).

The indexer must inject, into every rendered page:

1. A Google Fonts ``<link>`` tag pointing at the free alternative
   chosen by ``app.brand_font_registry`` for the brand's first-preference
   font (per slot).
2. An ``<aside class="rs-font-attribution">`` disclosure block carrying
   the brand's actual font name + the free-alternative attribution.
3. A ``:root`` override block that points ``--ds-font-display`` /
   ``--ds-font-body`` / ``--ds-font-mono`` at the free-alternative
   families so the loaded face actually paints the specimen.

These assertions run against synthetic AssetVersion rows so the test
is fast and deterministic; the heavier DRL-fixture path is exercised
by ``test_library_indexer_render_fidelity.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.constants import SCHEMA_V1
from app.library_indexer import (
    drain_pending,
    enqueue_for_asset_version,
)
from app.models import AssetVersion, Extraction, LibraryPage
from tests.conftest import seed_user


# Synthetic brand DTCG payloads keyed by brand slug. The values mirror
# the shape DRL-seeded brands carry: a nested ``tokens`` dict with
# already-namespaced ``ds-*`` keys.
_BRAND_FIXTURES: dict[str, dict[str, Any]] = {
    "aeon": {
        "url": "resemblio://seed/drl_v1/aeon/library/aeon-snapshot",
        "tokens": {
            "ds-bg": "#0a0a0a",
            "ds-surface": "#1a1a1a",
            "ds-text": "#f5f5f5",
            "ds-accent": "#c97b3a",
            "ds-font-display": (
                "'PP Right Grotesk Wide', 'Helvetica Neue', sans-serif"
            ),
            "ds-font-body": "'Academica', Georgia, serif",
            "ds-font-mono": "'Atlas Typewriter', Consolas, monospace",
            # D2 geometry tokens: required for badge/button/card/input
            # categories to pass the Library v2 capture gate and render
            # non-empty HTML (brand_capture_manifest.py thresholds).
            "ds-badge-padding-y": "4px",
            "ds-badge-padding-x": "10px",
            "ds-button-padding-y": "12px",
            "ds-button-padding-x": "22px",
            "ds-button-border-width": "1px",
            "ds-card-padding": "24px",
            "ds-card-border-width": "1px",
            "ds-input-padding-y": "12px",
            "ds-input-border-width": "1px",
        },
        "expected_brand_font_name": "PP Right Grotesk Wide",
        "expected_free_alternative": "Plus Jakarta Sans",
        "expected_free_alternative_designer": "Tokotype",
        "expected_brand_display_name": "Aeon",
    },
    "openai": {
        "url": "resemblio://seed/drl_v1/openai/library/openai-snapshot",
        "tokens": {
            "ds-bg": "#ffffff",
            "ds-surface": "#f7f7f8",
            "ds-text": "#0d0d0d",
            "ds-accent": "#10a37f",
            "ds-font-display": "'Sohne', 'Helvetica Neue', sans-serif",
            "ds-font-body": "'Sohne', system-ui, sans-serif",
            "ds-badge-padding-y": "2px",
            "ds-badge-padding-x": "8px",
            "ds-button-padding-y": "8px",
            "ds-button-padding-x": "16px",
            "ds-button-border-width": "1px",
            "ds-card-padding": "20px",
            "ds-card-border-width": "1px",
            "ds-input-padding-y": "8px",
            "ds-input-border-width": "1px",
        },
        "expected_brand_font_name": "Sohne",
        "expected_free_alternative": "Inter",
        "expected_free_alternative_designer": "Rasmus Andersson",
        "expected_brand_display_name": "OpenAI",
    },
    "stripe": {
        "url": "resemblio://seed/drl_v1/stripe/library/stripe-snapshot",
        "tokens": {
            "ds-bg": "#ffffff",
            "ds-surface": "#f6f9fc",
            "ds-text": "#0a2540",
            "ds-accent": "#635bff",
            "ds-font-display": "'Sohne', 'Helvetica Neue', sans-serif",
            "ds-font-body": "'Sohne', system-ui, sans-serif",
            "ds-badge-padding-y": "3px",
            "ds-badge-padding-x": "10px",
            "ds-button-padding-y": "10px",
            "ds-button-padding-x": "18px",
            "ds-button-border-width": "1px",
            "ds-card-padding": "24px",
            "ds-card-border-width": "1px",
            "ds-input-padding-y": "10px",
            "ds-input-border-width": "1px",
        },
        "expected_brand_font_name": "Sohne",
        "expected_free_alternative": "Inter",
        "expected_free_alternative_designer": "Rasmus Andersson",
        "expected_brand_display_name": "Stripe",
    },
}


def _make_asset_version(session: Session, brand_slug: str) -> AssetVersion:
    """Insert an AssetVersion carrying the synthetic fixture for ``brand_slug``."""
    fixture = _BRAND_FIXTURES[brand_slug]
    row = AssetVersion(
        url=fixture["url"],
        content_hash=f"{brand_slug}-disclosure-fixture-hash",
        dtcg_json={"tokens": fixture["tokens"]},
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label=f"{brand_slug}-disclosure-fixture",
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def _attach_passing_extraction(
    session: Session, asset_version: AssetVersion, *, user_id: int
) -> Extraction:
    """Attach a passing extraction so the quality gate lets the job through."""
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
    """Enqueue an index job for ``asset_version`` and drive one drain tick."""
    job = enqueue_for_asset_version(session, asset_version.id)
    assert job is not None
    session.commit()
    result = drain_pending(session)
    return result.pages_written


@pytest.mark.parametrize("brand_slug", ["aeon", "openai", "stripe"])
def test_rendered_html_carries_font_disclosure_aside(
    session: Session, brand_slug: str
) -> None:
    """Every rendered page contains the rs-font-attribution aside."""
    fixture = _BRAND_FIXTURES[brand_slug]
    user, _key, _ = seed_user(session)
    av = _make_asset_version(session, brand_slug)
    _attach_passing_extraction(session, av, user_id=user.id)
    written = _enqueue_and_drain(session, av)
    assert written > 0

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    assert pages, f"no library_pages rows for {brand_slug}"
    for page in pages:
        html = page.rendered_html
        assert 'class="rs-font-attribution"' in html, (
            f"{brand_slug}/{page.category_slug}: disclosure aside missing"
        )
        assert (
            f"{fixture['expected_brand_display_name']} uses "
            f"{fixture['expected_brand_font_name']}"
        ) in html, (
            f"{brand_slug}/{page.category_slug}: disclosure headline missing"
        )
        assert fixture["expected_free_alternative"] in html
        assert fixture["expected_free_alternative_designer"] in html
        assert "free, designed by" in html


@pytest.mark.parametrize("brand_slug", ["aeon", "openai", "stripe"])
def test_rendered_html_loads_free_alternative_via_google_fonts(
    session: Session, brand_slug: str
) -> None:
    """Every rendered page carries a Google Fonts link tag for the free alternative."""
    fixture = _BRAND_FIXTURES[brand_slug]
    user, _key, _ = seed_user(session)
    av = _make_asset_version(session, brand_slug)
    _attach_passing_extraction(session, av, user_id=user.id)
    _enqueue_and_drain(session, av)

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    assert pages
    expected_url_fragment = (
        f"family={fixture['expected_free_alternative'].replace(' ', '+')}"
        ":wght@300..700"
    )
    for page in pages:
        html = page.rendered_html
        assert "fonts.googleapis.com/css2" in html, (
            f"{brand_slug}/{page.category_slug}: Google Fonts link tag missing"
        )
        assert expected_url_fragment in html, (
            f"{brand_slug}/{page.category_slug}: free alternative "
            f"{fixture['expected_free_alternative']!r} not requested"
        )
        assert "display=swap" in html


@pytest.mark.parametrize("brand_slug", ["aeon", "openai", "stripe"])
def test_rendered_html_overrides_ds_font_variables_to_free_alternative(
    session: Session, brand_slug: str
) -> None:
    """The font-alternative root block must override --ds-font-* in the rendered HTML."""
    fixture = _BRAND_FIXTURES[brand_slug]
    user, _key, _ = seed_user(session)
    av = _make_asset_version(session, brand_slug)
    _attach_passing_extraction(session, av, user_id=user.id)
    _enqueue_and_drain(session, av)

    pages = session.query(LibraryPage).filter_by(asset_version_id=av.id).all()
    assert pages
    expected_family = fixture["expected_free_alternative"]
    for page in pages:
        html = page.rendered_html
        # The display-slot override must point at the free alternative.
        assert (
            f"--ds-font-display: '{expected_family}'"
            in html
        ), (
            f"{brand_slug}/{page.category_slug}: --ds-font-display override missing "
            f"for {expected_family}"
        )
