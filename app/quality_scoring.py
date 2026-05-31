"""S20 output-quality scoring for successful extractions.

Pure-data scorer that runs after the extractor returns success and before the
HTTP response is built. Six dimensions, composite-weight to a 0.0-1.0 score,
threshold-classified as `low_quality_output` if below ``DEFAULT_THRESHOLD_V1_1_X``.

Per the S20 ADR (Resemblio_BUILD_LOG.md, search "S20 ADR", 2026-05-26):

- Dimensions 1-6 ship in v1.1.x (this file).
- Dimensions 7 (`extraction_source_signal`) + 8 (`contrast_ratio`) defer to v1.2.
- All scorers are pure functions over the flat ``TokenSet`` dict the extractor
  produces and persists as ``Extraction.tokens_json``.
- No I/O; no network; no LLM calls. Estimated cost per call: < 10ms.

Refund contract: a low-quality classification ``low_quality_output`` is
Resemblio-attributable and refundable per ``app/failure_modes.py``
``REFUNDABLE_CODES``. The route handler wires the refund in
``app/routes/extractions.py``; the scorer itself never touches the ledger.

Schema: ``quality_score_v1``.
"""
from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.scoring_weights import (
    CHROMA_THRESHOLD,
    DEFAULT_THRESHOLD_V1_1_X,
    DEFAULT_WEIGHTS_V1_1_X,
    HEADING_WEIGHT_CONTRAST_UNITS,
    SCORING_WEIGHTS_SCHEMA_VERSION,
    SPACING_SCALE_TARGET,
    SUGGESTIONS_BY_DIMENSION,
    TYPE_SCALE_TARGET,
)


# Canonical role slots scored by `palette_role_coverage`. Slot is "populated"
# if the TokenSet carries a non-empty hex string at this key.
_PALETTE_ROLE_SLOTS: tuple[str, ...] = ("bg", "text", "accent", "text_muted", "border")


# Non-palette color slots used for chroma-diversity scoring. We exclude `bg`
# and `text` because the contrast pair is almost always near-monochrome and
# would mask vivid accent colors in the spread calculation.
_CHROMA_INPUT_SLOTS: tuple[str, ...] = (
    "accent",
    "accent_2",
    "success",
    "warning",
    "error",
    "info",
    "focus_ring",
)


# All `text_*` size keys per DRL `REQUIRED_TOKEN_KEYS` + the optional sizes.
# We count *populated* distinct values across the full superset rather than
# just required ones; sparser sites still register signal from the optional
# slots when they are present.
_TEXT_SIZE_KEYS: tuple[str, ...] = (
    "text_2xs", "text_xs", "text_sm", "text_base",
    "text_lg", "text_xl", "text_2xl", "text_3xl",
    "text_4xl", "text_5xl", "text_6xl", "text_7xl",
)


# All `space_*` keys. Same rationale as `_TEXT_SIZE_KEYS`.
_SPACING_KEYS: tuple[str, ...] = (
    "space_0", "space_1", "space_2", "space_3", "space_4", "space_5",
    "space_6", "space_8", "space_10", "space_12", "space_16", "space_32",
)


# Hex pattern used to validate that a color slot is populated with a real value
# rather than an empty string or default placeholder. Accepts 3- or 6-digit hex,
# with or without leading `#`.
_HEX_RE: re.Pattern[str] = re.compile(r"^#?[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")


