"""Brand-font registry: maps a brand's first-preference font to a free alternative.

Phase 1 of the Inspirado-no-copiado correction (Frank, 2026-06-04 02:35 UTC;
plan at ``projects/OptSus Team/cto-reviews/2026-06-04-resemblio-library-inspirado-no-copiado-correction-plan.md``).

The library page's job is to surface, for every brand in the corpus:

1. The actual brand font name (e.g. "Aeon uses PP Right Grotesk Wide").
2. The free alternative we render with, attributed back to its designer
   (e.g. "Rendered here with Plus Jakarta Sans, free, designed by Tokotype").
3. A Google Fonts link for the free alternative so every specimen on the
   page actually paints with the alternative face rather than falling
   through the brand's CSS stack to ``Helvetica Neue`` / ``Georgia``.

This module is the pure-data layer. ``lookup()`` accepts the brand's
first-preference font name (extracted from the brand's CSS variable
font-family stack) and returns a ``BrandFontMapping`` carrying both the
brand-font attribution and the free-alternative render target. Unknown
brand fonts fall back to a default Inter mapping so the page still
renders cleanly.

Public API
----------

- ``BrandFontMapping`` (TypedDict) - the per-brand registry row.
- ``BRAND_FONT_REGISTRY`` - dict of brand-font slugs to mappings.
- ``DEFAULT_FREE_ALTERNATIVE`` - the Inter fallback mapping.
- ``lookup(brand_font_first_preference)`` - resolver with exact + fuzzy + default.
- ``BRAND_FONT_REGISTRY_SCHEMA_VERSION`` - shape sentinel.

No I/O. No network. Pure data + a deterministic resolver. Tests at
``tests/test_brand_font_registry.py`` pin the registry shape and the
resolver's three branches (hit / fuzzy / default).

Throwaway: NO. Quality floor applies.
"""
from __future__ import annotations

import re
from typing import Final, TypedDict


BRAND_FONT_REGISTRY_SCHEMA_VERSION: Final[str] = "brand_font_registry_v1"
"""Schema sentinel. Bump when ``BrandFontMapping`` shape changes.

Downstream tools (library indexer disclosure injection, OG-image
attribution renderer, future converters) key off this string to detect
contract drift. Adding a new mapping row is back-compat and does NOT bump
the version; removing a field or changing a field name does.
"""


class BrandFontMapping(TypedDict):
    """One row in the brand-font registry.

    Fields
    ------
    brand_font_name:
        Canonical display name of the brand's actual font as named by
        the foundry (e.g. ``"PP Right Grotesk Wide"``). Surfaced in the
        disclosure block as ``"{brand} uses {brand_font_name}."``.
    brand_font_designer:
        Foundry or designer credit for the brand font. Surfaced for
        attribution honesty even though we do NOT render with it.
    free_alternative_name:
        The Google Fonts family the library page actually renders with
        (e.g. ``"Plus Jakarta Sans"``). Must be a member of
        ``library_web_fonts.GOOGLE_FONT_ALLOWLIST`` so the link tag
        builder will emit a real URL for it.
    free_alternative_designer:
        Designer credit for the free alternative. Surfaced in the
        disclosure as ``"free, designed by {free_alternative_designer}"``.
    free_alternative_google_fonts_url:
        Pre-built single-family Google Fonts URL with the canonical
        ``wght@300..700`` range and ``display=swap``. Carried as a
        first-class field so consumers do not have to re-derive it.
    similarity_rationale_short_line:
        One-sentence reason this free alternative is a defensible visual
        stand-in (e.g. ``"Similar humanist proportions and generous
        x-height"``). Editorial honesty, not algorithmic similarity
        scoring; the registry author picks each pairing on visual
        judgment.
    """

    brand_font_name: str
    brand_font_designer: str
    free_alternative_name: str
    free_alternative_designer: str
    free_alternative_google_fonts_url: str
    similarity_rationale_short_line: str


