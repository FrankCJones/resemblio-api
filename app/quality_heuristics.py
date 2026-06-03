"""S20 quality-score penalty heuristics for the "extractor returned defaults" pathology.

The base S20 composite scorer (`quality_scoring.py`) measures token *richness*:
how many slots are populated, how diverse the colors are, whether type pairs
are present. It cannot tell that a richly populated palette is in fact a
generic light-mode placeholder (`#ffffff` + `#f5f5f5` + `#1a1a1a` + indigo)
or that the fonts are a 100 percent system stack. The Susann finding
(`projects/Resemblio/02-prd/2026-05-31-extraction-fidelity-finding-susann.md`)
caught this directly: the auto-output scored as healthy on a result that
captured none of the actual brand identity.

This module adds DEFAULT-DETECTION penalties applied AFTER the base composite:

- Penalty A: if every detected font primary family is in the system stack
  set (`_SYSTEM_FONT_FAMILIES`), subtract `SYSTEM_FONT_STACK_PENALTY`.
- Penalty B: if every detected color value is in the common-default set
  (`_COMMON_DEFAULT_COLORS`), subtract `COMMON_DEFAULT_COLORS_PENALTY`.

The penalties stack. Composite is clipped to [0.0, 1.0] after. The route
handler is expected to call `apply_heuristic_penalties` AFTER
`compute_quality_score` and BEFORE the low-quality classification check, so
penalized extractions correctly fall below `DEFAULT_THRESHOLD_V1_1_X` and
trigger the existing refund path.

Provenance: extraction-fidelity finding 2026-05-31. Default-color and
default-font sets are taken VERBATIM from the finding's "Hypotheses" section
2 + 3 plus the extractor's own observed output on the Susann run.

Schema: `quality_heuristics_v1`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.constants import (
    DEFAULT_COLOR_DISTANCE_MAX,
    DEFAULT_COLOR_SCORE_THRESHOLD,
    NEAR_DEFAULT_EXTRACTION_FAILURE_MODE,
    NEAR_DEFAULT_EXTRACTION_FLAG,
    PENALTY_ACCENT_DIVERSITY,
    PENALTY_ACCENT_TEXT_LAB_THRESHOLD,
    PENALTY_DISPLAY_EQUALS_BODY,
    SYSTEM_STACK_SCORE_THRESHOLD,
)
from app.quality_scoring import QualityScoreResult, _normalize_hex


# Schema-version constant for any persisted heuristic result. Bumped to 1.1
# 2026-06-02 to mark the R3 additions (accent-diversity + display-equals-
# body penalties). Additive change: existing penalty names + score shape
# are unchanged; new entries can appear in ``penalties_applied``.
QUALITY_HEURISTICS_SCHEMA_VERSION: str = "1.2"


# Common-default font primary-family set. All values are lowercase, single-
# word, no quotes; matched against the first family name in a CSS stack.
#
# Provenance: extraction-fidelity-finding 2026-05-31 ("font_body":
# "system-ui, -apple-system, ...", "font_display": "Georgia, 'Times New
# Roman', serif"). These are the families a frequency-weighted extractor
# returns when it misses the @font-face / Google Fonts link in <head>.
_SYSTEM_FONT_FAMILIES: frozenset[str] = frozenset({
    "system-ui",
    "-apple-system",
    "blinkmacsystemfont",
    "segoe ui",
    "roboto",
    "helvetica",
    "helvetica neue",
    "arial",
    "georgia",
    "times",
    "times new roman",
    "courier",
    "courier new",
    "monaco",
    "consolas",
    "sans-serif",
    "serif",
    "monospace",
})


# Common-default color set. Hex strings normalized to lowercase 6-digit.
#
# Provenance: extraction-fidelity-finding 2026-05-31 ("bg": "#f5f5f5",
# "text": "#1a1a1a", "accent": "#4f46e5", "surface": "#ffffff"). #000000
# added because a black-text default is the other half of the same pathology.
# Tailwind indigo-600 (#4f46e5) is included because that color is the
# observed LLM-default-accent that prompted the finding.
_COMMON_DEFAULT_COLORS: frozenset[str] = frozenset({
    "#ffffff",
    "#f5f5f5",
    "#1a1a1a",
    "#000000",
    "#4f46e5",
})


# Penalty magnitudes. Each independently knocks the composite below the
# DEFAULT_THRESHOLD_V1_1_X (0.55) when applied to a base score in the
# 0.70-0.85 range, which is where the Susann extraction would otherwise sit.
SYSTEM_FONT_STACK_PENALTY: float = 0.30
COMMON_DEFAULT_COLORS_PENALTY: float = 0.30


# Token keys the heuristic inspects for fonts. Subset of the flat TokenSet
# keys defined in `extractor/drl_adapter.py`.
_FONT_KEYS: tuple[str, ...] = ("font_body", "font_display", "font_mono")


# Color slots the heuristic inspects. We restrict to the THREE role-defining
# slots (`bg`, `text`, `accent`) because they carry the brand signal. Gray
# neutrals like `border`/`hairline`/`text_muted` are often legitimately
# defaults on any well-built site (`#d1d5db`, `#e5e7eb`, `#6b7280` are
# standard Tailwind grays), so including them in the all-defaults check
# would mask the "we missed the brand color" pathology. Restricting to the
# brand-signal slots keeps the heuristic targeted on the failure mode the
# extraction-fidelity-finding 2026-05-31 documented.
_COLOR_KEYS: tuple[str, ...] = (
    "bg",
    "text",
    "accent",
)


# Color slots inspected by the R3.2 near-default-extraction score. Includes
# `surface` in addition to the brand-signal trio because a pure-white
# `surface` plus near-default bg/text/accent is the exact Susann signature.
_NEAR_DEFAULT_COLOR_KEYS: tuple[str, ...] = (
    "bg",
    "text",
    "accent",
    "surface",
)


# Common gray-scale and framework-default reference colors used by the
# R3.2 near-default-extraction score. Each populated color slot is compared
# to every reference; if any Manhattan-RGB distance is under
# DEFAULT_COLOR_DISTANCE_MAX, the slot counts as "near-default".
_NEAR_DEFAULT_REFERENCE_HEXES: tuple[str, ...] = (
    "#000000",  # pure black
    "#ffffff",  # pure white
    "#888888",  # mid gray
    "#f5f5f5",  # near-white (LLM bg default)
    "#1a1a1a",  # near-black (LLM text default)
    "#4f46e5",  # Tailwind indigo (LLM accent default)
)


@dataclass(frozen=True)
class HeuristicPenaltyResult:
    """Outcome of applying default-detection penalties to a composite score.

    Attributes:
        schema_version: ``quality_heuristics_v1``.
        original_score: The composite score before penalties.
        penalized_score: The composite score after penalties, clipped to [0,1].
        penalties_applied: Names of every triggered penalty in canonical order.
        diagnostic: Short human-readable summary of what triggered.
    """

    schema_version: str
    original_score: float
    penalized_score: float
    penalties_applied: tuple[str, ...]
    diagnostic: str


def _primary_family_lower(stack: Any) -> str | None:
    """Return the lowercased primary family of a CSS font-family stack.

    Returns None for unparseable or empty input. Edge case: stack values
    sometimes carry quoted families (`"Inter"`); we strip both single and
    double quotes from the primary token only.
    """
    if not isinstance(stack, str):
        return None
    text = stack.strip()
    if not text:
        return None
    first = text.split(",", 1)[0].strip().strip('"').strip("'").lower()
    return first or None


def _detect_all_system_fonts(tokens: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Return (all_system, observed_families) for the inspected font slots.

    `all_system` is True iff at least one font slot was populated AND every
    populated font slot's primary family is in `_SYSTEM_FONT_FAMILIES`. If
    no font slot is populated, we DO NOT trigger the penalty (the extractor
    simply did not surface any font signal; that is a separate failure
    captured by the base scorer's `type_pairing_signal` dimension).
    """
    observed: list[str] = []
    for key in _FONT_KEYS:
        family = _primary_family_lower(tokens.get(key))
        if family is None:
            continue
        observed.append(family)
    if not observed:
        return False, observed
    all_system = all(family in _SYSTEM_FONT_FAMILIES for family in observed)
    return all_system, observed