@dataclass(frozen=True)
class QualityScoreResult:
    """Quality-scoring outcome for one extraction.

    Persisted on the ``extractions`` row as ``quality_score`` and
    ``quality_dimension_scores`` columns (migration 0008). Returned to the
    customer in the HTTP response body when ``is_low_quality`` is True.

    Fields:
        schema_version: ``quality_score_v1``; bump on incompatible shape changes.
        composite_score: Sum of (dimension_score * weight) across all dimensions.
        dimension_scores: Per-dimension float in [0.0, 1.0], keyed by dimension name.
        threshold: The threshold used at scoring time (echoed for auditability).
        is_low_quality: True iff ``composite_score < threshold``.
        suggestion: Actionable one-liner for the customer; empty when not low quality.
        weights_used: Echoed weights used at scoring time; lets a reviewer
            reproduce the composite arithmetic from row data alone.
    """

    schema_version: str
    composite_score: float
    dimension_scores: dict[str, float]
    threshold: float
    is_low_quality: bool
    suggestion: str
    weights_used: dict[str, float] = field(default_factory=dict)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _normalize_hex(value: Any) -> str | None:
    """Return the lowercase 6-digit hex string for a populated color slot.

    Returns None when the slot is missing, empty, or not a recognizable hex
    color. Edge case: 3-digit shorthand (`#fff`) is expanded to 6-digit
    (`#ffffff`) so equality checks across slots do not falsely flag distinct
    spellings of the same color as different.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if not _HEX_RE.match(text):
        return None
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    return f"#{text}"


def _hex_to_hsl(hex_color: str) -> tuple[float, float, float]:
    """Convert a 6-digit hex string to HLS (hue, lightness, saturation).

    Uses Python's ``colorsys.rgb_to_hls`` which returns hue, lightness,
    saturation in that order with each component in [0.0, 1.0]. We expose the
    HLS triple directly; chroma scoring reads the saturation component.
    """
    hex_clean = hex_color.lstrip("#")
    r = int(hex_clean[0:2], 16) / 255.0
    g = int(hex_clean[2:4], 16) / 255.0
    b = int(hex_clean[4:6], 16) / 255.0
    return colorsys.rgb_to_hls(r, g, b)


def _populated_values(tokens: Mapping[str, Any], keys: tuple[str, ...]) -> list[str]:
    """Return the trimmed non-empty string values for ``keys`` present in ``tokens``."""
    out: list[str] = []
    for key in keys:
        raw = tokens.get(key)
        if isinstance(raw, str) and raw.strip():
            out.append(raw.strip())
    return out


def _parse_weight(value: Any) -> int | None:
    """Parse a CSS font-weight value into an integer.

    Accepts numeric strings (`"400"`, `"700"`), CSS keywords (`"normal"`,
    `"bold"`), or bare ints. Returns None on anything unparseable so callers
    can decide whether to treat missing-weight as same-weight.
    """
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    keyword_map = {"normal": 400, "regular": 400, "bold": 700, "bolder": 800, "lighter": 300}
    if text in keyword_map:
        return keyword_map[text]
    try:
        return int(text)
    except ValueError:
        return None


def _font_family_signature(value: Any) -> str | None:
    """Return the first family name in a font-family stack, lowercased.

    A value like ``"Inter, sans-serif"`` returns ``"inter"``. We compare on
    the primary family because that is what the page actually requests; the
    fallback stack is page-author boilerplate and rarely changes between
    heading and body even on well-paired sites.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    first = text.split(",", 1)[0].strip().strip('"').strip("'").lower()
    return first or None


# ----------------------------------------------------------------------
# Dimension scorers (1-6)
# ----------------------------------------------------------------------


def score_palette_role_coverage(tokens: Mapping[str, Any]) -> float:
    """Score the populated-and-distinct count over the five canonical role slots.

    Slots: bg, text, accent, text_muted, border. A slot counts iff it carries
    a hex value AND that value is distinct from every other populated slot.
    Two slots holding the same hex (e.g. `accent == text`) reduces the count
    by one because the role separation has been lost.

    Returns: distinct_populated_count / 5.0, clipped to [0.0, 1.0].
    """
    seen: set[str] = set()
    populated_distinct = 0
    for slot in _PALETTE_ROLE_SLOTS:
        normalized = _normalize_hex(tokens.get(slot))
        if normalized is None:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        populated_distinct += 1
    return populated_distinct / float(len(_PALETTE_ROLE_SLOTS))


def score_color_chroma_diversity(tokens: Mapping[str, Any]) -> float:
    """Score saturation spread across non-bg/text color slots.

    Converts every populated non-bg/text color slot to HSL, takes the maximum
    saturation observed, and divides by ``CHROMA_THRESHOLD`` (clipped at 1.0).
    All-grayscale palettes score 0.0; even one vivid accent lifts the score.

    Edge case: when no qualifying slots are populated, returns 0.0 (we cannot
    detect chroma from an empty input; the palette_role_coverage dimension
    catches that pathology separately).
    """
    saturations: list[float] = []
    for slot in _CHROMA_INPUT_SLOTS:
        normalized = _normalize_hex(tokens.get(slot))
        if normalized is None:
            continue
        _hue, _lightness, saturation = _hex_to_hsl(normalized)
        saturations.append(saturation)
    if not saturations:
        return 0.0
    max_saturation = max(saturations)
    return min(max_saturation / CHROMA_THRESHOLD, 1.0)


def score_type_scale_completeness(tokens: Mapping[str, Any]) -> float:
    """Score the count of distinct text_* size values present.

    Returns: min(distinct_count / TYPE_SCALE_TARGET, 1.0).
    """
    values = _populated_values(tokens, _TEXT_SIZE_KEYS)
    distinct_count = len(set(values))
    return min(distinct_count / float(TYPE_SCALE_TARGET), 1.0)


def score_type_pairing_signal(tokens: Mapping[str, Any]) -> float:
    """Score the presence of a typographic pairing.

    - 1.0 if heading and body font families differ
    - 0.5 if same family but heading-weight is at least
      ``HEADING_WEIGHT_CONTRAST_UNITS`` heavier than body weight, OR if both
      families are populated and identical but at least one weight is parseable
      and they differ at all
    - 0.0 if neither family nor weight signal is detectable

    Uses ``font_display`` for heading and ``font_body`` for body, matching the
    DRL ``REQUIRED_TOKEN_KEYS`` slots. Falls back to 0.0 if either is missing.
    """
    display_family = _font_family_signature(tokens.get("font_display"))
    body_family = _font_family_signature(tokens.get("font_body"))
    if display_family is None or body_family is None:
        return 0.0
    if display_family != body_family:
        return 1.0
    display_weight = _parse_weight(tokens.get("font_display_weight"))
    body_weight = _parse_weight(tokens.get("font_body_weight"))
    if display_weight is not None and body_weight is not None:
        if abs(display_weight - body_weight) >= HEADING_WEIGHT_CONTRAST_UNITS:
            return 0.5
        if display_weight != body_weight:
            return 0.5
    return 0.0