# Canonical weight range and display value the URL builder emits per
# family. Matches the contract in ``library_web_fonts.build_google_fonts_link_tag``
# so the disclosure block and the actual loaded face stay in sync.
_GOOGLE_FONTS_WEIGHT_RANGE: Final[str] = "wght@300..700"
_GOOGLE_FONTS_DISPLAY_PARAM: Final[str] = "display=swap"
_GOOGLE_FONTS_BASE: Final[str] = "https://fonts.googleapis.com/css2"


def build_single_family_google_fonts_url(family: str) -> str:
    """Build the single-family Google Fonts URL for ``family``.

    Used by the registry-row constructor below and by callers that need
    to derive the disclosure link target without rebuilding the format
    string from scratch. Spaces encode to ``+`` per Google's URL syntax.

    Pure-data. Deterministic.
    """
    family_param = f"family={family.replace(' ', '+')}:{_GOOGLE_FONTS_WEIGHT_RANGE}"
    return f"{_GOOGLE_FONTS_BASE}?{family_param}&{_GOOGLE_FONTS_DISPLAY_PARAM}"


def build_multi_family_google_fonts_url(families: tuple[str, ...]) -> str | None:
    """Build a single Google Fonts URL covering every family in ``families``.

    Returns ``None`` for an empty tuple. Used by the indexer to load
    every free alternative referenced across the brand's font slots
    (display + body + mono) in one stylesheet request.

    Each family receives the canonical ``wght@300..700`` range; the
    ``display=swap`` directive applies to the whole stylesheet.
    """
    if not families:
        return None
    family_params = "&".join(
        f"family={family.replace(' ', '+')}:{_GOOGLE_FONTS_WEIGHT_RANGE}"
        for family in families
    )
    return f"{_GOOGLE_FONTS_BASE}?{family_params}&{_GOOGLE_FONTS_DISPLAY_PARAM}"


def _mapping(
    *,
    brand_font_name: str,
    brand_font_designer: str,
    free_alternative_name: str,
    free_alternative_designer: str,
    similarity_rationale_short_line: str,
) -> BrandFontMapping:
    """Construct a ``BrandFontMapping`` and auto-derive the Google Fonts URL.

    The URL field is derived from ``free_alternative_name`` so a row
    author never has to hand-write a URL and the URL can never drift out
    of sync with the family name. The brand-font fields carry no URL
    because we do NOT load the brand font; attribution only.
    """
    return BrandFontMapping(
        brand_font_name=brand_font_name,
        brand_font_designer=brand_font_designer,
        free_alternative_name=free_alternative_name,
        free_alternative_designer=free_alternative_designer,
        free_alternative_google_fonts_url=build_single_family_google_fonts_url(
            free_alternative_name
        ),
        similarity_rationale_short_line=similarity_rationale_short_line,
    )


# The default free-alternative mapping used when the brand's first-preference
# font is unknown to the registry. Inter is the safest universal stand-in:
# it covers nearly every modern brand stack's intent (neo-grotesque sans)
# and is one of the most widely served free families.
DEFAULT_FREE_ALTERNATIVE: Final[BrandFontMapping] = _mapping(
    brand_font_name="Default free type system",
    brand_font_designer="Resemblio",
    free_alternative_name="Inter",
    free_alternative_designer="Rasmus Andersson",
    similarity_rationale_short_line=(
        "Inter is a neutral neo-grotesque chosen as the safe default when "
        "the brand's source font is unknown to the registry."
    ),
)
"""Fallback row returned by ``lookup()`` for unknown brand fonts.

The disclosure block renders a generic "Default free type system" line
when this mapping is returned, so the user sees clearly that no specific
brand attribution was available rather than a misleading match.
"""


