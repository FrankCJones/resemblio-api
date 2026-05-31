"""S20 quality-scoring weights, threshold, and suggestion strings.

Isolated from `quality_scoring.py` so weight or threshold tuning is a one-line
edit without touching the scorer logic. Per the S20 ADR (BUILD_LOG line 1145+).

Provenance for the default weights and threshold: S20 ADR sections 2 + 3
(Resemblio_BUILD_LOG.md, 2026-05-26). The weights bias toward palette signals
(45 percent combined) because Frank's problem statement led with palette
pathologies. Threshold 0.55 is the ship-and-calibrate starting point; the
calibration plan (ADR section 3) retunes after 100 organic extractions plus
the DRL baseline.

Schema: `scoring_weights_v1`.
"""
from __future__ import annotations

from typing import Final


# Schema-version constant for any persisted result that names a weights set.
SCORING_WEIGHTS_SCHEMA_VERSION: Final[str] = "1.0"


# Per-dimension weights for the v1.1.x scorer. Sum MUST equal 1.0; checked at
# module import via the assertion below. If a future weight sweep adjusts these
# values, keep the sum invariant.
DEFAULT_WEIGHTS_V1_1_X: Final[dict[str, float]] = {
    "palette_role_coverage":      0.25,
    "color_chroma_diversity":     0.20,
    "type_scale_completeness":    0.15,
    "type_pairing_signal":        0.10,
    "spacing_scale_completeness": 0.15,
    "token_value_diversity":      0.15,
}


# Composite score strictly less than the threshold classifies the run as
# `low_quality_output`. Default 0.55 per ADR section 3.
DEFAULT_THRESHOLD_V1_1_X: Final[float] = 0.55


# Chroma threshold used by `score_color_chroma_diversity`. HSL saturation
# spread above this value scores 1.0; below it scales linearly to 0.0.
CHROMA_THRESHOLD: Final[float] = 0.25


# Type-scale completeness target: this many distinct text_* sizes scores 1.0.
TYPE_SCALE_TARGET: Final[int] = 4


# Spacing-scale completeness target: this many distinct space_* values scores 1.0.
SPACING_SCALE_TARGET: Final[int] = 5


# Heading weight contrast (numeric CSS weight units) that, by itself, signals
# a typographic pairing even when families match.
HEADING_WEIGHT_CONTRAST_UNITS: Final[int] = 300


# Suggestion strings keyed by the lowest-scoring dimension. Deterministic per
# the S20 ADR section 6; NOT LLM-generated; covered by tests so accidental
# edits are caught.
SUGGESTIONS_BY_DIMENSION: Final[dict[str, str]] = {
    "palette_role_coverage":
        "Palette is sparse; some canonical role slots (bg, text, accent, muted, border) are missing or duplicated. "
        "Try a page that surfaces the main marketing palette (homepage, pricing, hero) rather than a doc or legal page.",
    "color_chroma_diversity":
        "Palette appears grayscale; page may render styles via JS or use CSS custom properties. "
        "Try a sub-page that includes the main marketing palette.",
    "type_scale_completeness":
        "Type scale is sparse; only one or two distinct text sizes were found. "
        "This often means the extractor sampled a paywall, 404, or stripped feed; try the homepage or a content-rich page.",
    "type_pairing_signal":
        "No typographic pairing detected; heading and body styles appear identical. "
        "Try a page that surfaces both a hero heading and body copy.",
    "spacing_scale_completeness":
        "Spacing scale is sparse; the extractor saw few distinct padding or margin values. "
        "Try a page with richer layout structure (homepage, product page) rather than a single-column doc.",
    "token_value_diversity":
        "Many token slots share the same default value; the extraction may have fallen back to defaults. "
        "Try a different URL on the same site, ideally one with explicit brand styling.",
}


# Sum-invariant guard. Module import fails fast if weights are mis-edited.
def _assert_weights_sum_to_one(weights: dict[str, float], tolerance: float = 1e-6) -> None:
    """Raise AssertionError unless ``weights`` sum to 1.0 within ``tolerance``.

    Defensive check per ADR section 12: weights summing not equal to 1.0
    silently shifts the threshold meaning and is the kind of edit that needs
    to fail loud at import rather than at the first low-quality call.
    """
    total = sum(weights.values())
    assert abs(total - 1.0) < tolerance, (
        f"Quality-scoring weights must sum to 1.0; got {total!r} from {weights!r}"
    )


_assert_weights_sum_to_one(DEFAULT_WEIGHTS_V1_1_X)
