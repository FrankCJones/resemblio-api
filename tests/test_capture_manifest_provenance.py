"""Tests for mined-component provenance in BrandCaptureManifest (issue #11).

Do this work at a level that would impress a senior developer.
Include documentation and code comments that make it easy for a future developer
to maintain this project.

RED tests - written before implementation. These pin the provenance contract
added to brand_capture_manifest.py to support honest 'X of 5 captured' counts
that reflect real OR mined component availability.

Acceptance criteria encoded
---------------------------
AC1: Given real+mined pipeline, When brand has real OR mined components,
     Then captured count reflects them accurately.
AC2: Given a mined component, When counted, Then it counts as captured BUT its
     provenance is recorded as "mined", distinct from natively-captured ("native").
AC3: Given synthesized-state pages (future issue #29), When surfaced, Then
     the 'synthesized-states' provenance value WILL be used - but no current
     code path produces it. This test suite pins that absence as the safe default.
AC4: Given a token-only/generic page with no real or mined component, When
     counted, Then it is NOT reported as captured (provenance="none").
AC5: Given the "5 showcase" framing, When a brand has mined buttons/cards/badges,
     Then build_hub_capture_signal counts them for the 'N of 5' display.

Design constraints
------------------
- mined_atom_classes is a frozenset[str] of DRL template class names that have
  mined asset_versions for this brand (e.g. frozenset({"buttons", "cards"})).
- The mapping from template class -> component groups is defined inside
  brand_capture_manifest._MINED_CLASS_TO_GROUPS (mirrors CATEGORY_CAPTURE_REQUIREMENTS
  from library_render_policy.py but avoids circular import).
- Provenance values: "native" | "mined" | "synthesized-states" | "none".
  "native" takes precedence over "mined" when both paths are active.
"""
from __future__ import annotations

import pytest