# Mapping table. Keys are normalized brand-font slugs (lowercase, ASCII,
# whitespace collapsed to single spaces) so the resolver can match
# input variations ("PP Right Grotesk Wide", "pp right grotesk wide",
# "PP-Right-Grotesk-Wide") against one canonical key.
#
# Selection rule for free alternatives
# ------------------------------------
# Free alternatives are picked on visual judgment (proportion, x-height,
# stroke contrast, terminal style). The ``similarity_rationale_short_line``
# explains the call. We do NOT claim the alternative IS the brand font; the
# disclosure block makes the substitution explicit. Inspirado, no copiado.
BRAND_FONT_REGISTRY: Final[dict[str, BrandFontMapping]] = {
    "pp right grotesk wide": _mapping(
        brand_font_name="PP Right Grotesk Wide",
        brand_font_designer="Pangram Pangram",
        free_alternative_name="Plus Jakarta Sans",
        free_alternative_designer="Tokotype",
        similarity_rationale_short_line=(
            "Plus Jakarta Sans carries the same wide-set neo-grotesque "
            "proportions and confident terminals."
        ),
    ),
    "pp right grotesk": _mapping(
        brand_font_name="PP Right Grotesk",
        brand_font_designer="Pangram Pangram",
        free_alternative_name="Plus Jakarta Sans",
        free_alternative_designer="Tokotype",
        similarity_rationale_short_line=(
            "Plus Jakarta Sans is the closest free analogue for "
            "PP Right Grotesk's geometric-humanist hybrid."
        ),
    ),
    "sohne": _mapping(
        brand_font_name="Sohne",
        brand_font_designer="Klim Type Foundry",
        free_alternative_name="Inter",
        free_alternative_designer="Rasmus Andersson",
        similarity_rationale_short_line=(
            "Inter matches Sohne's neo-grotesque rhythm and is the "
            "most widely-deployed free analogue."
        ),
    ),
    "soehne": _mapping(
        brand_font_name="Sohne",
        brand_font_designer="Klim Type Foundry",
        free_alternative_name="Inter",
        free_alternative_designer="Rasmus Andersson",
        similarity_rationale_short_line=(
            "Inter matches Sohne's neo-grotesque rhythm and is the "
            "most widely-deployed free analogue."
        ),
    ),
    "abc diatype": _mapping(
        brand_font_name="ABC Diatype",
        brand_font_designer="Dinamo",
        free_alternative_name="Inter",
        free_alternative_designer="Rasmus Andersson",
        similarity_rationale_short_line=(
            "Inter shares ABC Diatype's even color and quiet, "
            "screen-first construction."
        ),
    ),
    "atlas grotesk": _mapping(
        brand_font_name="Atlas Grotesk",
        brand_font_designer="Commercial Type",
        free_alternative_name="Plus Jakarta Sans",
        free_alternative_designer="Tokotype",
        similarity_rationale_short_line=(
            "Plus Jakarta Sans mirrors Atlas Grotesk's open apertures "
            "and editorial neutrality."
        ),
    ),
    "san francisco": _mapping(
        brand_font_name="San Francisco",
        brand_font_designer="Apple",
        free_alternative_name="Inter",
        free_alternative_designer="Rasmus Andersson",
        similarity_rationale_short_line=(
            "Inter is the canonical free analogue for San Francisco; "
            "the two share humanist-grotesque DNA."
        ),
    ),
    "sf pro": _mapping(
        brand_font_name="SF Pro",
        brand_font_designer="Apple",
        free_alternative_name="Inter",
        free_alternative_designer="Rasmus Andersson",
        similarity_rationale_short_line=(
            "Inter is the canonical free analogue for SF Pro; "
            "the two share humanist-grotesque DNA."
        ),
    ),
    "inter": _mapping(
        brand_font_name="Inter",
        brand_font_designer="Rasmus Andersson",
        free_alternative_name="Inter",
        free_alternative_designer="Rasmus Andersson",
        similarity_rationale_short_line=(
            "Inter is already free and served by Google Fonts; "
            "we render with the brand's actual face."
        ),
    ),
    "roboto": _mapping(
        brand_font_name="Roboto",
        brand_font_designer="Christian Robertson",
        free_alternative_name="Roboto",
        free_alternative_designer="Christian Robertson",
        similarity_rationale_short_line=(
            "Roboto is already free and served by Google Fonts; "
            "we render with the brand's actual face."
        ),
    ),
    "gt america": _mapping(
        brand_font_name="GT America",
        brand_font_designer="Grilli Type",
        free_alternative_name="Inter",
        free_alternative_designer="Rasmus Andersson",
        similarity_rationale_short_line=(
            "Inter approximates GT America's American-grotesque "
            "structure with similar overall color."
        ),
    ),
    "untitled sans": _mapping(
        brand_font_name="Untitled Sans",
        brand_font_designer="Klim Type Foundry",
        free_alternative_name="Inter",
        free_alternative_designer="Rasmus Andersson",
        similarity_rationale_short_line=(
            "Inter captures Untitled Sans's quiet, evenly-weighted "
            "screen rhythm."
        ),
    ),
    "pangram sans": _mapping(
        brand_font_name="Pangram Sans",
        brand_font_designer="Pangram Pangram",
        free_alternative_name="Inter",
        free_alternative_designer="Rasmus Andersson",
        similarity_rationale_short_line=(
            "Inter provides a neutral neo-grotesque stand-in for "
            "the Pangram Pangram family."
        ),
    ),
    "suisse": _mapping(
        brand_font_name="Suisse",
        brand_font_designer="Swiss Typefaces",
        free_alternative_name="Inter",
        free_alternative_designer="Rasmus Andersson",
        similarity_rationale_short_line=(
            "Inter mirrors Suisse's Swiss-grotesque neutrality and "
            "wide weight range."
        ),
    ),
    "suisse intl": _mapping(
        brand_font_name="Suisse Int'l",
        brand_font_designer="Swiss Typefaces",
        free_alternative_name="Inter",
        free_alternative_designer="Rasmus Andersson",
        similarity_rationale_short_line=(
            "Inter mirrors Suisse Int'l's Swiss-grotesque neutrality."
        ),
    ),
    "larsseit": _mapping(
        brand_font_name="Larsseit",
        brand_font_designer="Type Dynamic",
        free_alternative_name="Plus Jakarta Sans",
        free_alternative_designer="Tokotype",
        similarity_rationale_short_line=(
            "Plus Jakarta Sans matches Larsseit's geometric "
            "construction and friendly terminals."
        ),
    ),
    "whyte": _mapping(
        brand_font_name="Whyte",
        brand_font_designer="ABC Dinamo",
        free_alternative_name="Manrope",
        free_alternative_designer="Mikhail Sharanda",
        similarity_rationale_short_line=(
            "Manrope shares Whyte's rounded, soft-edged neo-grotesque "
            "character."
        ),
    ),
    "tiempos": _mapping(
        brand_font_name="Tiempos",
        brand_font_designer="Klim Type Foundry",
        free_alternative_name="Lora",
        free_alternative_designer="Cyreal",
        similarity_rationale_short_line=(
            "Lora is a contemporary serif with the editorial color "
            "Tiempos brings to long-form reading."
        ),
    ),
    "tiempos text": _mapping(
        brand_font_name="Tiempos Text",
        brand_font_designer="Klim Type Foundry",
        free_alternative_name="Lora",
        free_alternative_designer="Cyreal",
        similarity_rationale_short_line=(
            "Lora is a contemporary serif suitable as a Tiempos Text "
            "stand-in for editorial body copy."
        ),
    ),
    "tiempos headline": _mapping(
        brand_font_name="Tiempos Headline",
        brand_font_designer="Klim Type Foundry",
        free_alternative_name="Playfair Display",
        free_alternative_designer="Claus Eggers Sorensen",
        similarity_rationale_short_line=(
            "Playfair Display brings the high-contrast display-serif "
            "energy that Tiempos Headline is used for."
        ),
    ),
    "founders grotesk": _mapping(
        brand_font_name="Founders Grotesk",
        brand_font_designer="Klim Type Foundry",
        free_alternative_name="Plus Jakarta Sans",
        free_alternative_designer="Tokotype",
        similarity_rationale_short_line=(
            "Plus Jakarta Sans has the open-aperture grotesque feel "
            "of Founders Grotesk."
        ),
    ),
    "academica": _mapping(
        brand_font_name="Academica",
        brand_font_designer="Storm Type Foundry",
        free_alternative_name="Lora",
        free_alternative_designer="Cyreal",
        similarity_rationale_short_line=(
            "Lora's contemporary serif voice stands in for Academica's "
            "warm editorial serif."
        ),
    ),
}
"""Curated brand-font -> free-alternative table. Append-only.

Removing a row would silently fall a brand back to the Inter default,
changing the rendered output without a code review trail. Adding a row
is back-compat. Keys are normalized via the same rules ``_normalize_key``
applies; case + punctuation in the input does not need to match.
"""


