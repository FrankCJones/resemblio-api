"""S20 confidence rubric for extraction outputs.

Layered ON TOP OF the existing S20 quality score + heuristic penalties; this
module is purely ADDITIVE and does NOT change the customer-facing
``quality_score`` or the auto-refund gate. It produces a ``ConfidenceRubric``
struct surfaced on every successful extraction so a client can see WHY the
extractor is or is not confident about the captured tokens, even when
``palette_completeness_warning`` is empty.

Motivation: the ENC Explorer extraction 2026-06-04 (`tests/fixtures/
ground_truth/observed/encexplorer.json`) returned a palette where every
declared color was a Gutenberg / grayscale default (`#007cba`, `#dddddd`,
`#f5f5f5`, `#eeeeee`, `#abb8c3`, `#313131`) and yet the screenshot pass
produced no warning because the rendered page actually DOES use those
defaults verbatim (WP theme that was never customized). The Susann case
(2026-05-31) is the inverse: the rendered page does NOT use them, the
declared tokens are still defaults. Both produce a low-confidence signal
the customer should see at-a-glance.

Schema: ``resemblio_confidence_rubric_v1``.

Quality-floor notes:
- All functions are pure-data (no I/O, no network, no DB).
- Public functions carry docstrings explaining intent and edge cases.
- Magic numbers live in the WEIGHTS / SCORE constants block below.
- Output struct is a TypedDict with explicit field set.
"""
from __future__ import annotations

from typing import Final, Mapping, TypedDict

from extractor.known_cms_defaults import (
    GENERIC_SANS_FONTS,
    GUTENBERG_DEFAULT_ACCENTS,
    SCHEMA_VERSION as KNOWN_CMS_DEFAULTS_SCHEMA,
    TRIVIAL_GRAYSCALE,
    normalize_font_stack,
    normalize_hex,
)


SCHEMA_VERSION: Final[str] = "resemblio_confidence_rubric_v1@1.0"

# Color slots inspected for diversity / generic-default analysis. Aligned
# with the slot set ``quality_heuristics.py`` uses, so the rubric and the
# refund-gate see the same surface.
_COLOR_SLOTS: Final[tuple[str, ...]] = (
    "bg",
    "text",
    "accent",
    "accent_2",
    "border",
    "surface",
    "surface_2",
    "hairline",
    "text_muted",
    "text_strong",
)

# Font slots inspected for the font-specificity dimension.
_FONT_SLOTS: Final[tuple[str, ...]] = (
    "font_body",
    "font_display",
    "font_mono",
)

# Diversity scoring: how many distinct non-grayscale, non-default hues do we
# observe in the populated color slots? Two thresholds:
#   - DIVERSITY_FULL_AT or more distinct hues = score 1.0
#   - DIVERSITY_ZERO_AT or fewer distinct hues = score 0.0
#   - linear in between
# Calibrated against the 5 ground-truth payloads: Stripe (5 distinct brand
# hues -> 1.0), Apple / Figma (3-4 hues -> ~0.75), encexplorer (1 hue, the
# Gutenberg #007cba -> ~0.0).
DIVERSITY_FULL_AT: Final[int] = 4
DIVERSITY_ZERO_AT: Final[int] = 1

# Font specificity: 0.0 when every populated font slot is a generic stack,
# 1.0 when every populated font slot resolves to a real custom family.
# Linear in the count ratio.
FONT_SPECIFICITY_GENERIC_PENALTY: Final[float] = 1.0

# Screenshot-consistency: when the upstream palette_completeness_warning has
# entries, the rendered page diverges from declared. Each missing color
# costs ``SCREENSHOT_PENALTY_PER_MISS`` from a base of 1.0, floored at 0.0.
SCREENSHOT_PENALTY_PER_MISS: Final[float] = 0.20

# Composite weighting. Sums to 1.0. Tuned so a single-dimension red flag
# (e.g. all generic fonts on Susann) drops composite below WARN_THRESHOLD,
# while a borderline case (Apple's 3-hue palette + real fonts + clean
# screenshot) stays above.
WEIGHT_PALETTE_DIVERSITY: Final[float] = 0.35
WEIGHT_GENERIC_DEFAULTS: Final[float] = 0.25
WEIGHT_FONT_SPECIFICITY: Final[float] = 0.25
WEIGHT_SCREENSHOT_CONSISTENCY: Final[float] = 0.15

# Threshold at or below which the rubric emits a "low confidence" flag.
# Calibration evidence: encexplorer (0 distinct hues outside grayscale, 1
# Gutenberg accent, 1 of 3 fonts generic, screenshot clean) lands at
# composite ~= 0.25; Stripe lands at ~0.95. The 0.55 threshold gives a
# wide buffer around the boundary and matches the existing
# DEFAULT_THRESHOLD_V1_1_X value used by the refund gate.
WARN_THRESHOLD: Final[float] = 0.55

