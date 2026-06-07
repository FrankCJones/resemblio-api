"""Tests for app.brand_capture_manifest - the per-brand provenance primitive.

TDD: these tests were written BEFORE the implementation. They define exactly
what ``build_capture_manifest`` must do and pin the "captured" rule per group
(the per-group thresholds the plan calls out as review-tunable in Section 8).

Test scenarios:
  - DRL-seed-rich brand (palette + fonts + spacing + radius; no component geometry)
  - Sparse brand (color only)
  - Empty token bag
  - Brand WITH a button snapshot (ButtonTokens present)
  - Brand with full button geometry in token bag

Phase 0 audit result encoded here: for DRL seed brands, color/typography/spacing/
radius/layout/section are captured; button/card/badge/input are NOT.
"""
from __future__ import annotations

import pytest

from app.brand_capture_manifest import (
    CAPTURE_MANIFEST_SCHEMA_VERSION,
    COMPONENT_GROUPS,
    GroupCaptureDetail,
    BrandCaptureManifest,
    build_capture_manifest,
)
from extractor.button_tokens import ButtonTokens


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A DRL-seed-style token dict: palette + fonts + spacing + radius + type scale.
# Represents what 'tokens_for_compose' returns for a typical DRL seed brand.
# Uses ds- prefix keys (how DRL stores them after the key-normalization pass).
DRL_SEED_TOKENS: dict[str, str] = {
    "ds-bg": "#ffffff",
    "ds-surface": "#f6f9fc",
    "ds-surface-2": "#edf2f7",
    "ds-text": "#0a2540",
    "ds-text-muted": "#425466",
    "ds-border": "#d4d4d4",
    "ds-hairline": "#e3e8ee",
    "ds-accent": "#635bff",
    "ds-accent-2": "#f0edff",
    "ds-focus-ring": "#635bff",
    "ds-font-display": "Sohne, system-ui",
    "ds-font-body": "Sohne, system-ui",
    "ds-font-weight-display": "600",
    "ds-font-weight-body": "400",
    "ds-font-weight-medium": "500",
    "ds-tracking-tight": "-0.02em",
    "ds-tracking-snug": "-0.018em",
    "ds-tracking-wide": "0.06em",
    "ds-tracking-wider": "0.08em",
    "ds-radius-xs": "4px",
    "ds-radius-sm": "6px",
    "ds-radius-md": "8px",
    "ds-radius-lg": "12px",
    "ds-radius-full": "9999px",
    "ds-space-1": "4px",
    "ds-space-2": "8px",
    "ds-space-3": "12px",
    "ds-space-4": "16px",
    "ds-space-6": "24px",
    "ds-space-8": "32px",
    "ds-space-12": "48px",
    "ds-space-16": "64px",
    "ds-space-24": "96px",
    "ds-page-pad-x": "32px",
    "ds-page-pad-y": "96px",
    "ds-page-max-narrow": "720px",
    "ds-page-max-default": "880px",
    "ds-page-max-wide": "1100px",
    "ds-page-max-full": "1200px",
    "ds-section-padding-x": "32px",
    "ds-section-padding-y": "96px",
    "ds-section-divider-width": "1px",
    # Type scale (extras, not in contract)
    "ds-text-xs": "11px",
    "ds-text-sm": "13px",
    "ds-text-base": "15px",
    "ds-text-lg": "18px",
    "ds-text-xl": "20px",
}

# Only a palette - brand with minimal data.
COLOR_ONLY_TOKENS: dict[str, str] = {
    "ds-bg": "#ffffff",
    "ds-accent": "#ff3366",
    "ds-text": "#111111",
}

# A minimal ButtonTokens snapshot (the CTA slot is populated).
MINIMAL_BUTTON_TOKENS: ButtonTokens = {
    "--ds-button-radius": "9999px",
    "--ds-button-padding-block": "12px",
    "--ds-button-padding-inline": "24px",
    "--ds-button-font-size": "14px",
    "--ds-button-font-weight": "600",
    "--ds-button-font-family": "system-ui",
    "--ds-button-border-width": "0px",
}

# Tokens with actual button geometry in the flat token bag.
BUTTON_GEOMETRY_TOKENS: dict[str, str] = {
    **COLOR_ONLY_TOKENS,
    "ds-button-padding-y": "12px",
    "ds-button-padding-x": "24px",
    "ds-button-border-width": "0px",
    "ds-button-radius": "9999px",
}

# Tokens with full card geometry.
CARD_GEOMETRY_TOKENS: dict[str, str] = {
    **COLOR_ONLY_TOKENS,
    "ds-card-border-width": "1px",
    "ds-card-padding": "24px",
    "ds-card-radius": "12px",
}

