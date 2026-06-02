"""Tests for `app.quality_heuristics` default-detection penalties.

The flagship assertion: feeding the Susann raw extracted tokens (verbatim
from the extraction-fidelity-finding 2026-05-31 evidence block) through
`compute_quality_score` followed by `apply_heuristic_penalties` yields a
penalized score below `DEFAULT_THRESHOLD_V1_1_X`. This is the regression
test that documents "the extractor returned defaults; the quality scorer
should not have called it healthy."
"""
from __future__ import annotations

import pytest

from app.constants import (
    PENALTY_ACCENT_DIVERSITY,
    PENALTY_ACCENT_TEXT_LAB_THRESHOLD,
    PENALTY_DISPLAY_EQUALS_BODY,
)
from app.quality_heuristics import (
    COMMON_DEFAULT_COLORS_PENALTY,
    QUALITY_HEURISTICS_SCHEMA_VERSION,
    SYSTEM_FONT_STACK_PENALTY,
    _delta_e_cie76,
    _detect_display_equals_body,
    _detect_missing_accent_diversity,
    apply_heuristic_penalties,
)
from app.quality_scoring import compute_quality_score
from app.scoring_weights import DEFAULT_THRESHOLD_V1_1_X


# The Susann extraction's flat TokenSet form, derived from the raw DTCG JSON
# evidence block in `projects/Resemblio/02-prd/2026-05-31-extraction-fidelity-finding-susann.md`.
# The raw file uses nested category objects (color.bg, fontFamily.font-body);
# this fixture flattens them to the snake-case keys the scorer reads.
SUSANN_EXTRACTED_TOKENS: dict[str, str] = {
    "bg": "#f5f5f5",
    "text": "#1a1a1a",
    "accent": "#4f46e5",
    "border": "#d1d5db",  # gray-300; not in our default set but valid populated value
    "surface": "#ffffff",
    "hairline": "#e5e7eb",  # gray-200; not in our default set
    "text_muted": "#6b7280",  # gray-500; not in our default set
    "font_body": "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    "font_mono": "'Courier New', Courier, monospace",
    "font_display": "Georgia, 'Times New Roman', serif",
    "text_lg": "1.125rem",
    "text_xl": "1.25rem",
    "text_2xl": "1.5rem",
    "text_3xl": "1.875rem",
    "text_4xl": "2.25rem",
    "text_base": "1rem",
    "space_1": "4px",
    "space_2": "8px",
    "space_3": "12px",
    "space_4": "16px",
    "space_5": "20px",
    "space_6": "24px",
}


def test_schema_version_constant_shape() -> None:
    """The schema version stays a simple semver-major string for cheap diffs.

    Bumped to ``1.1`` on 2026-06-02 with the R3 additions (accent-diversity
    and display-equals-body penalties). Additive change; existing penalty
    names and the result shape are unchanged.
    """
    assert QUALITY_HEURISTICS_SCHEMA_VERSION == "1.1"


def test_apply_penalties_no_op_on_distinctive_brand_tokens() -> None:
    """A rich, brand-distinctive palette + custom fonts triggers no penalties."""
    tokens = {
        "bg": "#0B0B0F",
        "text": "#F5F2EA",
        "accent": "#FBE71F",
        "text_muted": "#999988",
        "border": "#222222",
        "font_body": "Inter, sans-serif",
        "font_display": "Anton, sans-serif",
    }
    base = compute_quality_score(tokens)
    out = apply_heuristic_penalties(tokens, base)
    assert out.penalties_applied == ()
    assert out.penalized_score == base.composite_score
    assert out.diagnostic == "no penalties"


def test_apply_penalties_triggers_on_all_system_fonts() -> None:
    """All-system-stack fonts (even with distinctive colors) drops the score."""
    tokens = {
        "bg": "#0B0B0F",
        "text": "#F5F2EA",
        "accent": "#FBE71F",
        "font_body": "system-ui, -apple-system, sans-serif",
        "font_display": "Georgia, 'Times New Roman', serif",
    }
    base = compute_quality_score(tokens)
    out = apply_heuristic_penalties(tokens, base)
    assert "all_system_font_stack" in out.penalties_applied
    assert "all_common_default_colors" not in out.penalties_applied
    assert out.penalized_score == pytest.approx(base.composite_score - SYSTEM_FONT_STACK_PENALTY)


