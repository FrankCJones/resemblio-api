"""Tests for app.missing_data_notice - honest gap acknowledgment (Phase 3).

TDD: tests written BEFORE implementation. These pin:
  - build_missing_notice returns a sorted, human-readable list of uncaptured
    showcase groups for a brand's manifest.
  - A complete brand returns [].
  - The notice is deterministic, neutral, non-fabricated.
  - captured_count and total_showcase_groups are correct for hub cards.
  - The copy passes the banned-framing guard (no fabricated brand claims).
"""
from __future__ import annotations

import pytest

from app.brand_capture_manifest import build_capture_manifest
from app.missing_data_notice import (
    SHOWCASE_CATEGORY_DISPLAY_NAMES,
    MissingDataSummary,
    build_missing_notice,
    build_hub_capture_signal,
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
# SHOWCASE_CATEGORY_DISPLAY_NAMES
# ---------------------------------------------------------------------------

class TestDisplayNames:
    """Every showcase category has a human-readable display name."""

    def test_buttons_display_name(self) -> None:
        assert SHOWCASE_CATEGORY_DISPLAY_NAMES["buttons"] == "Buttons"

    def test_cards_display_name(self) -> None:
        assert SHOWCASE_CATEGORY_DISPLAY_NAMES["cards"] == "Cards"

    def test_badges_display_name(self) -> None:
        assert SHOWCASE_CATEGORY_DISPLAY_NAMES["badges"] == "Badges"

    def test_form_fields_display_name(self) -> None:
        assert SHOWCASE_CATEGORY_DISPLAY_NAMES["form-fields"] == "Form Fields"

    def test_inputs_display_name(self) -> None:
        assert SHOWCASE_CATEGORY_DISPLAY_NAMES["inputs"] == "Inputs"


# ---------------------------------------------------------------------------
# build_missing_notice
# ---------------------------------------------------------------------------

class TestBuildMissingNotice:
    """build_missing_notice returns the right list."""

    def test_drl_seed_missing_list(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        summary = build_missing_notice(manifest)
        assert isinstance(summary, MissingDataSummary)
        # DRL seed brands lack button/card/badge/input capture
        missing_names = [item.display_name for item in summary.missing_items]
        assert "Buttons" in missing_names
        assert "Cards" in missing_names
        assert "Badges" in missing_names
        assert "Form Fields" in missing_names
        assert "Inputs" in missing_names

    def test_full_capture_returns_empty_list(self) -> None:
        manifest = build_capture_manifest(FULL_CAPTURE_TOKENS)
        summary = build_missing_notice(manifest)
        assert summary.missing_items == ()

    def test_deterministic_order(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        s1 = build_missing_notice(manifest)
        s2 = build_missing_notice(manifest)
        assert s1 == s2

    def test_empty_bag_all_showcase_missing(self) -> None:
        manifest = build_capture_manifest({})
        summary = build_missing_notice(manifest)
        # An empty token bag has no captured showcase groups
        assert len(summary.missing_items) > 0

    def test_no_fabricated_content_in_notice(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        summary = build_missing_notice(manifest)
        # The notice must not contain hedging or apologetic language.
        # It should be factual: "not captured" not "we're sorry" or "coming soon".
        for item in summary.missing_items:
            assert item.display_name  # non-empty
            assert isinstance(item.display_name, str)
            # No AI-power marketing speak
            assert "AI-powered" not in item.display_name
            assert "amazing" not in item.display_name.lower()


# ---------------------------------------------------------------------------
# build_hub_capture_signal
# ---------------------------------------------------------------------------

class TestBuildHubCaptureSignal:
    """build_hub_capture_signal returns the right counts for hub cards."""

    def test_drl_seed_captured_count(self) -> None:
        manifest = build_capture_manifest(DRL_SEED_TOKENS)
        signal = build_hub_capture_signal(manifest)
        # DRL seed has 0 of the showcase categories captured
        assert signal.captured_count == 0
        assert signal.total_showcase_groups > 0

    def test_full_capture_all_captured(self) -> None:
        manifest = build_capture_manifest(FULL_CAPTURE_TOKENS)
        signal = build_hub_capture_signal(manifest)
        assert signal.captured_count == signal.total_showcase_groups
        assert signal.captured_count > 0

    def test_partial_capture_correct_count(self) -> None:
        # Only button geometry captured.
        partial_tokens = {
            **DRL_SEED_TOKENS,
            "ds-button-padding-y": "12px",
            "ds-button-padding-x": "24px",
            "ds-button-border-width": "0px",
        }
        manifest = build_capture_manifest(partial_tokens)
        signal = build_hub_capture_signal(manifest)
        assert signal.captured_count == 1
        assert signal.total_showcase_groups > 1

    def test_schema_version_present(self) -> None:
        manifest = build_capture_manifest({})
        signal = build_hub_capture_signal(manifest)
        assert signal.schema_version is not None
        assert "v1" in signal.schema_version


# ---------------------------------------------------------------------------
# hub_capture_signal_from_captured_groups (issue #11 hub-aggregation helper)
# ---------------------------------------------------------------------------
#
# This helper is the single source of truth for the "N of 5 captured" count rule.
# Both build_hub_capture_signal (per-manifest path) and the hub route's
# cross-page union aggregation (_hub_meta_for_brand) must delegate here so that
# both surfaces produce identical counts for the same captured-group set.


class TestHubCaptureSignalFromCapturedGroups:
    """hub_capture_signal_from_captured_groups: count rule unit tests (issue #11).

    RED tests - written before the function exists. These pin:
      - empty frozenset -> captured_count = 0
      - frozenset({"button"}) -> 1 (satisfies 'buttons' showcase category)
      - frozenset({"button","card","badge"}) -> 3 distinct showcase categories
      - total_showcase_groups is always 5 (the fixed primary-showcase count)
      - Result matches build_hub_capture_signal for the same group set (one source of truth)
    """

    def test_empty_set_returns_zero(self) -> None:
        """Empty group set -> captured_count = 0."""
        from app.missing_data_notice import hub_capture_signal_from_captured_groups
        signal = hub_capture_signal_from_captured_groups(frozenset())
        assert signal.captured_count == 0

    def test_button_group_only_counts_buttons_showcase_category(self) -> None:
        """frozenset({"button"}) -> 1: satisfies the 'buttons' showcase category."""
        from app.missing_data_notice import hub_capture_signal_from_captured_groups
        signal = hub_capture_signal_from_captured_groups(frozenset({"button"}))
        assert signal.captured_count == 1

    def test_button_card_badge_groups_returns_three(self) -> None:
        """frozenset({"button","card","badge"}) -> 3 distinct showcase categories."""
        from app.missing_data_notice import hub_capture_signal_from_captured_groups
        signal = hub_capture_signal_from_captured_groups(frozenset({"button", "card", "badge"}))
        assert signal.captured_count == 3

    def test_total_showcase_groups_is_always_five(self) -> None:
        """total_showcase_groups is 5 regardless of how many groups are captured."""
        from app.missing_data_notice import hub_capture_signal_from_captured_groups
        assert hub_capture_signal_from_captured_groups(frozenset()).total_showcase_groups == 5
        assert hub_capture_signal_from_captured_groups(
            frozenset({"button", "card"})
        ).total_showcase_groups == 5

    def test_result_matches_build_hub_capture_signal_for_same_group_set(self) -> None:
        """Both paths produce identical counts for the same group set (one source of truth)."""
        from app.missing_data_notice import hub_capture_signal_from_captured_groups
        manifest = build_capture_manifest(
            DRL_SEED_TOKENS, mined_atom_classes=frozenset({"buttons", "cards"})
        )
        via_manifest = build_hub_capture_signal(manifest)
        via_groups = hub_capture_signal_from_captured_groups(frozenset({"button", "card"}))
        assert via_manifest.captured_count == via_groups.captured_count
        assert via_manifest.total_showcase_groups == via_groups.total_showcase_groups
