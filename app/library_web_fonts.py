"""Web-font loader for library-page rendered HTML.

L-20 corrected strategy (Frank, 2026-06-04 02:35 UTC).

The original L-20 fix loaded only those brand-declared families that
happened to be present on Google Fonts and dropped everything else. The
result on prod was that the great majority of brands (whose first-
preference fonts are private CDN-only faces like ``PP Right Grotesk``,
``Sohne``, ``ABC Diatype``) silently fell through to the system fallback
and still read identically across the library.

Inspirado-no-copiado correction. For every brand we:

1. Name the brand's actual font in a disclosure block
   ("Aeon uses PP Right Grotesk Wide.") and attribute the foundry.
2. Render every specimen on the page with a curated free alternative
   (e.g. Plus Jakarta Sans for the PP Right Grotesk row) and attribute
   the free alternative's designer.
3. Load the free alternative via Google Fonts and emit a CSS root
   override so ``--ds-font-display`` / ``--ds-font-body`` / ``--ds-font-mono``
   point at the free-alternative family instead of falling through to a
   system fallback.

The pairing table lives in ``app.brand_font_registry``. This module is
the thin layer that walks brand tokens, picks first-preference fonts per
slot, resolves them through the registry, and returns:

- the ``<link>`` tag for the free-alternative Google Fonts URLs,
- the structured disclosure payload the indexer renders into HTML,
- a CSS root-block override that swaps ``--ds-font-*`` to the free
  alternative families.

Public API
----------
- ``extract_first_preference_families(tokens)`` - one family per font slot.
- ``resolve_free_alternatives(tokens)`` - de-dup tuple of ``BrandFontMapping``.
- ``build_google_fonts_link_tag(tokens)`` - single ``<link>`` for every alt.
- ``build_font_disclosure_payload(tokens)`` - structured disclosure data.
- ``render_font_disclosure_html(payload, brand_display_name)`` - aside block.
- ``build_font_alternative_root_block(tokens)`` - CSS root overrides.
- ``LIBRARY_WEB_FONTS_SCHEMA_VERSION`` - shape sentinel.

The legacy v1 API (``extract_google_font_families``) is retained as a
thin alias that delegates to the new resolver so any external caller
that imported it keeps working; the allowlist constant
(``GOOGLE_FONT_ALLOWLIST``) is also retained for back-compat.

Throwaway: NO. Quality floor applies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from app.brand_font_registry import (
    BRAND_FONT_REGISTRY_SCHEMA_VERSION,
    BrandFontMapping,
    DEFAULT_FREE_ALTERNATIVE,
    build_multi_family_google_fonts_url,
    lookup,
)


# Schema sentinel. v2 swaps the v1 allowlist-and-link-tag contract for
# the registry-driven inspirado-no-copiado contract. Downstream consumers
# keying off v1 will break and SHOULD: the rendered-page contract changed.
LIBRARY_WEB_FONTS_SCHEMA_VERSION: Final[str] = "library_web_fonts_v2"


# Token field names that may carry font-family stacks. Order matters:
# display first, then body, then mono. Underscored and ``ds-``-prefixed
# variants both appear depending on the source pipeline.
FONT_TOKEN_KEYS: Final[tuple[str, ...]] = (
    "ds-font-display",
    "ds-font-body",
    "ds-font-mono",
    "ds-font-accent",
    "font-display",
    "font-body",
    "font-mono",
    "font-accent",
    "font_display",
    "font_body",
    "font_mono",
    "font_accent",
)
"""Token keys the parser inspects, in priority order.