# Tokens with full input geometry.
INPUT_GEOMETRY_TOKENS: dict[str, str] = {
    **COLOR_ONLY_TOKENS,
    "ds-input-padding-y": "10px",
    "ds-input-border-width": "1px",
    "ds-input-radius": "6px",
}

# Tokens with full badge geometry.
BADGE_GEOMETRY_TOKENS: dict[str, str] = {
    **COLOR_ONLY_TOKENS,
    "ds-badge-padding-y": "3px",
    "ds-badge-padding-x": "10px",
}


# ---------------------------------------------------------------------------
# Schema + structure
# ---------------------------------------------------------------------------

class TestManifestStructure:
    """The manifest is schema-versioned and covers all component groups."""

    def test_schema_version_present(self) -> None:
        manifest = build_capture_manifest({})
        assert manifest["schema_version"] == CAPTURE_MANIFEST_SCHEMA_VERSION

    def test_all_component_groups_present(self) -> None:
        manifest = build_capture_manifest({})
        for group in COMPONENT_GROUPS:
            assert group in manifest["groups"], f"Missing group: {group}"

    def test_each_group_has_captured_field(self) -> None:
        manifest = build_capture_manifest({})
        for group, detail in manifest["groups"].items():
            assert "captured" in detail, f"{group} missing 'captured'"
            assert isinstance(detail["captured"], bool)

    def test_each_group_has_source_fields_detail(self) -> None:
        manifest = build_capture_manifest({})
        for group, detail in manifest["groups"].items():
            assert "present_source_fields" in detail, f"{group} missing source fields"
            assert "absent_source_fields" in detail, f"{group} missing absent source fields"

    def test_empty_bag_nothing_captured(self) -> None:
        manifest = build_capture_manifest({})
        for group in COMPONENT_GROUPS:
            assert not manifest["groups"][group]["captured"], (
                f"{group} should NOT be captured for an empty token bag"
            )


# ---------------------------------------------------------------------------
# DRL seed brand - the primary scenario
# ---------------------------------------------------------------------------