def _detect_all_default_colors(tokens: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Return (all_default, observed_hexes) for the inspected color slots.

    `all_default` is True iff at least one color slot was populated AND every
    populated color slot's normalized hex is in `_COMMON_DEFAULT_COLORS`. As
    with fonts, an empty palette does not trigger this penalty (caught by
    `palette_role_coverage` in the base scorer).
    """
    observed: list[str] = []
    for key in _COLOR_KEYS:
        normalized = _normalize_hex(tokens.get(key))
        if normalized is None:
            continue
        observed.append(normalized)
    if not observed:
        return False, observed
    all_default = all(hex_value in _COMMON_DEFAULT_COLORS for hex_value in observed)
    return all_default, observed


# ----------------------------------------------------------------------
# R3 additions (2026-06-02): accent-diversity + display-equals-body rules.
# Source mission: `projects/OptSus Team/missions/resemblio-r3-extraction-
# fidelity-v1.md` Deliverable C. Each rule is a pure function with its
# own docstring naming the SOURCE finding hypothesis it covers.
# ----------------------------------------------------------------------


def _srgb_to_linear(channel: float) -> float:
    """Convert one 0-1 sRGB component to its linear-light equivalent.

    Standard sRGB inverse companding curve. Edge case: input is clipped to
    [0, 1] before applying the curve so out-of-gamut inputs do not produce
    NaNs from the power function.
    """
    if channel < 0.0:
        channel = 0.0
    elif channel > 1.0:
        channel = 1.0
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _hex_to_lab(hex_value: str) -> tuple[float, float, float]:
    """Convert a 6-digit hex color to CIE LAB (D65 white point).

    sRGB -> linear-light RGB -> XYZ -> LAB. Standard formula; reference
    constants are the D65 illuminant whites. Returns (L*, a*, b*).
    """
    text = hex_value[1:] if hex_value.startswith("#") else hex_value
    r = int(text[0:2], 16) / 255.0
    g = int(text[2:4], 16) / 255.0
    b = int(text[4:6], 16) / 255.0
    rl = _srgb_to_linear(r)
    gl = _srgb_to_linear(g)
    bl = _srgb_to_linear(b)
    # sRGB -> XYZ (D65) via the standard matrix.
    x = rl * 0.4124564 + gl * 0.3575761 + bl * 0.1804375
    y = rl * 0.2126729 + gl * 0.7151522 + bl * 0.0721750
    z = rl * 0.0193339 + gl * 0.1191920 + bl * 0.9503041
    # Normalize against D65 white.
    xn = x / 0.95047
    yn = y / 1.00000
    zn = z / 1.08883

    def _f(t: float) -> float:
        if t > (6 / 29) ** 3:
            return t ** (1 / 3)
        return t / (3 * (6 / 29) ** 2) + (4 / 29)

    fx = _f(xn)
    fy = _f(yn)
    fz = _f(zn)
    L = 116 * fy - 16
    a = 500 * (fx - fy)
    b_lab = 200 * (fy - fz)
    return L, a, b_lab


def _delta_e_cie76(hex_a: str, hex_b: str) -> float:
    """Return the CIE76 Delta-E distance between two normalized hex colors.

    CIE76 (the original 1976 formula) is the cheap-and-good-enough metric
    for "are these two colors perceptually similar?". A Delta-E under ~2.3
    is the just-noticeable-difference threshold; under ~5 is "the human
    eye reads them as related"; under ~10 is "same hue, different shade."
    The accent-diversity penalty threshold is `PENALTY_ACCENT_TEXT_LAB_THRESHOLD`.
    """
    la, aa, ba = _hex_to_lab(hex_a)
    lb, ab, bb = _hex_to_lab(hex_b)
    return ((la - lb) ** 2 + (aa - ab) ** 2 + (ba - bb) ** 2) ** 0.5


def _detect_missing_accent_diversity(
    tokens: Mapping[str, Any],
) -> tuple[bool, str | None]:
    """Return (penalty_fires, diagnostic) for the accent-vs-text diversity rule.

    Penalty fires iff both `accent` AND `text` slots are populated with
    valid hex AND their CIE76 Delta-E distance is below
    `PENALTY_ACCENT_TEXT_LAB_THRESHOLD`. Covers the failure mode where the
    extractor emits a near-monochrome palette (accent and text are both
    dark grays) because it could not resolve a brand color signal. Source
    finding 2026-05-31 Hypothesis 2 (frequency-weighting can wash out a
    single-CTA accent into the dominant text gray).
    """
    accent = _normalize_hex(tokens.get("accent"))
    text = _normalize_hex(tokens.get("text"))
    if accent is None or text is None:
        return False, None
    distance = _delta_e_cie76(accent, text)
    if distance >= PENALTY_ACCENT_TEXT_LAB_THRESHOLD:
        return False, None
    return True, (
        f"accent={accent} text={text} delta_e={distance:.2f} < "
        f"threshold={PENALTY_ACCENT_TEXT_LAB_THRESHOLD}"
    )


def _detect_display_equals_body(
    tokens: Mapping[str, Any],
) -> tuple[bool, str | None]:
    """Return (penalty_fires, diagnostic) for the display==body font rule.

    Penalty fires iff both `font_display` AND `font_body` are populated AND
    their primary family names are equal (case-insensitive). Real design
    systems pair a display face with a distinct body face; when an
    extractor cannot resolve the type pairing it sometimes copies one slot
    into the other. Source finding 2026-05-31 Hypothesis 3 (Google Fonts
    link tags in `<head>` not parsed; type-pairing signal collapses).
    """
    display = _primary_family_lower(tokens.get("font_display"))
    body = _primary_family_lower(tokens.get("font_body"))
    if display is None or body is None:
        return False, None
    if display != body:
        return False, None
    return True, f"font_display={display!r} == font_body={body!r}"


# ----------------------------------------------------------------------
# R3.2 additions (2026-06-02): near-default-extraction failure rule.
# Source dispatch: `projects/Resemblio/_handoff/inbox/claude/
# 2026-06-02-susann-extraction-fidelity-investigation.md`. This is the
# fail-loud rule that drives `quality_score` to 0.0 when BOTH the system-
# stack-font score and the default-color score exceed
# SYSTEM_STACK_SCORE_THRESHOLD / DEFAULT_COLOR_SCORE_THRESHOLD. Pre-R3.2
# the two individual penalties (each -0.30) summed to -0.60 which could
# still leave a base 0.85 above the 0.55 refund threshold on a degenerate
# scorer state; this rule provides a deterministic floor.
# ----------------------------------------------------------------------


def _system_stack_score(tokens: Mapping[str, Any]) -> tuple[float, list[str]]:
    """Return (fraction-in-system-stack, observed_primary_families).

    The fraction is `populated_system_fonts / populated_font_slots`. When
    no font slot is populated the fraction is 0.0 (the rule should not
    fire on an empty payload; the base scorer flags that case separately).
    """
    observed: list[str] = []
    system_hits = 0
    for key in _FONT_KEYS:
        family = _primary_family_lower(tokens.get(key))
        if family is None:
            continue
        observed.append(family)
        if family in _SYSTEM_FONT_FAMILIES:
            system_hits += 1
    if not observed:
        return 0.0, observed
    return system_hits / len(observed), observed


def _manhattan_rgb_distance(hex_a: str, hex_b: str) -> int:
    """Return the sum of absolute per-channel differences between two hexes.

    Inputs must be 7-character `#rrggbb` strings already normalized by
    `_normalize_hex`. Range: 0 (identical) to 765 (#000 vs #fff). Cheap
    metric; perceptual fidelity is not required for the gray-scale-
    neighborhood test the R3.2 rule cares about.
    """
    ar = int(hex_a[1:3], 16)
    ag = int(hex_a[3:5], 16)
    ab = int(hex_a[5:7], 16)
    br = int(hex_b[1:3], 16)
    bg = int(hex_b[3:5], 16)
    bb = int(hex_b[5:7], 16)
    return abs(ar - br) + abs(ag - bg) + abs(ab - bb)


def _is_near_default_color(hex_value: str) -> bool:
    """Return True if `hex_value` is within DEFAULT_COLOR_DISTANCE_MAX of any reference.

    References are the LLM-default palette (`#000`, `#fff`, `#888`, plus
    the observed-Susann-defaults `#f5f5f5` / `#1a1a1a` / `#4f46e5`). A
    distinctive brand color (sun yellow `#FBE71F`, ink `#0B0B0F` near
    pure-black) is checked against EVERY reference; `#0B0B0F` is Manhattan
    33 from `#000000` so it WILL count as near-default, which is correct
    behavior - a near-black brand background still passes the
    "did the extractor capture distinctive identity?" test only via the
    DISTINCTIVE accent slot, which the score requires across the slot set.
    """
    for ref in _NEAR_DEFAULT_REFERENCE_HEXES:
        if _manhattan_rgb_distance(hex_value, ref) < DEFAULT_COLOR_DISTANCE_MAX:
            return True
    return False


def _default_color_score(tokens: Mapping[str, Any]) -> tuple[float, list[str]]:
    """Return (fraction-near-default, observed_normalized_hexes).

    The fraction is `populated_near_default_colors / populated_color_slots`.
    When no color slot is populated the fraction is 0.0.
    """
    observed: list[str] = []
    near_default_hits = 0
    for key in _NEAR_DEFAULT_COLOR_KEYS:
        normalized = _normalize_hex(tokens.get(key))
        if normalized is None:
            continue
        observed.append(normalized)
        if _is_near_default_color(normalized):
            near_default_hits += 1
    if not observed:
        return 0.0, observed
    return near_default_hits / len(observed), observed


def _detect_near_default_extraction(
    tokens: Mapping[str, Any],
) -> tuple[bool, str | None]:
    """Return (penalty_fires, diagnostic) for the R3.2 fail-loud rule.

    Penalty fires iff BOTH the system-stack font score AND the default-color
    score equal or exceed their respective thresholds. The two-score-AND
    structure is deliberate: a brand with all-system fonts is plausible
    (some sites genuinely choose Helvetica), and a brand with all-default
    colors is plausible (a minimalist black-on-white site), but the
    CONJUNCTION is the smoking gun for an extractor that resolved nothing.
    """
    font_score, observed_fonts = _system_stack_score(tokens)
    color_score, observed_colors = _default_color_score(tokens)
    if font_score < SYSTEM_STACK_SCORE_THRESHOLD:
        return False, None
    if color_score < DEFAULT_COLOR_SCORE_THRESHOLD:
        return False, None
    return True, (
        f"system_stack_score={font_score:.2f} (fonts={observed_fonts!r}); "
        f"default_color_score={color_score:.2f} (colors={observed_colors!r}); "
        f"failure_mode={NEAR_DEFAULT_EXTRACTION_FAILURE_MODE}"
    )


def apply_heuristic_penalties(
    tokens: Mapping[str, Any] | None,
    base_result: QualityScoreResult,
) -> HeuristicPenaltyResult:
    """Apply default-detection penalties to a base quality-score result.

    Args:
        tokens: Flat TokenSet dict the extractor produced. None or empty
            triggers neither penalty (the base scorer already flags the
            empty case via every-dimension-zero).
        base_result: The `QualityScoreResult` from `compute_quality_score`.

    Returns:
        `HeuristicPenaltyResult` with the penalized score clipped to [0, 1].
        The caller is responsible for persisting the penalized score and
        re-checking against the low-quality threshold; this helper does not
        touch the database or the refund ledger.
    """
    safe_tokens: Mapping[str, Any] = tokens if isinstance(tokens, Mapping) else {}
    original = float(base_result.composite_score)
    penalized = original
    applied: list[str] = []
    diagnostic_parts: list[str] = []

    all_system_fonts, observed_fonts = _detect_all_system_fonts(safe_tokens)
    if all_system_fonts:
        penalized -= SYSTEM_FONT_STACK_PENALTY
        applied.append("all_system_font_stack")
        diagnostic_parts.append(
            f"all_system_font_stack(-{SYSTEM_FONT_STACK_PENALTY}): "
            f"primary_families={observed_fonts!r}"
        )

    all_defaults, observed_colors = _detect_all_default_colors(safe_tokens)
    if all_defaults:
        penalized -= COMMON_DEFAULT_COLORS_PENALTY
        applied.append("all_common_default_colors")
        diagnostic_parts.append(
            f"all_common_default_colors(-{COMMON_DEFAULT_COLORS_PENALTY}): "
            f"colors={observed_colors!r}"
        )

    accent_collapse, accent_diag = _detect_missing_accent_diversity(safe_tokens)
    if accent_collapse:
        penalized -= PENALTY_ACCENT_DIVERSITY
        applied.append("missing_accent_diversity")
        diagnostic_parts.append(
            f"missing_accent_diversity(-{PENALTY_ACCENT_DIVERSITY}): {accent_diag}"
        )

    type_collapse, type_diag = _detect_display_equals_body(safe_tokens)
    if type_collapse:
        penalized -= PENALTY_DISPLAY_EQUALS_BODY
        applied.append("display_equals_body")
        diagnostic_parts.append(
            f"display_equals_body(-{PENALTY_DISPLAY_EQUALS_BODY}): {type_diag}"
        )

    # R3.2 fail-loud rule. Runs LAST so the individual diagnostics still
    # appear in the trail; when this fires we hard-floor the score to 0.0
    # regardless of the per-rule arithmetic above so downstream consumers
    # always see the worst-case signal.
    near_default, near_default_diag = _detect_near_default_extraction(safe_tokens)
    if near_default:
        applied.append(NEAR_DEFAULT_EXTRACTION_FLAG)
        diagnostic_parts.append(
            f"{NEAR_DEFAULT_EXTRACTION_FLAG}(floor=0.0): {near_default_diag}"
        )
        penalized = 0.0

    # Clip to valid score range. A composite below 0 carries no extra
    # information for downstream consumers and would break any sort that
    # treats `quality_score` as a 0-to-1 confidence metric.
    if penalized < 0.0:
        penalized = 0.0
    elif penalized > 1.0:
        penalized = 1.0

    return HeuristicPenaltyResult(
        schema_version=f"quality_heuristics_v1@{QUALITY_HEURISTICS_SCHEMA_VERSION}",
        original_score=round(original, 6),
        penalized_score=round(penalized, 6),
        penalties_applied=tuple(applied),
        diagnostic="; ".join(diagnostic_parts) if diagnostic_parts else "no penalties",
    )
