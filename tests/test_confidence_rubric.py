"""Unit tests for the S20 confidence rubric (R3-downstream cycle #2).

Covers each scoring function with synthetic fixtures plus an integration
pass against two of the ground-truth observed payloads:

- ``encexplorer.json``: every declared color is a Gutenberg / grayscale
  default; composite must fall below the warn threshold and the flag list
  must call out the Gutenberg default match.
- ``stripe.json``: real brand palette + real font families; composite must
  clear the warn threshold and the flag list must be empty.

Schema: ``resemblio_confidence_rubric_v1@1.0``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from extractor.confidence_rubric import (
    DEFAULT_MAX_HITS,
    SCHEMA_VERSION,
    WARN_THRESHOLD,
    compute_confidence_rubric,
    count_generic_default_matches,
    score_font_specificity,
    score_generic_defaults,
    score_palette_diversity,
    score_screenshot_consistency,
)
from extractor.known_cms_defaults import normalize_font_stack, normalize_hex


GROUND_TRUTH_DIR = Path(__file__).parent / "fixtures" / "ground_truth" / "observed"


# ---------- known_cms_defaults helpers ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("#007CBA", "#007cba"),
        ("007cba", "#007cba"),
        ("#fff", "#ffffff"),
        ("FFF", "#ffffff"),
        ("  #ABC  ", "#aabbcc"),
        ("", None),
        (None, None),
        ("rgb(0,0,0)", None),
        ("#12345", None),
    ],
)
def test_normalize_hex(raw: str | None, expected: str | None) -> None:
    """Normalize hex strings to lowercase 6-digit form, reject non-hex input."""
    assert normalize_hex(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("system-ui", "system-ui"),
        ("'Dosis', sans-serif", "dosis, sans-serif"),
        ("  'Playfair Display' , serif ", "playfair display, serif"),
        ('"Sohne", sans-serif', "sohne, sans-serif"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_font_stack(raw: str | None, expected: str | None) -> None:
    """Strip quotes, lowercase, collapse spaces."""
    assert normalize_font_stack(raw) == expected


# ---------- score_palette_diversity ----------


def test_diversity_score_full_brand_palette() -> None:
    """4+ distinct non-grayscale, non-Gutenberg hues = 1.0."""
    score = score_palette_diversity(["#635bff", "#0a2540", "#0073e6", "#425466", "#031323"])
    assert score == 1.0


def test_diversity_score_pure_grayscale_plus_gutenberg() -> None:
    """Trivial grayscale + Gutenberg accent only = 0.0."""
    score = score_palette_diversity(["#ffffff", "#000000", "#f5f5f5", "#007cba", "#dddddd"])
    assert score == 0.0


def test_diversity_score_empty() -> None:
    """Empty hex list scores 0.0 (cannot judge diversity of nothing)."""
    assert score_palette_diversity([]) == 0.0


def test_diversity_score_partial() -> None:
    """3 distinct brand hues falls in the linear-interpolation band."""
    score = score_palette_diversity(["#aa1111", "#22bb22", "#3333cc"])
    assert 0.0 < score < 1.0


# ---------- count + score generic defaults ----------


def test_count_generic_default_matches_gutenberg() -> None:
    """Only Gutenberg accents count; trivial grayscale does not."""
    matches = count_generic_default_matches(["#007cba", "#006ba1", "#ffffff", "#000000"])
    assert matches == 2


def test_score_generic_defaults_zero_hits_is_one() -> None:
    """No CMS-default matches = perfect score."""
    assert score_generic_defaults(0) == 1.0


def test_score_generic_defaults_cap() -> None:
    """Floors at 0.0 once match count meets DEFAULT_MAX_HITS."""
    assert score_generic_defaults(DEFAULT_MAX_HITS) == 0.0
    assert score_generic_defaults(DEFAULT_MAX_HITS + 5) == 0.0


# ---------- score_font_specificity ----------


def test_font_specificity_all_real() -> None:
    """All real font families = 1.0."""
    score, generic_hits = score_font_specificity(["dosis, sans-serif", "playfair display, serif"])
    assert score == 1.0
    assert generic_hits == 0


def test_font_specificity_all_generic() -> None:
    """Every populated stack is generic = 0.0."""
    score, generic_hits = score_font_specificity(["system-ui", "sans-serif", "monospace"])
    assert score == 0.0
    assert generic_hits == 3


def test_font_specificity_mixed() -> None:
    """One generic out of two slots = 0.5."""
    score, generic_hits = score_font_specificity(["dosis, sans-serif", "monospace"])
    assert score == 0.5
    assert generic_hits == 1


def test_font_specificity_no_fonts() -> None:
    """No font slots populated returns 0.0 + 0 generic hits."""
    score, generic_hits = score_font_specificity([])
    assert score == 0.0
    assert generic_hits == 0


# ---------- score_screenshot_consistency ----------


def test_screenshot_consistency_none_is_neutral() -> None:
    """None (screenshot pass skipped) scores 1.0 with a flag raised separately."""
    assert score_screenshot_consistency(None) == 1.0


def test_screenshot_consistency_empty_warning() -> None:
    """Empty list (palette complete) scores 1.0."""
    assert score_screenshot_consistency([]) == 1.0


def test_screenshot_consistency_penalty_per_miss() -> None:
    """Each missing color subtracts a fixed penalty, floored at 0.0."""
    assert score_screenshot_consistency(["#abc123"]) == pytest.approx(0.80, rel=1e-3)
    assert score_screenshot_consistency(["#abc123"] * 10) == 0.0


# ---------- compute_confidence_rubric (synthetic) ----------


def test_rubric_synthetic_clean_brand() -> None:
    """A Stripe-like clean palette + real fonts + no screenshot miss = high confidence."""
    tokens = {
        "bg": "#ffffff",
        "text": "#0a2540",
        "accent": "#635bff",
        "accent_2": "#0073e6",
        "surface": "#f6f9fc",
        "border": "#e6ebf1",
        "text_muted": "#425466",
        "text_strong": "#031323",
        "font_body": "Sohne, sans-serif",
        "font_display": "Sohne, sans-serif",
        "font_mono": "Source Code Pro, monospace",
    }
    rubric = compute_confidence_rubric(tokens, palette_completeness_warning=[])
    assert rubric["composite_confidence"] > WARN_THRESHOLD
    assert rubric["generic_default_match_count"] == 0
    assert rubric["palette_diversity_score"] == 1.0
    assert rubric["schema_version"] == SCHEMA_VERSION
    assert "matches WP Gutenberg default accent" not in " ".join(rubric["flags"])


def test_rubric_synthetic_susann_pathology() -> None:
    """Susann case: generic fonts dominate + rendered palette diverges."""
    tokens = {
        "bg": "#ffffff",
        "text": "#1a1a1a",
        "accent": "#6366f1",
        "surface": "#f5f5f5",
        "font_body": "system-ui",
        "font_display": "Georgia, serif",
    }
    rubric = compute_confidence_rubric(
        tokens,
        palette_completeness_warning=["#1d1d1b", "#f3ecde", "#ffd84d"],
    )
    assert rubric["composite_confidence"] < WARN_THRESHOLD
    assert any("font slots are generic" in flag or "every populated font" in flag for flag in rubric["flags"])
    assert any("screenshot palette missed" in flag for flag in rubric["flags"])


def test_rubric_empty_tokens() -> None:
    """Degenerate empty input still returns a full rubric struct with all flags."""
    rubric = compute_confidence_rubric({}, palette_completeness_warning=None)
    assert rubric["composite_confidence"] < WARN_THRESHOLD
    assert "no color slots populated" in rubric["flags"]
    assert "no font slots populated" in rubric["flags"]
    assert "screenshot palette pass was not run" in rubric["flags"]


# ---------- ground-truth integration ----------


def _load_ground_truth(name: str) -> dict:
    """Load a ground-truth observed payload for integration testing."""
    path = GROUND_TRUTH_DIR / name
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def test_ground_truth_encexplorer_flags_gutenberg_default() -> None:
    """ENC Explorer ground-truth: composite must fall below warn threshold + flag Gutenberg."""
    payload = _load_ground_truth("encexplorer.json")
    rubric = compute_confidence_rubric(payload["tokens"], palette_completeness_warning=[])
    assert rubric["generic_default_match_count"] >= 1
    assert rubric["composite_confidence"] < WARN_THRESHOLD
    assert any("Gutenberg" in flag for flag in rubric["flags"])


def test_ground_truth_stripe_clears_threshold() -> None:
    """Stripe ground-truth: clean brand palette must clear the warn threshold."""
    payload = _load_ground_truth("stripe.json")
    rubric = compute_confidence_rubric(payload["tokens"], palette_completeness_warning=[])
    assert rubric["composite_confidence"] > WARN_THRESHOLD
    assert rubric["generic_default_match_count"] == 0
    # No "below warn threshold" flag should fire on a clean payload.
    assert not any("below warn threshold" in flag for flag in rubric["flags"])