class TestDRLSeedBrand:
    """DRL seed brand: palette + fonts + geometry scale. No component geometry."""

    def test_color_captured(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        assert manifest["groups"]["color"]["captured"]

    def test_typography_captured_via_font_families(self) -> None:
        # Font families (ds-font-body, ds-font-display) are extras but they ARE
        # the signal that typography renders faithfully for DRL seed brands.
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        assert manifest["groups"]["typography"]["captured"]

    def test_spacing_captured(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        assert manifest["groups"]["spacing"]["captured"]

    def test_radius_captured(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        assert manifest["groups"]["radius"]["captured"]

    def test_layout_captured(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        assert manifest["groups"]["layout"]["captured"]

    def test_section_captured(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        assert manifest["groups"]["section"]["captured"]

    def test_button_NOT_captured_no_snapshot_no_geometry(self) -> None:
        # Core assertion: DRL seed brands have NO button geometry, NO snapshot.
        # The button showcase must NOT render with default geometry (D2).
        manifest = build_capture_manifest(DRL_SEED_TOKENS, button_tokens=None)
        assert not manifest["groups"]["button"]["captured"]

    def test_card_NOT_captured(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        assert not manifest["groups"]["card"]["captured"]

    def test_badge_NOT_captured(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        assert not manifest["groups"]["badge"]["captured"]

    def test_input_NOT_captured(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        assert not manifest["groups"]["input"]["captured"]


# ---------------------------------------------------------------------------
# Color-only brand (sparse)
# ---------------------------------------------------------------------------

class TestColorOnlyBrand:
    """Brand with only palette data: only color is captured."""

    def test_color_captured(self) -> None:
        manifest = build_capture_manifest(COLOR_ONLY_TOKENS)
        assert manifest["groups"]["color"]["captured"]

    def test_typography_NOT_captured_no_font_families(self) -> None:
        # No font-family extras, no weight/tracking slots -> NOT captured.
        manifest = build_capture_manifest(COLOR_ONLY_TOKENS)
        assert not manifest["groups"]["typography"]["captured"]

    def test_spacing_NOT_captured(self) -> None:
        manifest = build_capture_manifest(COLOR_ONLY_TOKENS)
        assert not manifest["groups"]["spacing"]["captured"]

    def test_radius_NOT_captured(self) -> None:
        manifest = build_capture_manifest(COLOR_ONLY_TOKENS)
        assert not manifest["groups"]["radius"]["captured"]

    def test_layout_NOT_captured(self) -> None:
        manifest = build_capture_manifest(COLOR_ONLY_TOKENS)
        assert not manifest["groups"]["layout"]["captured"]

    def test_button_NOT_captured(self) -> None:
        manifest = build_capture_manifest(COLOR_ONLY_TOKENS)
        assert not manifest["groups"]["button"]["captured"]


# ---------------------------------------------------------------------------
# ButtonTokens snapshot - the button-capture path
# ---------------------------------------------------------------------------

class TestButtonSnapshot:
    """A brand WITH a ButtonTokens snapshot: button IS captured."""

    def test_button_captured_via_snapshot(self) -> None:
        # Even with only a color token bag, a ButtonTokens snapshot makes
        # the button group captured. This is the signal that we have REAL
        # computed-style data for this brand's button shape.
        manifest = build_capture_manifest(COLOR_ONLY_TOKENS, button_tokens=MINIMAL_BUTTON_TOKENS)
        assert manifest["groups"]["button"]["captured"]

    def test_button_not_captured_without_snapshot_or_geometry(self) -> None:
        manifest = build_capture_manifest(COLOR_ONLY_TOKENS, button_tokens=None)
        assert not manifest["groups"]["button"]["captured"]

    def test_other_groups_unaffected_by_snapshot(self) -> None:
        # Snapshot only unlocks the button group; card/badge/input stay not-captured.
        manifest = build_capture_manifest(COLOR_ONLY_TOKENS, button_tokens=MINIMAL_BUTTON_TOKENS)
        assert not manifest["groups"]["card"]["captured"]
        assert not manifest["groups"]["badge"]["captured"]
        assert not manifest["groups"]["input"]["captured"]


# ---------------------------------------------------------------------------
# Geometry slots in token bag
# ---------------------------------------------------------------------------

class TestGeometryInTokenBag:
    """Component geometry slots in the flat token bag also count as captured."""

    def test_button_geometry_in_bag_captures_button(self) -> None:
        manifest = build_capture_manifest(BUTTON_GEOMETRY_TOKENS)
        assert manifest["groups"]["button"]["captured"]

    def test_card_geometry_in_bag_captures_card(self) -> None:
        manifest = build_capture_manifest(CARD_GEOMETRY_TOKENS)
        assert manifest["groups"]["card"]["captured"]

    def test_input_geometry_in_bag_captures_input(self) -> None:
        manifest = build_capture_manifest(INPUT_GEOMETRY_TOKENS)
        assert manifest["groups"]["input"]["captured"]

    def test_badge_geometry_in_bag_captures_badge(self) -> None:
        manifest = build_capture_manifest(BADGE_GEOMETRY_TOKENS)
        assert manifest["groups"]["badge"]["captured"]


# ---------------------------------------------------------------------------
# Key normalization edge cases
# ---------------------------------------------------------------------------

class TestKeyNormalization:
    """Token keys arrive in multiple formats; all must normalize correctly."""

    def test_bare_key_accent(self) -> None:
        # Organic row format: 'accent' (no ds- prefix)
        manifest = build_capture_manifest({"accent": "#ff3366", "bg": "#fff", "text": "#111"})
        assert manifest["groups"]["color"]["captured"]

    def test_underscored_key_font_body(self) -> None:
        # Some organic rows use underscores: 'font_body'
        manifest = build_capture_manifest({"font_body": "Inter, sans-serif", "bg": "#fff"})
        assert manifest["groups"]["typography"]["captured"]

    def test_dsprefix_key_font_display(self) -> None:
        # DRL seed format: 'ds-font-display'
        manifest = build_capture_manifest({"ds-font-display": "Sohne", "ds-bg": "#fff"})
        assert manifest["groups"]["typography"]["captured"]


# ---------------------------------------------------------------------------
# Source field detail
# ---------------------------------------------------------------------------

class TestSourceFieldDetail:
    """present_source_fields and absent_source_fields are accurate."""

    def test_color_present_fields_populated(self) -> None:
        manifest = build_capture_manifest(COLOR_ONLY_TOKENS)
        present = manifest["groups"]["color"]["present_source_fields"]
        # ds-bg, ds-accent, ds-text are in COLOR_ONLY_TOKENS
        assert len(present) >= 1

    def test_button_absent_fields_populated_for_drl_seed(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        absent = manifest["groups"]["button"]["absent_source_fields"]
        # None of the button geometry slots are in DRL_SEED_TOKENS
        assert len(absent) > 0

    def test_deterministic_output(self) -> None:
        # Two calls with the same input produce identical output.
        m1 = build_capture_manifest(DRL_SEED_TOKENS)
        m2 = build_capture_manifest(DRL_SEED_TOKENS)
        assert m1 == m2