from app.brand_capture_manifest import (
    CAPTURE_MANIFEST_SCHEMA_VERSION,
    COMPONENT_GROUPS,
    build_capture_manifest,
)
from app.missing_data_notice import (
    build_hub_capture_signal,
    build_missing_notice,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# DRL seed tokens: color/typography/spacing/radius captured natively,
# but no component geometry (button/card/badge/input slots absent).
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


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


class TestSchemaVersionBump:
    """Schema version must be bumped to v2 to signal the new provenance field."""

    def test_constant_is_v2(self) -> None:
        """The module-level constant reflects the new shape."""
        assert CAPTURE_MANIFEST_SCHEMA_VERSION == "capture_manifest_v2"

    def test_manifest_carries_v2_schema_version(self) -> None:
        """Every built manifest carries the v2 schema version string."""
        manifest = build_capture_manifest({})
        assert manifest["schema_version"] == "capture_manifest_v2"


# ---------------------------------------------------------------------------
# Provenance field presence (structure guard)
# ---------------------------------------------------------------------------


class TestProvenanceFieldPresent:
    """Every group detail must include a 'provenance' key."""

    def test_all_groups_have_provenance(self) -> None:
        """Empty token bag still produces a provenance field on every group."""
        manifest = build_capture_manifest({})
        for group in COMPONENT_GROUPS:
            detail = manifest["groups"][group]
            assert "provenance" in detail, (
                f"Group '{group}' is missing the 'provenance' field"
            )

    def test_provenance_values_are_strings(self) -> None:
        """Provenance is always a string (never None or bool)."""
        manifest = build_capture_manifest(DRL_SEED_TOKENS, mined_atom_classes=frozenset({"buttons"}))
        for group, detail in manifest["groups"].items():
            assert isinstance(detail["provenance"], str), (
                f"Group '{group}' provenance is not a string: {detail['provenance']!r}"
            )

    def test_provenance_values_are_known_literals(self) -> None:
        """All provenance values are from the defined literal set."""
        valid_values = {"native", "mined", "synthesized-states", "none"}
        manifest = build_capture_manifest(DRL_SEED_TOKENS, mined_atom_classes=frozenset({"buttons"}))
        for group, detail in manifest["groups"].items():
            assert detail["provenance"] in valid_values, (
                f"Group '{group}' has unexpected provenance: {detail['provenance']!r}"
            )


# ---------------------------------------------------------------------------
# AC4: token-only brand - NOT captured, provenance="none"
# ---------------------------------------------------------------------------


class TestTokenOnlyProvenance:
    """AC4: token-only/generic page with no real or mined component is not captured."""

    def test_empty_bag_all_provenance_none(self) -> None:
        """An empty token bag produces provenance='none' for every group."""
        manifest = build_capture_manifest({})
        for group in COMPONENT_GROUPS:
            detail = manifest["groups"][group]
            assert not detail["captured"], (
                f"{group}: should not be captured for empty token bag"
            )
            assert detail["provenance"] == "none", (
                f"{group}: expected 'none', got {detail['provenance']!r}"
            )

    def test_no_mined_classes_uncaptured_groups_are_none(self) -> None:
        """DRL seed without mined classes: uncaptured groups have provenance='none'."""
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        for group, detail in manifest["groups"].items():
            if not detail["captured"]:
                assert detail["provenance"] == "none", (
                    f"{group}: uncaptured group expected 'none', got {detail['provenance']!r}"
                )


# ---------------------------------------------------------------------------
# AC2: native provenance
# ---------------------------------------------------------------------------


class TestNativeProvenance:
    """Groups captured via native token bag get provenance='native'."""

    def test_color_native_when_palette_supplied(self) -> None:
        """The three primary palette slots make color group provenance='native'."""
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        detail = manifest["groups"]["color"]
        assert detail["captured"] is True
        assert detail["provenance"] == "native"

    def test_typography_native_when_font_families_supplied(self) -> None:
        """Font-family extras (ds-font-body, ds-font-display) produce 'native'."""
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        detail = manifest["groups"]["typography"]
        assert detail["captured"] is True
        assert detail["provenance"] == "native"

    def test_button_native_when_geometry_in_tokens(self) -> None:
        """Core button geometry slots in the token bag give provenance='native'."""
        tokens = {
            **DRL_SEED_TOKENS,
            "ds-button-padding-y": "12px",
            "ds-button-padding-x": "24px",
            "ds-button-border-width": "0px",
        }
        manifest = build_capture_manifest(tokens)
        detail = manifest["groups"]["button"]
        assert detail["captured"] is True
        assert detail["provenance"] == "native"

    def test_native_wins_over_mined_when_both_active(self) -> None:
        """Native provenance takes precedence over mined when both paths are active.

        A brand that has both button geometry in its token bag AND a mined buttons
        asset_version is natively-captured; the mined asset_version is redundant.
        """
        tokens = {
            **DRL_SEED_TOKENS,
            "ds-button-padding-y": "12px",
            "ds-button-padding-x": "24px",
            "ds-button-border-width": "0px",
        }
        manifest = build_capture_manifest(tokens, mined_atom_classes=frozenset({"buttons"}))
        detail = manifest["groups"]["button"]
        assert detail["captured"] is True
        assert detail["provenance"] == "native", (
            "Native capture takes precedence; 'mined' would misrepresent the higher-fidelity source"
        )


# ---------------------------------------------------------------------------
# AC1 + AC2: mined provenance
# ---------------------------------------------------------------------------


class TestMinedProvenance:
    """AC1+AC2: mined components count as captured with provenance='mined'."""

    def test_mined_buttons_captures_button_group(self) -> None:
        """AC1: brand with mined buttons asset has button group captured."""
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"buttons"})
        )
        assert manifest["groups"]["button"]["captured"] is True

    def test_mined_buttons_provenance_is_mined(self) -> None:
        """AC2: mined-sourced capture has provenance='mined', not 'native'."""
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"buttons"})
        )
        assert manifest["groups"]["button"]["provenance"] == "mined"

    def test_mined_buttons_does_not_capture_card(self) -> None:
        """Mined 'buttons' class only covers the button group, not card."""
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"buttons"})
        )
        assert manifest["groups"]["card"]["captured"] is False
        assert manifest["groups"]["card"]["provenance"] == "none"

    def test_mined_cards_captures_card_group(self) -> None:
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"cards"})
        )
        detail = manifest["groups"]["card"]
        assert detail["captured"] is True
        assert detail["provenance"] == "mined"

    def test_mined_badges_captures_badge_group(self) -> None:
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"badges"})
        )
        detail = manifest["groups"]["badge"]
        assert detail["captured"] is True
        assert detail["provenance"] == "mined"

    def test_mined_form_fields_captures_input_group(self) -> None:
        """'form-fields' template class maps to the 'input' component group."""
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"form-fields"})
        )
        detail = manifest["groups"]["input"]
        assert detail["captured"] is True
        assert detail["provenance"] == "mined"

    def test_mined_inputs_captures_input_group(self) -> None:
        """'inputs' template class also maps to the 'input' component group."""
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"inputs"})
        )
        detail = manifest["groups"]["input"]
        assert detail["captured"] is True
        assert detail["provenance"] == "mined"

    def test_mined_library_covers_button_card_badge(self) -> None:
        """'library' mined class covers button + card + badge (composite showcase)."""
        manifest = build_capture_manifest({}, mined_atom_classes=frozenset({"library"}))
        assert manifest["groups"]["button"]["captured"] is True
        assert manifest["groups"]["button"]["provenance"] == "mined"
        assert manifest["groups"]["card"]["captured"] is True
        assert manifest["groups"]["card"]["provenance"] == "mined"
        assert manifest["groups"]["badge"]["captured"] is True
        assert manifest["groups"]["badge"]["provenance"] == "mined"
        # input is NOT covered by 'library'
        assert manifest["groups"]["input"]["captured"] is False

    def test_multiple_mined_classes_each_cover_their_group(self) -> None:
        """Multiple simultaneous mined classes each mark their respective group."""
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS,
            mined_atom_classes=frozenset({"buttons", "cards"}),
        )
        assert manifest["groups"]["button"]["provenance"] == "mined"
        assert manifest["groups"]["card"]["provenance"] == "mined"
        # badge and input were not mined
        assert manifest["groups"]["badge"]["captured"] is False
        assert manifest["groups"]["input"]["captured"] is False

    def test_empty_mined_classes_frozenset_same_as_no_arg(self) -> None:
        """frozenset() mined_atom_classes is identical to the default (no mined effect)."""
        manifest_default = build_capture_manifest(DRL_SEED_TOKENS)
        manifest_empty = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset()
        )
        for group in COMPONENT_GROUPS:
            assert manifest_default["groups"][group]["captured"] == manifest_empty["groups"][group]["captured"]
            assert manifest_default["groups"][group]["provenance"] == manifest_empty["groups"][group]["provenance"]

    def test_unknown_mined_class_is_silently_ignored(self) -> None:
        """An unknown mined atom class (no group mapping) does not crash or alter state."""
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"unknown-component-class"})
        )
        # All component groups retain their token-only status
        assert manifest["groups"]["button"]["captured"] is False
        assert manifest["groups"]["button"]["provenance"] == "none"


