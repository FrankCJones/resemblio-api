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

from app.quality_heuristics import (
    COMMON_DEFAULT_COLORS_PENALTY,
    QUALITY_HEURISTICS_SCHEMA_VERSION,
    SYSTEM_FONT_STACK_PENALTY,
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
    """The schema version stays a simple semver-major string for cheap diffs."""
    assert QUALITY_HEURISTICS_SCHEMA_VERSION == "1.0"


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