def test_apply_penalties_triggers_on_all_default_colors() -> None:
    """All-default colors (even with custom fonts) drops the score."""
    tokens = {
        "bg": "#ffffff",
        "text": "#1a1a1a",
        "accent": "#4f46e5",
        "surface": "#f5f5f5",
        "font_body": "Inter, sans-serif",
        "font_display": "Anton, sans-serif",
    }
    base = compute_quality_score(tokens)
    out = apply_heuristic_penalties(tokens, base)
    assert "all_common_default_colors" in out.penalties_applied
    assert "all_system_font_stack" not in out.penalties_applied
    assert out.penalized_score == pytest.approx(base.composite_score - COMMON_DEFAULT_COLORS_PENALTY)


def test_apply_penalties_clips_at_zero() -> None:
    """Penalty cannot drive the score below zero."""
    tokens = {
        "bg": "#ffffff",
        "text": "#000000",
        "accent": "#4f46e5",
        "font_body": "Arial, sans-serif",
        "font_display": "Georgia, serif",
    }
    base = compute_quality_score(tokens)
    out = apply_heuristic_penalties(tokens, base)
    assert out.penalized_score >= 0.0


def test_apply_penalties_no_trigger_on_empty_tokens() -> None:
    """No populated slots means neither penalty triggers; base scorer's
    every-dimension-zero result is the proper signal for that case."""
    base = compute_quality_score({})
    out = apply_heuristic_penalties({}, base)
    assert out.penalties_applied == ()


def test_apply_penalties_partial_brand_font_skips_penalty() -> None:
    """One brand font in the mix means the all-system trigger does not fire."""
    tokens = {
        "font_body": "Inter, sans-serif",
        "font_display": "Georgia, serif",  # system serif
    }
    base = compute_quality_score(tokens)
    out = apply_heuristic_penalties(tokens, base)
    assert "all_system_font_stack" not in out.penalties_applied


def test_apply_penalties_handles_none_tokens() -> None:
    """None for tokens is safe (matches `compute_quality_score`'s contract)."""
    base = compute_quality_score(None)
    out = apply_heuristic_penalties(None, base)
    assert out.penalties_applied == ()
    assert out.penalized_score == 0.0


def test_susann_extraction_now_falls_below_refund_threshold() -> None:
    """The flagship regression: the raw Susann extraction must score below
    `DEFAULT_THRESHOLD_V1_1_X` AFTER heuristic penalties are applied. Pre-
    heuristics, this same input could pass the threshold and never trigger
    the existing refund path; the finding doc records that pathology.
    """
    base = compute_quality_score(SUSANN_EXTRACTED_TOKENS)
    out = apply_heuristic_penalties(SUSANN_EXTRACTED_TOKENS, base)

    # Both penalties must fire on this input. The heuristic inspects bg,
    # text, and accent only (see `_COLOR_KEYS` rationale); all three of
    # Susann's extracted values (#f5f5f5, #1a1a1a, #4f46e5) are in the
    # default set, and all three populated font slots are in the system
    # stack set. The gray border/hairline/muted slots are not inspected
    # for color defaults because they are commonly defaults on any site.
    assert "all_system_font_stack" in out.penalties_applied, (
        f"font-stack penalty must fire on the Susann fixture; got {out.diagnostic}"
    )
    assert "all_common_default_colors" in out.penalties_applied, (
        f"color-defaults penalty must fire on the Susann fixture; got {out.diagnostic}"
    )

    assert out.penalized_score < DEFAULT_THRESHOLD_V1_1_X, (
        f"Susann extraction (raw) must score below the refund threshold "
        f"{DEFAULT_THRESHOLD_V1_1_X} after heuristics; "
        f"base={base.composite_score} penalized={out.penalized_score} "
        f"diagnostic={out.diagnostic}"
    )


# ----------------------------------------------------------------------
# R3 additions: missing-accent-diversity + display-equals-body rules.
# ----------------------------------------------------------------------


def test_delta_e_cie76_identical_colors_is_zero() -> None:
    """Sanity check on the LAB color-distance helper.

    Identical hex inputs produce zero distance; LAB conversion is stable.
    """
    assert _delta_e_cie76("#abcdef", "#abcdef") == pytest.approx(0.0, abs=1e-9)


def test_delta_e_cie76_known_distinct_colors_above_threshold() -> None:
    """Black vs white has a large LAB distance; threshold check is sane.

    L*=0 vs L*=100 is roughly Delta-E 100; this confirms the helper is
    not silently returning a tiny value (which would void the rule).
    """
    distance = _delta_e_cie76("#000000", "#ffffff")
    assert distance > PENALTY_ACCENT_TEXT_LAB_THRESHOLD * 5