# ---------------------------------------------------------------------------
# AC3: synthesized-states provenance (future, not produced today)
# ---------------------------------------------------------------------------


class TestSynthesizedStatesProvenance:
    """AC3: 'synthesized-states' provenance is defined but produced by issue #29.

    This test class pins the ABSENCE of false-positive 'synthesized-states'
    in all current code paths (native token capture + mined). When #29 lands,
    tests that ASSERT the presence of 'synthesized-states' will be added there.
    """

    def test_no_synthesized_states_from_native_path(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        for group, detail in manifest["groups"].items():
            assert detail["provenance"] != "synthesized-states", (
                f"Group '{group}': 'synthesized-states' must not appear from token-only path"
            )

    def test_no_synthesized_states_from_mined_path(self) -> None:
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"buttons"})
        )
        for group, detail in manifest["groups"].items():
            assert detail["provenance"] != "synthesized-states", (
                f"Group '{group}': 'synthesized-states' must not appear from mined path"
            )


# ---------------------------------------------------------------------------
# AC5: hub signal counts mined as captured
# ---------------------------------------------------------------------------


class TestHubSignalWithMined:
    """AC5: build_hub_capture_signal includes mined components in the count."""

    def test_drl_seed_no_mined_is_zero(self) -> None:
        """Baseline: DRL seed without mined components is 0 of 5."""
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        signal = build_hub_capture_signal(manifest)
        assert signal.captured_count == 0

    def test_mined_buttons_increments_hub_count_to_one(self) -> None:
        """AC5: mined buttons → 'buttons' showcase category counts in hub signal."""
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"buttons"})
        )
        signal = build_hub_capture_signal(manifest)
        assert signal.captured_count == 1, (
            f"Expected 1 (buttons via mined), got {signal.captured_count}"
        )

    def test_mined_buttons_cards_badges_is_three(self) -> None:
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS,
            mined_atom_classes=frozenset({"buttons", "cards", "badges"}),
        )
        signal = build_hub_capture_signal(manifest)
        assert signal.captured_count == 3

    def test_total_showcase_groups_unchanged_by_mined(self) -> None:
        """Mined components increase captured_count but not total_showcase_groups."""
        manifest_mined = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"buttons"})
        )
        manifest_baseline = build_capture_manifest(DRL_SEED_TOKENS)
        signal_mined = build_hub_capture_signal(manifest_mined)
        signal_baseline = build_hub_capture_signal(manifest_baseline)
        assert signal_mined.total_showcase_groups == signal_baseline.total_showcase_groups


# ---------------------------------------------------------------------------
# Mined components must NOT appear in the missing-data notice
# ---------------------------------------------------------------------------


class TestMinedNotInMissingNotice:
    """Mined-but-real components must NOT appear in the honest-gap notice.

    The honest-gap notice is for TRULY absent classes (issue #6). A mined
    component is real extracted brand code, so it is not a gap.
    """

    def test_mined_buttons_absent_from_missing_items(self) -> None:
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"buttons"})
        )
        summary = build_missing_notice(manifest)
        missing_slugs = {item.category_slug for item in summary.missing_items}
        assert "buttons" not in missing_slugs, (
            "Mined buttons are real captured code; they must not appear as a gap"
        )

    def test_mined_cards_absent_from_missing_items(self) -> None:
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"cards"})
        )
        summary = build_missing_notice(manifest)
        missing_slugs = {item.category_slug for item in summary.missing_items}
        assert "cards" not in missing_slugs

    def test_truly_absent_group_still_in_missing_items(self) -> None:
        """Groups that are neither native nor mined still appear in the gap notice."""
        # Only buttons is mined; cards/badges/inputs are still absent
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"buttons"})
        )
        summary = build_missing_notice(manifest)
        missing_slugs = {item.category_slug for item in summary.missing_items}
        assert "cards" in missing_slugs
        assert "badges" in missing_slugs