Both ``ds-`` prefixed (seed) and bare (organic) forms are included so
the parser is shape-agnostic. Underscore variants are included so an
organic row that surfaced through the ``font_display`` envelope still
flows through.
"""


# Per-semantic-slot alias map. The resolver walks the aliases for each
# slot in order and uses the first non-empty value as that slot's
# source stack. Accent slots are intentionally excluded because they
# would over-load the disclosure block; a future extension can add
# accent without bumping the schema version.
_FONT_SLOT_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "display": (
        "ds-font-display",
        "font-display",
        "font_display",
    ),
    "body": (
        "ds-font-body",
        "font-body",
        "font_body",
    ),
    "mono": (
        "ds-font-mono",
        "font-mono",
        "font_mono",
    ),
}


# Curated set of Google Fonts family names. Retained for back-compat
# with the v1 public API. The v2 resolver does NOT use this as a gating
# filter; the registry's curated alternatives drive what loads.
GOOGLE_FONT_ALLOWLIST: Final[frozenset[str]] = frozenset({
    "Inter",
    "Roboto",
    "Open Sans",
    "Lato",
    "Montserrat",
    "Poppins",
    "Source Sans 3",
    "Source Sans Pro",
    "Work Sans",
    "Nunito",
    "Nunito Sans",
    "Manrope",
    "DM Sans",
    "IBM Plex Sans",
    "Public Sans",
    "Plus Jakarta Sans",
    "Outfit",
    "Albert Sans",
    "Geist",
    "Onest",
    "Figtree",
    "Hanken Grotesk",
    "Space Grotesk",
    "Archivo",
    "Karla",
    "Cabin",
    "Mulish",
    "Rubik",
    "Barlow",
    "Fira Sans",
    "Noto Sans",
    "PT Sans",
    "Oxygen",
    "Ubuntu",
    "Bitter",
    "Playfair Display",
    "Merriweather",
    "Lora",
    "PT Serif",
    "Source Serif 4",
    "Source Serif Pro",
    "Cormorant Garamond",
    "EB Garamond",
    "Crimson Pro",
    "Crimson Text",
    "Libre Baskerville",
    "Libre Caslon Text",
    "Spectral",
    "Fraunces",
    "DM Serif Display",
    "DM Serif Text",
    "IBM Plex Serif",
    "Noto Serif",
    "Bodoni Moda",
    "Newsreader",
    "Literata",
    "Anton",
    "Bebas Neue",
    "Oswald",
    "Raleway",
    "Quicksand",
    "Comfortaa",
    "Josefin Sans",
    "Abril Fatface",
    "JetBrains Mono",
    "Fira Code",
    "Fira Mono",
    "Source Code Pro",
    "IBM Plex Mono",
    "Space Mono",
    "Roboto Mono",
    "Inconsolata",
    "Ubuntu Mono",
    "DM Mono",
    "Geist Mono",
})


# Generic CSS family keywords. The resolver skips these when looking
# for a slot's first-preference brand family; otherwise every brand
# stack ending in ``sans-serif`` would feed the literal ``"sans-serif"``
# string to the registry resolver.
_GENERIC_FAMILY_KEYWORDS: Final[frozenset[str]] = frozenset({
    "sans-serif",
    "serif",
    "monospace",
    "system-ui",
    "ui-monospace",
    "ui-sans-serif",
    "ui-serif",
    "cursive",
    "fantasy",
    "inherit",
    "initial",
    "unset",
    "revert",
})


# A CSS font-family value is a comma-separated list of family names,
# any of which may be quoted (single or double). The quote-stripper
# runs twice to handle double-quoted values; the regex strips one
# quote pass per call.
_FAMILY_QUOTE_RE: Final[re.Pattern[str]] = re.compile(r"^['\"]|['\"]$")


def _parse_family_stack(value: str) -> tuple[str, ...]:
    """Split a CSS font-family value into clean family names.

    Returns names in source order (first preference first), with all
    quoting and surrounding whitespace stripped. Empty entries (from
    trailing commas or doubled commas) are dropped.

    Generic families (``sans-serif``, ``serif``, ``monospace``,
    ``system-ui``, ``ui-monospace``) are preserved so callers can decide
    whether to honor them; ``_first_real_family`` skips them.

    Pure-data. No I/O.
    """
    if not value:
        return ()
    parts: list[str] = []
    for raw in value.split(","):
        stripped = raw.strip()
        cleaned = _FAMILY_QUOTE_RE.sub("", stripped).strip()
        cleaned = _FAMILY_QUOTE_RE.sub("", cleaned).strip()
        if cleaned:
            parts.append(cleaned)
    return tuple(parts)


def _first_real_family(stack: tuple[str, ...]) -> str | None:
    """Return the first non-generic family from a parsed family stack.

    Walks the stack in source order and returns the first entry that is
    not a generic CSS keyword. Returns ``None`` when every entry is
    generic (defensive; real brand tokens always lead with a named
    family).
    """
    for family in stack:
        if family.lower() not in _GENERIC_FAMILY_KEYWORDS:
            return family
    return None


def _slot_first_preference(slot: str, tokens: dict[str, str]) -> str | None:
    """Return the first-preference family for ``slot`` from ``tokens``.

    Walks ``_FONT_SLOT_ALIASES[slot]`` in order, parses the first
    non-empty value's family stack, and returns the first non-generic
    family. Returns ``None`` when no alias is present or every stack is
    generics-only.
    """
    for alias in _FONT_SLOT_ALIASES[slot]:
        raw = tokens.get(alias)
        if not raw:
            continue
        stack = _parse_family_stack(raw)
        family = _first_real_family(stack)
        if family is not None:
            return family
    return None


# ----------------------------------------------------------------------
# Resolution data types
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class SlotResolution:
    """Per-slot resolver result.

    Fields:
        slot: ``"display"`` / ``"body"`` / ``"mono"``.
        brand_font_first_preference: verbatim brand family the resolver
            was asked about (preserved with original case / punctuation
            so the disclosure surfaces the brand's spelling). ``None``
            when the brand declared no family for this slot.
        mapping: the ``BrandFontMapping`` the registry returned.
    """

    slot: str
    brand_font_first_preference: str | None
    mapping: BrandFontMapping


@dataclass(frozen=True)
class FontDisclosurePayload:
    """Structured payload the indexer renders into the disclosure block.

    Fields:
        primary_resolution: drives the headline disclosure sentence.
            The display-slot resolution is preferred; falls back to the
            first available resolution.
        resolutions: every per-slot resolution.
        free_alternative_families: de-dup tuple of free-alternative
            family names referenced across all resolutions.
        google_fonts_url: single Google Fonts URL covering every family
            in ``free_alternative_families``.
        schema_version: stamp for downstream consumers.
    """

    primary_resolution: SlotResolution
    resolutions: tuple[SlotResolution, ...]
    free_alternative_families: tuple[str, ...]
    google_fonts_url: str | None
    schema_version: str = field(default=BRAND_FONT_REGISTRY_SCHEMA_VERSION)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def extract_first_preference_families(tokens: dict[str, str]) -> dict[str, str]:
    """Return ``{slot: family}`` for each slot the brand declares.

    Probes slots in the order display, body, mono. Slots with no
    brand-declared family are omitted, so a brand that ships only a
    display font produces a one-entry dict.

    Pure-data. No I/O.
    """
    if not tokens:
        return {}
    result: dict[str, str] = {}
    for slot in _FONT_SLOT_ALIASES:
        family = _slot_first_preference(slot, tokens)
        if family is not None:
            result[slot] = family
    return result


def _resolutions_from_tokens(tokens: dict[str, str]) -> tuple[SlotResolution, ...]:
    """Resolve every slot's brand family into a ``SlotResolution`` tuple.

    Returns at least one entry; when the brand has no font tokens at
    all, a single ``display``-slot resolution carrying
    ``DEFAULT_FREE_ALTERNATIVE`` is returned so the disclosure block
    always has something to render.

    Pure-data. No I/O.
    """
    families = extract_first_preference_families(tokens)
    if not families:
        return (
            SlotResolution(
                slot="display",
                brand_font_first_preference=None,
                mapping=DEFAULT_FREE_ALTERNATIVE,
            ),
        )
    resolutions: list[SlotResolution] = []
    for slot, family in families.items():
        resolutions.append(
            SlotResolution(
                slot=slot,
                brand_font_first_preference=family,
                mapping=lookup(family),
            )
        )
    return tuple(resolutions)


def resolve_free_alternatives(tokens: dict[str, str]) -> tuple[BrandFontMapping, ...]:
    """Return the de-duplicated tuple of free-alternative mappings to load.

    De-dup is by ``free_alternative_name``. Order follows the slot
    iteration order (display, body, mono).

    Pure-data. No I/O.
    """
    seen: set[str] = set()
    ordered: list[BrandFontMapping] = []
    for resolution in _resolutions_from_tokens(tokens):
        name = resolution.mapping["free_alternative_name"]
        if name in seen:
            continue
        seen.add(name)
        ordered.append(resolution.mapping)
    return tuple(ordered)


def build_google_fonts_link_tag(tokens: dict[str, str]) -> str | None:
    """Build a single Google Fonts ``<link>`` tag for every free alternative.

    Returns ``None`` only when the URL builder returns ``None`` (empty
    family tuple); in v2 this is effectively unreachable because the
    resolver always returns at least the default Inter mapping. Callers
    should still treat ``None`` as "no font load" and fall through to
    the brand's CSS stack.

    The URL requests every free-alternative family with the canonical
    ``wght@300..700`` weight range and ``display=swap`` so the library
    page paints immediately with the fallback face and swaps to the
    loaded face when ready. Single-quoted attributes are used so the
    indexer can inline the tag inside a double-quoted HTML string
    without escaping.

    Pure-data. No I/O.
    """
    alternatives = resolve_free_alternatives(tokens)
    if not alternatives:
        return None
    families = tuple(alt["free_alternative_name"] for alt in alternatives)
    url = build_multi_family_google_fonts_url(families)
    if url is None:
        return None
    return f'<link rel="stylesheet" href="{url}" crossorigin="anonymous">'


def build_font_disclosure_payload(tokens: dict[str, str]) -> FontDisclosurePayload:
    """Build the disclosure payload the indexer injects into rendered HTML.

    Carries per-slot resolutions, the de-duplicated free-alternative
    families, and the Google Fonts URL covering every free alternative.
    The primary disclosure line is keyed off the display-slot resolution
    when available, otherwise the first resolution in the tuple.

    Pure-data. No I/O.
    """
    resolutions = _resolutions_from_tokens(tokens)
    primary = next(
        (r for r in resolutions if r.slot == "display"),
        resolutions[0],
    )
    seen: set[str] = set()
    families: list[str] = []
    for resolution in resolutions:
        name = resolution.mapping["free_alternative_name"]
        if name in seen:
            continue
        seen.add(name)
        families.append(name)
    google_url = build_multi_family_google_fonts_url(tuple(families))
    return FontDisclosurePayload(
        primary_resolution=primary,
        resolutions=resolutions,
        free_alternative_families=tuple(families),
        google_fonts_url=google_url,
    )


def render_font_disclosure_html(
    payload: FontDisclosurePayload,
    *,
    brand_display_name: str,
) -> str:
    """Render the disclosure ``<aside>`` block from a payload.

    Output shape::

        <aside class="rs-font-attribution" data-rs-class="font-attribution">
          <strong>{brand} uses {brand_font_name}.</strong>
          Rendered here with <a href="{url}">{alt_name}</a>
          (free, designed by {alt_designer}).
        </aside>

    When the brand declared no font, the headline reads
    ``"{brand} ships no captured brand font."`` instead of pretending
    the brand picked the default.

    Pure-data. No I/O. Deterministic.
    """
    primary = payload.primary_resolution
    brand_font_name = primary.mapping["brand_font_name"]
    alt_name = primary.mapping["free_alternative_name"]
    alt_url = primary.mapping["free_alternative_google_fonts_url"]
    alt_designer = primary.mapping["free_alternative_designer"]
    if primary.brand_font_first_preference is None:
        headline = (
            f"<strong>{brand_display_name} ships no captured brand font.</strong>"
        )
    else:
        headline = (
            f"<strong>{brand_display_name} uses {brand_font_name}.</strong>"
        )
    return (
        '<aside class="rs-font-attribution" data-rs-class="font-attribution">'
        f"{headline} "
        f'Rendered here with <a href="{alt_url}">{alt_name}</a> '
        f"(free, designed by {alt_designer})."
        "</aside>"
    )


def build_font_alternative_root_block(tokens: dict[str, str]) -> str:
    """Return a CSS ``:root`` block that overrides ``--ds-font-*`` to the free alternative.

    The indexer emits the brand's own ``--ds-font-display`` /
    ``--ds-font-body`` / ``--ds-font-mono`` variables verbatim from the
    brand token stack, which means the rendered specimen falls through
    to a system font when no web font for the brand's preferred face is
    available. This helper returns an additional ``:root { ... }`` block
    that overrides those variables to a stack led by the free-alternative
    family, so the loaded Google Fonts face is what actually paints.

    The block is emitted AFTER the brand's ``:root`` block in source
    order so the override wins on cascade. Each slot whose resolver
    returned a free alternative gets one override line; slots the brand
    never declared still get a default override (so the page does not
    paint in the browser default) keyed off the body-slot.

    Always returns a non-empty string ending in a newline.
    """
    resolutions = _resolutions_from_tokens(tokens)
    real_slot_resolutions = [
        r for r in resolutions if r.brand_font_first_preference is not None
    ]
    if not real_slot_resolutions:
        default_family = DEFAULT_FREE_ALTERNATIVE["free_alternative_name"]
        return (
            ":root {\n"
            f"  --ds-font-display: '{default_family}', system-ui, sans-serif;\n"
            f"  --ds-font-body: '{default_family}', system-ui, sans-serif;\n"
            "}\n"
        )
    lines = [":root {"]
    for resolution in real_slot_resolutions:
        family = resolution.mapping["free_alternative_name"]
        if resolution.slot == "mono":
            fallback = "ui-monospace, monospace"
        else:
            fallback = "system-ui, sans-serif"
        lines.append(
            f"  --ds-font-{resolution.slot}: '{family}', {fallback};"
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# v1 back-compat shim
# ----------------------------------------------------------------------


def extract_google_font_families(tokens: dict[str, str]) -> tuple[str, ...]:
    """v1 alias: return the families the link tag will load.

    Retained so any external caller importing the v1 name keeps working.
    Returns the de-dup tuple of free-alternative family names (not the
    brand's first-preference families). Order matches
    ``resolve_free_alternatives``.

    Pure-data. No I/O.
    """
    return tuple(alt["free_alternative_name"] for alt in resolve_free_alternatives(tokens))