def test_accent_diversity_rule_fires_on_near_monochrome_palette() -> None:
    """Accent and text within Delta-E 5 of each other triggers the rule.

    Two dark-near-grays (#1a1a1a and #1d1d1f) read as basically the same
    color; the rule catches the "extractor defaulted into a monochrome
    palette" failure mode adjacent to the Susann case.
    """
    tokens = {
        "bg": "#0B0B0F",
        "text": "#1a1a1a",
        "accent": "#1d1d1f",
        "font_body": "Inter, sans-serif",
        "font_display": "Anton, sans-serif",
    }
    fires, diag = _detect_missing_accent_diversity(tokens)
    assert fires
    assert "delta_e" in (diag or "")
    base = compute_quality_score(tokens)
    out = apply_heuristic_penalties(tokens, base)
    assert "missing_accent_diversity" in out.penalties_applied
    assert out.penalized_score == pytest.approx(base.composite_score - PENALTY_ACCENT_DIVERSITY)


def test_accent_diversity_rule_does_not_fire_on_distinctive_brand_accent() -> None:
    """Sun yellow vs bone cream are perceptually distinct; rule must not fire.

    Calibrated against Susann's actual brand: accent #FBE71F vs text
    #F5F2EA. Both are light, but yellow vs warm-cream is a wide LAB
    distance. If this case triggers the penalty, the threshold is wrong.
    """
    tokens = {
        "bg": "#0B0B0F",
        "text": "#F5F2EA",
        "accent": "#FBE71F",
    }
    fires, _ = _detect_missing_accent_diversity(tokens)
    assert not fires


def test_display_equals_body_rule_fires_on_duplicated_typeface() -> None:
    """display_font_family == body_font_family triggers the rule.

    Case-insensitive primary-family match; the rule does not care about
    fallback chain order. Single-typeface SYSTEMS are common (and the
    fixture set includes such cases), so this single penalty alone must
    not push a healthy design below the refund threshold.
    """
    # Realistic-healthy single-typeface design: Susann's headlights palette
    # plus the surrounding signal a healthy extraction surfaces (full palette
    # role coverage, a real type scale, a real spacing scale). The minimal
    # bg/text/accent-only form scores ~0.5 from the base scorer because four
    # of the six dimensions (type_scale, type_pairing, spacing_scale,
    # plus partial palette coverage) read as empty; that is not a "healthy"
    # extraction, it is a sparse one. The docstring's claim is about a
    # realistic-healthy design, so the fixture must carry realistic-healthy
    # signal across the dimensions the base scorer measures.
    tokens = {
        "bg": "#0B0B0F",
        "surface": "#14141A",
        "text": "#F5F2EA",
        "text_muted": "#A8A496",
        "accent": "#FBE71F",
        "border": "#3A372E",
        "font_body": "Inter, sans-serif",
        "font_display": "INTER, system-ui, sans-serif",
        "text_base": "1rem",
        "text_lg": "1.125rem",
        "text_xl": "1.25rem",
        "text_2xl": "1.5rem",
        "text_3xl": "1.875rem",
        "space_1": "0.25rem",
        "space_2": "0.5rem",
        "space_4": "1rem",
        "space_6": "1.5rem",
        "space_8": "2rem",
    }
    fires, diag = _detect_display_equals_body(tokens)
    assert fires
    assert "inter" in (diag or "").lower()
    base = compute_quality_score(tokens)
    out = apply_heuristic_penalties(tokens, base)
    assert "display_equals_body" in out.penalties_applied
    # Single-typeface design must not collapse below threshold from this
    # rule alone (Susann's distinctive palette saves the composite).
    assert out.penalized_score >= DEFAULT_THRESHOLD_V1_1_X


def test_display_equals_body_rule_does_not_fire_on_paired_typefaces() -> None:
    """A real display+body pair (Anton + Inter) leaves this rule silent."""
    tokens = {
        "font_body": "Inter, sans-serif",
        "font_display": "Anton, sans-serif",
    }
    fires, _ = _detect_display_equals_body(tokens)
    assert not fires


def test_susann_subset_with_only_default_colors_triggers_color_penalty() -> None:
    """Tighter Susann-like fixture: only the bg/text/accent/surface slots
    populated (all defaults), no non-default neutrals to dilute the check.
    Both penalties must fire and composite must collapse to near zero.
    """
    tight = {
        "bg": "#f5f5f5",
        "text": "#1a1a1a",
        "accent": "#4f46e5",
        "surface": "#ffffff",
        "font_body": "system-ui, -apple-system, sans-serif",
        "font_display": "Georgia, serif",
    }
    base = compute_quality_score(tight)
    out = apply_heuristic_penalties(tight, base)
    assert "all_common_default_colors" in out.penalties_applied
    assert "all_system_font_stack" in out.penalties_applied
    assert out.penalized_score < DEFAULT_THRESHOLD_V1_1_X