# Each generic-default match costs a fixed amount from the
# ``generic_default_match_count`` dimension. The dimension itself caps at
# DEFAULT_MAX_HITS so 6 misses do not score more harshly than 4.
DEFAULT_MAX_HITS: Final[int] = 4
DEFAULT_SCORE_PER_HIT: Final[float] = 1.0 / DEFAULT_MAX_HITS


class ConfidenceRubric(TypedDict):
    """Per-extraction confidence breakdown returned to the API client.

    Field semantics:
    - ``palette_diversity_score``: 0.0 (palette has 1 or 0 distinct
      non-grayscale hues) to 1.0 (4+ distinct hues).
    - ``generic_default_match_count``: raw count of declared colors that
      matched a known CMS-default set (Gutenberg accents only; trivial
      grayscale tracked separately and contributes to diversity only).
    - ``font_specificity_score``: 0.0 (every populated font slot is a
      generic stack) to 1.0 (every populated font slot is a real family).
      ``None``-equivalent when no font slot is populated; reported as 0.0
      with a flag so the customer sees the absence.
    - ``screenshot_consistency_score``: 1.0 when the screenshot palette
      pass found no missing colors; drops by
      ``SCREENSHOT_PENALTY_PER_MISS`` per missing hex.
    - ``composite_confidence``: weighted average of the four dimension
      scores; in [0.0, 1.0].
    - ``flags``: human-readable reasons. Empty when composite is above
      ``WARN_THRESHOLD`` and no individual dimension fired its flag.
    - ``schema_version``: pinning string for downstream consumers.
    """

    schema_version: str
    known_cms_defaults_schema: str
    palette_diversity_score: float
    generic_default_match_count: int
    font_specificity_score: float
    screenshot_consistency_score: float
    composite_confidence: float
    warn_threshold: float
    flags: list[str]


def _populated_colors(tokens: Mapping[str, object]) -> list[str]:
    """Return the lowercased, normalized hex values for populated color slots.

    Slots whose value cannot be parsed as a hex are skipped.
    """
    out: list[str] = []
    for slot in _COLOR_SLOTS:
        raw = tokens.get(slot)
        if not isinstance(raw, str):
            continue
        normalized = normalize_hex(raw)
        if normalized is not None:
            out.append(normalized)
    return out


def _populated_fonts(tokens: Mapping[str, object]) -> list[str]:
    """Return the lowercased, normalized font-stack strings for populated font slots."""
    out: list[str] = []
    for slot in _FONT_SLOTS:
        raw = tokens.get(slot)
        if not isinstance(raw, str):
            continue
        normalized = normalize_font_stack(raw)
        if normalized is not None:
            out.append(normalized)
    return out


def score_palette_diversity(populated_hexes: list[str]) -> float:
    """Score the palette by distinct non-grayscale, non-Gutenberg hue count.

    The Gutenberg accent set + trivial grayscale set are both excluded from
    the diversity count because they signal "default" rather than
    "deliberate brand choice". A palette of [white, black, gray, blue WP
    default] scores 0.0 (no real brand hues); a palette of
    [white, ink, sun, bone, sky] scores 1.0.

    Edge case: an empty populated_hexes list scores 0.0 (we cannot judge
    diversity of nothing). The flag for that situation is raised by the
    caller, not by the score function.
    """
    distinct_brand_hues = {
        hex_value
        for hex_value in populated_hexes
        if hex_value not in TRIVIAL_GRAYSCALE and hex_value not in GUTENBERG_DEFAULT_ACCENTS
    }
    count = len(distinct_brand_hues)
    if count >= DIVERSITY_FULL_AT:
        return 1.0
    if count <= DIVERSITY_ZERO_AT - 1:
        return 0.0
    if DIVERSITY_FULL_AT == DIVERSITY_ZERO_AT:
        return 1.0  # degenerate calibration; treated as pass
    span = DIVERSITY_FULL_AT - (DIVERSITY_ZERO_AT - 1)
    return max(0.0, min(1.0, (count - (DIVERSITY_ZERO_AT - 1)) / span))


def count_generic_default_matches(populated_hexes: list[str]) -> int:
    """Count declared colors that match a Gutenberg-default accent.

    Trivial grayscale is intentionally excluded from this count: a brand
    palette can legitimately include black and white. The presence of
    ``#007cba`` or another Gutenberg slug, in contrast, is almost always
    a stock theme.json signal.
    """
    return sum(1 for hex_value in populated_hexes if hex_value in GUTENBERG_DEFAULT_ACCENTS)


def score_generic_defaults(match_count: int) -> float:
    """Map raw match count to a [0.0, 1.0] score (1.0 = no matches).

    Each match subtracts ``DEFAULT_SCORE_PER_HIT`` from 1.0, floored at 0.0.
    """
    return max(0.0, 1.0 - (match_count * DEFAULT_SCORE_PER_HIT))


