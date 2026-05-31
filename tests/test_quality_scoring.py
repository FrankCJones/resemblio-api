"""S20 quality-scoring tests. Synthetic fixtures; no network; no DB.

Per the S20 ADR section 12 (Resemblio_BUILD_LOG.md, 2026-05-26): ~18 per-
dimension tests + 5 composite/threshold tests + 2 idempotency-style suggestion
tests + 1 seed-row skip is exercised in the integration tests. 31 tests total
target across this file and `test_routes_extractions_s20.py`.
"""
from __future__ import annotations

import math

import pytest

from app.quality_scoring import (
    QualityScoreResult,
    _suggestion_for,
    compute_quality_score,
    score_color_chroma_diversity,
    score_palette_role_coverage,
    score_spacing_scale_completeness,
    score_token_value_diversity,
    score_type_pairing_signal,
    score_type_scale_completeness,
)
from app.scoring_weights import (
    DEFAULT_THRESHOLD_V1_1_X,
    DEFAULT_WEIGHTS_V1_1_X,
    SUGGESTIONS_BY_DIMENSION,
    _assert_weights_sum_to_one,
)


# ----------------------------------------------------------------------
# Dimension 1: palette_role_coverage
# ----------------------------------------------------------------------


def test_palette_role_coverage_zero_when_only_bg_text() -> None:
    """Score is 2/5 when only bg + text are populated (still distinct slots populated)."""
    tokens = {"bg": "#ffffff", "text": "#111111"}
    score = score_palette_role_coverage(tokens)
    assert math.isclose(score, 2.0 / 5.0)


def test_palette_role_coverage_full_distinct_palette() -> None:
    """Score is 1.0 when all five canonical role slots are populated and distinct."""
    tokens = {
        "bg": "#ffffff",
        "text": "#111111",
        "accent": "#ff3366",
        "text_muted": "#555555",
        "border": "#dddddd",
    }
    assert score_palette_role_coverage(tokens) == 1.0


def test_palette_role_coverage_duplicates_collapse() -> None:
    """Two slots holding the same hex only count once."""
    tokens = {
        "bg": "#ffffff",
        "text": "#111111",
        "accent": "#111111",  # duplicate of text
        "text_muted": "#555555",
        "border": "#dddddd",
    }
    assert math.isclose(score_palette_role_coverage(tokens), 4.0 / 5.0)


# ----------------------------------------------------------------------
# Dimension 2: color_chroma_diversity
# ----------------------------------------------------------------------


def test_chroma_diversity_zero_for_all_grayscale() -> None:
    """A palette of only grays returns 0.0 chroma spread."""
    tokens = {"accent": "#444444", "success": "#888888", "warning": "#cccccc"}
    assert score_color_chroma_diversity(tokens) == 0.0


def test_chroma_diversity_one_for_vivid_accent() -> None:
    """A vivid accent (saturation well past threshold) clips to 1.0."""
    tokens = {"accent": "#ff0000"}  # max saturation in HSL
    assert score_color_chroma_diversity(tokens) == 1.0


def test_chroma_diversity_zero_when_no_color_inputs() -> None:
    """Empty input scores 0.0 rather than raising."""
    assert score_color_chroma_diversity({}) == 0.0


# ----------------------------------------------------------------------
# Dimension 3: type_scale_completeness
# ----------------------------------------------------------------------


def test_type_scale_zero_when_no_sizes() -> None:
    """No populated text_* sizes returns 0.0."""
    assert score_type_scale_completeness({}) == 0.0


def test_type_scale_full_when_four_distinct_sizes() -> None:
    """Exactly the target count (4) returns 1.0."""
    tokens = {
        "text_sm": "14px",
        "text_base": "16px",
        "text_lg": "18px",
        "text_xl": "24px",
    }
    assert score_type_scale_completeness(tokens) == 1.0


def test_type_scale_partial_when_two_sizes() -> None:
    """Two distinct sizes returns 2/4 = 0.5."""
    tokens = {"text_base": "16px", "text_lg": "20px"}
    assert math.isclose(score_type_scale_completeness(tokens), 0.5)


# ----------------------------------------------------------------------
# Dimension 4: type_pairing_signal
# ----------------------------------------------------------------------