# Regex that collapses any run of non-alphanumeric characters (dashes,
# underscores, apostrophes, periods, multiple spaces) to a single space.
# Used to normalize brand-font keys and resolver inputs to a single shape.
_NORMALIZE_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


def _normalize_key(raw: str) -> str:
    """Lowercase, ASCII-strip, and collapse punctuation to single spaces.

    ``"PP-Right-Grotesk Wide"``, ``"PP Right Grotesk Wide"``, and
    ``"pp_right_grotesk_wide"`` all normalize to ``"pp right grotesk wide"``
    so they collide on the same registry key.

    Returns the empty string for ``None`` / empty / all-punctuation input.
    """
    if not raw:
        return ""
    lowered = raw.lower().strip()
    collapsed = _NORMALIZE_PUNCT_RE.sub(" ", lowered).strip()
    return collapsed


def _fuzzy_match(normalized_input: str) -> BrandFontMapping | None:
    """Try a stem-prefix and substring match against registry keys.

    Two passes:

    1. **Stem prefix.** Drop the last word from the input (so
       ``"pp right grotesk wide narrow"`` falls back to
       ``"pp right grotesk wide"``) and probe the registry. Repeat
       until either the registry hits or the stem is empty. This
       handles brand fonts that ship under a multi-word family name
       where the user-facing slug carries an extra weight/style word.

    2. **Substring containment.** Walk every registry key and return
       the first row whose key is a substring of the input or the input
       is a substring of the key. Handles ``"pp-right-grotesk"``
       matching the registered ``"pp right grotesk wide"`` row.

    Returns ``None`` when neither pass matches; the caller falls back
    to ``DEFAULT_FREE_ALTERNATIVE``.
    """
    if not normalized_input:
        return None
    parts = normalized_input.split()
    # Stem-prefix pass.
    while len(parts) > 1:
        parts.pop()
        candidate = " ".join(parts)
        if candidate in BRAND_FONT_REGISTRY:
            return BRAND_FONT_REGISTRY[candidate]
    # Substring containment pass.
    for key, mapping in BRAND_FONT_REGISTRY.items():
        if key in normalized_input or normalized_input in key:
            return mapping
    return None


def lookup(brand_font_first_preference: str) -> BrandFontMapping:
    """Resolve ``brand_font_first_preference`` to a ``BrandFontMapping``.

    Resolution order:

    1. **Exact match** on the normalized key (lowercase + punctuation
       collapsed to single spaces).
    2. **Fuzzy match** via stem-prefix drop and substring containment.
    3. **Default fallback** (``DEFAULT_FREE_ALTERNATIVE`` - Inter).

    Always returns a ``BrandFontMapping``; never raises and never
    returns ``None``. Callers can rely on this to drive a "render every
    page with a known free alternative" invariant. Empty / missing
    input returns the default, which is documented as the
    "Default free type system" disclosure.

    Pure-data. No I/O.
    """
    normalized = _normalize_key(brand_font_first_preference)
    if not normalized:
        return DEFAULT_FREE_ALTERNATIVE
    direct = BRAND_FONT_REGISTRY.get(normalized)
    if direct is not None:
        return direct
    fuzzy = _fuzzy_match(normalized)
    if fuzzy is not None:
        return fuzzy
    return DEFAULT_FREE_ALTERNATIVE
