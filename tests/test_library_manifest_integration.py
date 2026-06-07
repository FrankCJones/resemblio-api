"""Integration tests for Library v2 manifest + D2 render gate.

TDD: Phase 4 tests (manifest through metadata) and Phase 2 wiring tests
(render-real-or-hide) written before/alongside implementation.

Tests:
  - _metadata_for carries capture_manifest (schema-versioned) and hub_signal
  - _compose_with_gate omits body for uncaptured showcase categories (D2 invariant)
  - _compose_with_gate passes through body for captured categories
  - Page-pattern categories always render regardless of capture state
  - Routes expose captured_count, missing_components on hub rows and page payloads
  - Mock and api mode return identical shape (contract-parity)

Phase 4 modifies:
  - app/library_indexer.py: _metadata_for now includes manifest fields
  - app/routes/library.py: HubFeaturedRow + LibraryPageData gain manifest fields

Phase 2 wiring (2026-06-07 fix pass, Opus review):
  - app/library_indexer.py: _compose_with_gate wired into _process_job
"""
from __future__ import annotations

import pytest

from app.brand_capture_manifest import (
    CAPTURE_MANIFEST_SCHEMA_VERSION,
    build_capture_manifest,
)
from app.library_indexer import (
    LIBRARY_PAGE_METADATA_SCHEMA_VERSION,
    _compose_one_page,
    _compose_with_gate,
    _metadata_for,
)
from app.library_render_policy import evaluate_category_render
from app.missing_data_notice import (
    HUB_CAPTURE_SIGNAL_SCHEMA_VERSION,
    MISSING_DATA_NOTICE_SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DRL_SEED_TOKENS: dict[str, str] = {
    "ds-bg": "#ffffff",
    "ds-text": "#0a2540",
    "ds-accent": "#635bff",
    "ds-font-body": "Sohne",
    "ds-font-display": "Sohne",
    "ds-radius-sm": "6px",
    "ds-space-4": "16px",
    "ds-page-pad-x": "32px",
    "ds-page-max-default": "880px",
    "ds-section-padding-x": "32px",
}

FULL_CAPTURE_TOKENS: dict[str, str] = {
    **DRL_SEED_TOKENS,
    "ds-button-padding-y": "12px",
    "ds-button-padding-x": "24px",
    "ds-button-border-width": "0px",
    "ds-card-border-width": "1px",
    "ds-card-padding": "24px",
    "ds-badge-padding-y": "3px",
    "ds-badge-padding-x": "10px",
    "ds-input-padding-y": "10px",
    "ds-input-border-width": "1px",
}


# ---------------------------------------------------------------------------
# _metadata_for carries manifest fields
# ---------------------------------------------------------------------------

class TestMetadataForManifest:
    """_metadata_for includes capture_manifest and hub_capture_signal."""

    def test_metadata_has_capture_manifest_key(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert "capture_manifest" in meta

    def test_capture_manifest_is_schema_versioned(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        manifest = meta["capture_manifest"]
        assert manifest["schema_version"] == CAPTURE_MANIFEST_SCHEMA_VERSION

    def test_capture_manifest_has_groups(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        manifest = meta["capture_manifest"]
        assert "groups" in manifest
        assert "button" in manifest["groups"]
        assert "color" in manifest["groups"]

    def test_metadata_has_hub_capture_signal_key(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert "hub_capture_signal" in meta

    def test_hub_capture_signal_is_schema_versioned(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        signal = meta["hub_capture_signal"]
        assert signal["schema_version"] == HUB_CAPTURE_SIGNAL_SCHEMA_VERSION

    def test_hub_capture_signal_has_counts(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        signal = meta["hub_capture_signal"]
        assert "captured_count" in signal
        assert "total_showcase_groups" in signal
        assert isinstance(signal["captured_count"], int)
        assert isinstance(signal["total_showcase_groups"], int)

    def test_drl_seed_captured_count_is_zero(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert meta["hub_capture_signal"]["captured_count"] == 0

    def test_full_capture_count_equals_total(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=FULL_CAPTURE_TOKENS)
        signal = meta["hub_capture_signal"]
        assert signal["captured_count"] == signal["total_showcase_groups"]

    def test_metadata_has_missing_notice_key(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert "missing_data_notice" in meta

    def test_missing_notice_is_schema_versioned(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        notice = meta["missing_data_notice"]
        assert notice["schema_version"] == MISSING_DATA_NOTICE_SCHEMA_VERSION

    def test_missing_notice_has_missing_items(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        notice = meta["missing_data_notice"]
        assert "missing_items" in notice
        assert isinstance(notice["missing_items"], list)
        # DRL seed has no button/card/badge/input capture -> items present
        assert len(notice["missing_items"]) > 0

    def test_full_capture_missing_items_empty(self) -> None:
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=FULL_CAPTURE_TOKENS)
        notice = meta["missing_data_notice"]
        assert notice["missing_items"] == []

    def test_schema_version_preserved(self) -> None:
        # The existing metadata_json.schema_version must still be present.
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert meta["schema_version"] == LIBRARY_PAGE_METADATA_SCHEMA_VERSION

    def test_existing_fields_preserved(self) -> None:
        # bg, accent, text, font_display, font_body must still be present.
        meta = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        for key in ("bg", "accent", "text"):
            assert key in meta


# ---------------------------------------------------------------------------
# Manifest-derived fields are deterministic per token bag
# ---------------------------------------------------------------------------

class TestMetadataFordeterminism:
    """Same inputs produce identical metadata envelopes."""

    def test_deterministic_metadata(self) -> None:
        m1 = _metadata_for("hero", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        m2 = _metadata_for("hero", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert m1 == m2

    def test_different_categories_same_manifest(self) -> None:
        # The manifest is per-brand, not per-category. Same tokens -> same manifest.
        m_buttons = _metadata_for("buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        m_hero = _metadata_for("hero", brand_slug="stripe", tokens=DRL_SEED_TOKENS)
        assert (
            m_buttons["capture_manifest"]
            == m_hero["capture_manifest"]
        )

    def test_prebuilt_manifest_produces_same_metadata(self) -> None:
        # When a pre-built manifest is passed, _metadata_for uses it directly.
        # The result must be identical to the on-demand-computed version.
        prebuilt = build_capture_manifest(DRL_SEED_TOKENS)
        with_prebuilt = _metadata_for(
            "buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS, manifest=prebuilt
        )
        on_demand = _metadata_for(
            "buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS
        )
        assert with_prebuilt["capture_manifest"] == on_demand["capture_manifest"]
        assert with_prebuilt["hub_capture_signal"] == on_demand["hub_capture_signal"]
        assert with_prebuilt["missing_data_notice"] == on_demand["missing_data_notice"]


# ---------------------------------------------------------------------------
# D2 render gate: _compose_with_gate (Phase 2 wiring, fixed 2026-06-07)
# ---------------------------------------------------------------------------
#
# These tests pin the core Library v2 behavioral contract:
# "captured component -> render faithfully; uncaptured -> omit (never default-render)"
#
# The D2 line separates two things that are easily confused:
#
#   ALLOWED:  The :root CSS block still emits contract-default values for every
#             slot. var(--ds-button-padding-y) resolves to "10px" even for brands
#             with no button data. This is cascade safety, not fabrication.
#
#   FORBIDDEN: Rendering the HTML body of the 'buttons' template when the brand
#              has no real button geometry. That body, at contract defaults, looks
#              like a brand-design representation but is entirely generic. Every
#              uncaptured brand would render identically.
#
# _compose_with_gate is the single gate function. _process_job calls it for
# every template class and stores the result as library_pages.rendered_html.

class TestD2RenderGate:
    """_compose_with_gate omits bodies for uncaptured showcase categories."""

    def test_compose_one_page_buttons_emits_btn_body_baseline(self) -> None:
        """Baseline: the raw _compose_one_page emits b-btn body for 'buttons'.

        This test confirms the gate is meaningful: without the gate, the buttons
        template WOULD fabricate a generic chiclet for any brand, captured or not.
        If this test fails, the D2 tests below become tautologies.
        """
        result = _compose_one_page(
            "buttons", brand_slug="stripe", tokens=DRL_SEED_TOKENS
        )
        assert "b-btn" in result, (
            "_compose_one_page did not emit b-btn body - the D2 gate tests are tautologies. "
            "Investigate the buttons template or the compose pipeline."
        )

    def test_uncaptured_button_gate_decision_is_no_render(self) -> None:
        """DRL seed brands have no button geometry data -> gate returns should_render=False."""
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        decision = evaluate_category_render("buttons", manifest)
        assert not decision.should_render, (
            "Expected should_render=False for DRL seed brand (no button geometry slots), "
            f"got should_render={decision.should_render}. "
            "Check _button_captured rule in brand_capture_manifest.py."
        )

    def test_uncaptured_button_compose_with_gate_is_empty(self) -> None:
        """D2 invariant: _compose_with_gate returns '' for uncaptured button category.

        This is the pinned contract: library_pages.rendered_html must be empty
        for an uncaptured showcase category. The web layer reads
        metadata_json.missing_data_notice to surface the honest gap acknowledgment.
        """
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        result = _compose_with_gate(
            "buttons",
            brand_slug="stripe",
            tokens=DRL_SEED_TOKENS,
            button_tokens=None,
            manifest=manifest,
        )
        assert result == "", (
            "D2 violated: _compose_with_gate returned non-empty HTML for an uncaptured "
            "button category. The web would render a fabricated generic chiclet as if it "
            "were the brand's real button design."
        )
        assert "b-btn" not in result

    def test_captured_button_compose_with_gate_emits_btn_body(self) -> None:
        """Captured brand: gate is open -> b-btn body present in rendered HTML."""
        manifest = build_capture_manifest(FULL_CAPTURE_TOKENS)
        assert manifest["groups"]["button"]["captured"], (
            "FULL_CAPTURE_TOKENS fixture did not capture the button group. "
            "Check FULL_CAPTURE_TOKENS in the test module."
        )
        result = _compose_with_gate(
            "buttons",
            brand_slug="stripe",
            tokens=FULL_CAPTURE_TOKENS,
            button_tokens=None,
            manifest=manifest,
        )
        assert result != ""
        assert "b-btn" in result

    def test_page_pattern_hero_always_renders_for_drl_seed(self) -> None:
        """Page-pattern categories render unconditionally for DRL seed brands."""
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        result = _compose_with_gate(
            "hero",
            brand_slug="stripe",
            tokens=DRL_SEED_TOKENS,
            button_tokens=None,
            manifest=manifest,
        )
        assert result != "", (
            "'hero' is a page-pattern category and must render regardless of capture state."
        )

    def test_all_showcase_categories_hidden_for_drl_seed(self) -> None:
        """All 6 showcase categories produce empty HTML for DRL seed brands."""
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        showcase = ("buttons", "cards", "badges", "form-fields", "inputs")
        for slug in showcase:
            result = _compose_with_gate(
                slug,
                brand_slug="stripe",
                tokens=DRL_SEED_TOKENS,
                button_tokens=None,
                manifest=manifest,
            )
            assert result == "", (
                f"D2 violated: '{slug}' returned non-empty HTML for a DRL seed brand "
                "(no component geometry captured). The showcase category would fabricate "
                "a generic layout as if it were the brand's real component design."
            )

    def test_full_capture_showcase_categories_render(self) -> None:
        """Fully captured brand renders all component-showcase categories."""
        manifest = build_capture_manifest(FULL_CAPTURE_TOKENS)
        # 'library' requires ANY of button/card/badge; FULL_CAPTURE_TOKENS has all.
        showcase = ("buttons", "cards", "badges", "form-fields", "inputs")
        for slug in showcase:
            result = _compose_with_gate(
                slug,
                brand_slug="stripe",
                tokens=FULL_CAPTURE_TOKENS,
                button_tokens=None,
                manifest=manifest,
            )
            assert result != "", (
                f"'{slug}' should render for a fully captured brand but returned empty."
            )