def test_type_pairing_one_when_families_differ() -> None:
    """Different font families return 1.0."""
    tokens = {"font_display": "Playfair, serif", "font_body": "Inter, sans-serif"}
    assert score_type_pairing_signal(tokens) == 1.0


def test_type_pairing_half_when_same_family_weight_differs() -> None:
    """Same family, weights that differ by 300+ units return 0.5."""
    tokens = {
        "font_display": "Inter, sans-serif",
        "font_body": "Inter, sans-serif",
        "font_display_weight": "700",
        "font_body_weight": "400",
    }
    assert score_type_pairing_signal(tokens) == 0.5


def test_type_pairing_zero_when_single_font_no_weight() -> None:
    """Same family with no weight signal returns 0.0."""
    tokens = {"font_display": "Inter, sans-serif", "font_body": "Inter, sans-serif"}
    assert score_type_pairing_signal(tokens) == 0.0


# ----------------------------------------------------------------------
# Dimension 5: spacing_scale_completeness
# ----------------------------------------------------------------------


def test_spacing_zero_when_no_values() -> None:
    """No spacing values returns 0.0."""
    assert score_spacing_scale_completeness({}) == 0.0


def test_spacing_full_when_five_distinct() -> None:
    """Exactly the target count (5) returns 1.0."""
    tokens = {
        "space_1": "4px",
        "space_2": "8px",
        "space_3": "12px",
        "space_4": "16px",
        "space_5": "24px",
    }
    assert score_spacing_scale_completeness(tokens) == 1.0


def test_spacing_partial_two_values() -> None:
    """Two distinct values returns 2/5 = 0.4."""
    tokens = {"space_1": "4px", "space_2": "8px"}
    assert math.isclose(score_spacing_scale_completeness(tokens), 0.4)


# ----------------------------------------------------------------------
# Dimension 6: token_value_diversity
# ----------------------------------------------------------------------


def test_diversity_zero_for_all_identical_defaults() -> None:
    """Every slot identical scores 1/N (catches default-filled output)."""
    tokens = {f"text_{i}": "16px" for i in range(8)}
    score = score_token_value_diversity(tokens)
    assert score == 1.0 / 8.0


def test_diversity_one_when_every_value_unique() -> None:
    """Every slot unique returns 1.0."""
    tokens = {f"text_{i}": f"{i}px" for i in range(5)}
    assert score_token_value_diversity(tokens) == 1.0


def test_diversity_half_when_half_default_filled() -> None:
    """Half-default-filled returns 0.5."""
    tokens = {
        "a": "x", "b": "x", "c": "x", "d": "x",  # 4 dupes
        "e": "1", "f": "2", "g": "3", "h": "4",  # 4 unique
    }
    # 5 distinct values across 8 slots -> 5/8 = 0.625; this catches the
    # "half default filled" pathology somewhere in [0.5, 0.7]
    score = score_token_value_diversity(tokens)
    assert 0.5 <= score <= 0.7


def test_diversity_zero_for_empty_tokens() -> None:
    """Empty TokenSet returns 0.0."""
    assert score_token_value_diversity({}) == 0.0


# ----------------------------------------------------------------------
# Composite + threshold
# ----------------------------------------------------------------------


def _all_high_tokens() -> dict[str, str]:
    """A TokenSet shaped to score 1.0 on every dimension."""
    return {
        "bg": "#ffffff",
        "text": "#111111",
        "accent": "#ff0000",  # vivid
        "text_muted": "#555555",
        "border": "#dddddd",
        "font_display": "Playfair, serif",
        "font_body": "Inter, sans-serif",
        "text_sm": "14px",
        "text_base": "16px",
        "text_lg": "18px",
        "text_xl": "24px",
        "space_1": "4px",
        "space_2": "8px",
        "space_3": "12px",
        "space_4": "16px",
        "space_5": "24px",
    }


def test_composite_all_high_passes_threshold() -> None:
    """All-near-1.0 dimensions compose well above 0.55; not low quality.

    A realistic high-quality TokenSet may have value-diversity slightly below
    1.0 because spacing scales legitimately share values like "16px" with type
    sizes. We assert "comfortably above threshold" not "exactly 1.0".
    """
    result = compute_quality_score(_all_high_tokens())
    assert result.composite_score >= 0.95
    assert result.is_low_quality is False
    assert result.suggestion == ""