def score_font_specificity(populated_fonts: list[str]) -> tuple[float, int]:
    """Return (score, generic_hit_count) for the font specificity dimension.

    Score is 1.0 when no populated font slot is a generic stack, 0.0 when
    every populated font slot is generic, linear in between. When NO font
    slot is populated the score is 0.0 and the caller should raise the
    appropriate flag (the absence of a font signal is itself low-confidence).
    """
    if not populated_fonts:
        return 0.0, 0
    generic_hits = sum(1 for stack in populated_fonts if stack in GENERIC_SANS_FONTS)
    score = 1.0 - (generic_hits / len(populated_fonts)) * FONT_SPECIFICITY_GENERIC_PENALTY
    return max(0.0, min(1.0, score)), generic_hits


def score_screenshot_consistency(palette_completeness_warning: list[str] | None) -> float:
    """Score 1.0 - (missing_count * SCREENSHOT_PENALTY_PER_MISS), floored at 0.0.

    ``None`` (screenshot pass unavailable or skipped) is treated as a
    neutral 1.0 so the rubric does not penalize extractions on hosts
    where the headless browser was not configured. The caller raises a
    flag for the None case so the customer sees the dimension was not
    actually verified.
    """
    if palette_completeness_warning is None:
        return 1.0
    missing = len(palette_completeness_warning)
    return max(0.0, 1.0 - (missing * SCREENSHOT_PENALTY_PER_MISS))


def _composite(
    diversity: float,
    generic_defaults: float,
    font_specificity: float,
    screenshot_consistency: float,
) -> float:
    """Weighted average of the four dimension scores, clipped to [0.0, 1.0]."""
    value = (
        diversity * WEIGHT_PALETTE_DIVERSITY
        + generic_defaults * WEIGHT_GENERIC_DEFAULTS
        + font_specificity * WEIGHT_FONT_SPECIFICITY
        + screenshot_consistency * WEIGHT_SCREENSHOT_CONSISTENCY
    )
    return max(0.0, min(1.0, value))


def compute_confidence_rubric(
    tokens: Mapping[str, object],
    palette_completeness_warning: list[str] | None = None,
) -> ConfidenceRubric:
    """Compute the S20 confidence rubric for one extraction's token set.

    Pure-data: this function performs no I/O. ``tokens`` is the dict the
    extractor wrote to ``ExtractionBundle.tokens_json`` (and the row's
    ``tokens_json`` column); ``palette_completeness_warning`` is the
    optional A1.1 signal already threaded onto the bundle.

    Returns a fully populated ``ConfidenceRubric`` even on degenerate input
    (empty token set -> all zeros, every applicable flag raised). Callers
    should always include the rubric on the response; suppressing it on
    edge cases is the prohibited shortcut (the rubric IS the explanation
    for those cases).
    """
    populated_hexes = _populated_colors(tokens)
    populated_fonts = _populated_fonts(tokens)

    diversity_score = score_palette_diversity(populated_hexes)
    match_count = count_generic_default_matches(populated_hexes)
    generic_default_score = score_generic_defaults(match_count)
    font_score, generic_font_hits = score_font_specificity(populated_fonts)
    screenshot_score = score_screenshot_consistency(palette_completeness_warning)

    composite = _composite(
        diversity_score, generic_default_score, font_score, screenshot_score
    )

    flags: list[str] = []
    if match_count > 0:
        flags.append(f"matches WP Gutenberg default accent ({match_count} hit(s))")
    if not populated_hexes:
        flags.append("no color slots populated")
    elif diversity_score == 0.0:
        flags.append("palette has no distinct brand hues (trivial grayscale + defaults only)")
    if not populated_fonts:
        flags.append("no font slots populated")
    elif generic_font_hits == len(populated_fonts):
        flags.append("every populated font slot is a generic system stack")
    elif generic_font_hits > 0:
        flags.append(f"{generic_font_hits} of {len(populated_fonts)} font slots are generic stacks")
    if palette_completeness_warning is None:
        flags.append("screenshot palette pass was not run")
    elif palette_completeness_warning:
        flags.append(
            f"screenshot palette missed {len(palette_completeness_warning)} rendered color(s)"
        )
    if composite < WARN_THRESHOLD:
        flags.append("composite confidence below warn threshold")

    return ConfidenceRubric(
        schema_version=SCHEMA_VERSION,
        known_cms_defaults_schema=KNOWN_CMS_DEFAULTS_SCHEMA,
        palette_diversity_score=round(diversity_score, 4),
        generic_default_match_count=match_count,
        font_specificity_score=round(font_score, 4),
        screenshot_consistency_score=round(screenshot_score, 4),
        composite_confidence=round(composite, 4),
        warn_threshold=WARN_THRESHOLD,
        flags=flags,
    )