def score_spacing_scale_completeness(tokens: Mapping[str, Any]) -> float:
    """Score the count of distinct space_* values present.

    Returns: min(distinct_count / SPACING_SCALE_TARGET, 1.0).
    """
    values = _populated_values(tokens, _SPACING_KEYS)
    distinct_count = len(set(values))
    return min(distinct_count / float(SPACING_SCALE_TARGET), 1.0)


def score_token_value_diversity(tokens: Mapping[str, Any]) -> float:
    """Score the ratio of distinct values to populated slots across all tokens.

    Catches the worst pathology: every slot filled with the same default
    (e.g. every color = `#000000`). A perfectly diverse extraction (every
    slot unique) scores 1.0; a maximally degenerate extraction (every slot
    identical) trends toward 1/N where N is the populated-slot count.

    Edge case: zero populated slots returns 0.0 to avoid divide-by-zero. An
    empty TokenSet is itself a quality failure; the score reflects that.
    """
    populated: list[str] = []
    for value in tokens.values():
        if isinstance(value, str) and value.strip():
            populated.append(value.strip())
    if not populated:
        return 0.0
    distinct = len(set(populated))
    return min(distinct / float(len(populated)), 1.0)


# Map dimension name to its scorer. Keyed in display order; the keys are also
# the keys used in ``QualityScoreResult.dimension_scores`` and in the response
# body's ``error_log.dimension_scores`` field.
_DIMENSION_SCORERS = {
    "palette_role_coverage":      score_palette_role_coverage,
    "color_chroma_diversity":     score_color_chroma_diversity,
    "type_scale_completeness":    score_type_scale_completeness,
    "type_pairing_signal":        score_type_pairing_signal,
    "spacing_scale_completeness": score_spacing_scale_completeness,
    "token_value_diversity":      score_token_value_diversity,
}


def _suggestion_for(dimension_scores: Mapping[str, float]) -> str:
    """Return the suggestion string for the lowest-scoring dimension.

    Deterministic per ADR section 6. When multiple dimensions tie for lowest,
    the first one (in the canonical scorer order) wins; this keeps the
    suggestion stable across re-runs of the same input.
    """
    if not dimension_scores:
        return ""
    min_score = min(dimension_scores.values())
    for name in _DIMENSION_SCORERS:
        if name in dimension_scores and dimension_scores[name] == min_score:
            return SUGGESTIONS_BY_DIMENSION.get(name, "")
    return ""


def compute_quality_score(
    tokens: Mapping[str, Any] | None,
    *,
    weights: Mapping[str, float] | None = None,
    threshold: float | None = None,
) -> QualityScoreResult:
    """Score an extraction's tokens across the six v1.1.x dimensions.

    Args:
        tokens: Flat ``TokenSet`` dict the extractor produces. None or empty
            scores as low quality across the board.
        weights: Override weights; defaults to ``DEFAULT_WEIGHTS_V1_1_X``.
            Must include exactly the six dimension keys and sum to 1.0.
        threshold: Override threshold; defaults to ``DEFAULT_THRESHOLD_V1_1_X``.

    Returns:
        ``QualityScoreResult`` with per-dimension scores, composite, and the
        suggestion string for the lowest-scoring dimension.

    Edge case: ``tokens=None`` returns an all-zero result classified as
    low quality. The caller should never invoke the scorer when the extractor
    has already classified the run as failed, but the defensive zero-result
    means a bug upstream cannot produce a malformed score row.
    """
    effective_weights = dict(weights) if weights is not None else dict(DEFAULT_WEIGHTS_V1_1_X)
    effective_threshold = threshold if threshold is not None else DEFAULT_THRESHOLD_V1_1_X
    safe_tokens: Mapping[str, Any] = tokens if isinstance(tokens, Mapping) else {}

    dimension_scores: dict[str, float] = {}
    for name, scorer in _DIMENSION_SCORERS.items():
        dimension_scores[name] = float(scorer(safe_tokens))

    composite = 0.0
    for name, score in dimension_scores.items():
        weight = effective_weights.get(name, 0.0)
        composite += score * weight

    is_low_quality = composite < effective_threshold
    suggestion = _suggestion_for(dimension_scores) if is_low_quality else ""

    return QualityScoreResult(
        schema_version=f"quality_score_v1@{SCORING_WEIGHTS_SCHEMA_VERSION}",
        composite_score=round(composite, 6),
        dimension_scores=dimension_scores,
        threshold=effective_threshold,
        is_low_quality=is_low_quality,
        suggestion=suggestion,
        weights_used=effective_weights,
    )