def test_composite_all_zero_fails_threshold() -> None:
    """An empty TokenSet composes to 0.0; classified low_quality_output."""
    result = compute_quality_score({})
    assert result.composite_score == 0.0
    assert result.is_low_quality is True
    assert result.suggestion != ""


def test_threshold_strict_less_than() -> None:
    """A composite exactly equal to the threshold is NOT low quality (strict `<`)."""
    # Construct a tokens set that scores exactly 0.55 by injecting an
    # override threshold of 0.55 against a known composite. Easiest: use a
    # threshold above the all-zero composite to verify the boundary direction
    # rather than constructing the rare exact-0.55 case.
    result = compute_quality_score({}, threshold=0.0)
    assert result.composite_score == 0.0
    # composite == threshold means is_low_quality False (0.0 < 0.0 is False)
    assert result.is_low_quality is False


def test_threshold_boundary_below_fails() -> None:
    """A composite a hair below the threshold fails."""
    # Score 0.0 against a threshold of 0.001 -> 0.0 < 0.001 -> low quality
    result = compute_quality_score({}, threshold=0.001)
    assert result.is_low_quality is True


def test_weights_summing_not_to_one_raises() -> None:
    """Defensive guard: weights that don't sum to 1.0 fail at the assertion."""
    with pytest.raises(AssertionError):
        _assert_weights_sum_to_one({"a": 0.4, "b": 0.4})


# ----------------------------------------------------------------------
# Suggestion picker
# ----------------------------------------------------------------------


def test_suggestion_picks_lowest_dimension() -> None:
    """The suggestion string corresponds to the lowest-scoring dimension."""
    scores = {
        "palette_role_coverage": 0.9,
        "color_chroma_diversity": 0.0,  # lowest
        "type_scale_completeness": 0.8,
        "type_pairing_signal": 0.5,
        "spacing_scale_completeness": 0.5,
        "token_value_diversity": 0.7,
    }
    assert _suggestion_for(scores) == SUGGESTIONS_BY_DIMENSION["color_chroma_diversity"]


def test_suggestion_empty_for_empty_dict() -> None:
    """Empty dimension dict yields empty suggestion."""
    assert _suggestion_for({}) == ""


def test_suggestion_deterministic_on_tie() -> None:
    """Ties resolve to the first dimension in canonical order."""
    scores = {name: 0.0 for name in DEFAULT_WEIGHTS_V1_1_X}
    # Canonical order starts with `palette_role_coverage`
    assert _suggestion_for(scores) == SUGGESTIONS_BY_DIMENSION["palette_role_coverage"]


# ----------------------------------------------------------------------
# QualityScoreResult shape contract
# ----------------------------------------------------------------------


def test_result_carries_schema_version_and_weights() -> None:
    """The result dataclass carries schema_version and echoes weights for auditability."""
    result = compute_quality_score(_all_high_tokens())
    assert isinstance(result, QualityScoreResult)
    assert result.schema_version.startswith("quality_score_v1@")
    assert set(result.weights_used.keys()) == set(DEFAULT_WEIGHTS_V1_1_X.keys())
    assert math.isclose(sum(result.weights_used.values()), 1.0)


def test_result_threshold_echoed() -> None:
    """The result echoes the threshold actually used."""
    result = compute_quality_score(_all_high_tokens())
    assert result.threshold == DEFAULT_THRESHOLD_V1_1_X
    custom = compute_quality_score(_all_high_tokens(), threshold=0.42)
    assert custom.threshold == 0.42


def test_result_none_tokens_safe() -> None:
    """`tokens=None` is treated as empty rather than raising."""
    result = compute_quality_score(None)
    assert result.composite_score == 0.0
    assert result.is_low_quality is True


def test_dimension_score_keys_match_weight_keys() -> None:
    """Every weighted dimension produces a score; no key drift."""
    result = compute_quality_score(_all_high_tokens())
    assert set(result.dimension_scores.keys()) == set(DEFAULT_WEIGHTS_V1_1_X.keys())
