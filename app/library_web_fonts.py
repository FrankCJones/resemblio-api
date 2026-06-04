"""Web-font loader for library-page rendered HTML.

L-20 fix (Frank, 2026-06-04). The library indexer emits the brand's
captured ``--ds-font-display`` / ``--ds-font-body`` / ``--ds-font-mono``
CSS variables, but the rendered HTML never loads the actual web fonts.
Every brand's typography therefore falls through the family stack to
the first system fallback (``Helvetica Neue``, ``Georgia``, ``Consolas``),
which is why ``/library/<brand>/alphabet/`` reads identically across
every brand on the live site.

This module emits a single ``<link>`` tag pointing at Google Fonts (the
only no-key web-font CDN we currently trust) for every brand-supplied
family that matches the allowlist. Anything outside the allowlist is
ignored, so a brand that ships an internal proprietary face still
renders cleanly with its CSS fallback rather than producing a 404 burst
or a CORS warning. The allowlist is data, not code; growing it later is
a one-line edit and does not bump the schema version.

Why an allowlist rather than "load every family we see":
    - Many brand stacks include private-licensed faces (``PP Right
      Grotesk Wide``, ``Söhne``, ``Atlas Grotesk``) that are NOT on
      any free CDN; requesting them would 404 with no fallback.
    - The library page is server-rendered and cached; a noisy 404 per
      brand multiplies through the corpus.
    - Google Fonts is the de-facto free CDN and covers the
      open-licensed face most common-name brand stacks fall back to.

Public API:
    - extract_google_font_families(tokens) -> tuple[str, ...]
    - build_google_fonts_link_tag(tokens) -> str | None

Both functions are pure-data. Tests at
``tests/test_library_web_fonts.py`` pin parsing + the allowlist.

Throwaway: NO. Quality floor applies.
"""
from __future__ import annotations

import re
from typing import Final

# Schema sentinel for downstream tooling that wants to detect the
# parser-shape version. Bump when the public-API output shape changes
# (e.g. moving from ``str | None`` to a structured dict).
LIBRARY_WEB_FONTS_SCHEMA_VERSION: Final[str] = "library_web_fonts_v1"

# Curated list of font families known to be served by Google Fonts at
# the time of writing. Names are case-sensitive against the Google Fonts
# API. Growing this list later is a one-line edit; the parser is
# allowlist-driven so an unknown family silently drops out rather than
# producing a 404. This bias toward "ship what we know" matches the
# workspace MVP rule: ship the working surface, harden as observed.
#
# Selection rule: includes the families most common in modern web
# brand stacks (sans-serif neo-grotesques, popular serifs, common
# monos). Proprietary CDN-only faces (Söhne, Atlas, PP Right Grotesk)
# are deliberately excluded.
GOOGLE_FONT_ALLOWLIST: Final[frozenset[str]] = frozenset({
    # Sans-serif (most common library brand defaults)
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
    # Serif
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
    # Display / brand-leaning sans
    "Anton",
    "Bebas Neue",
    "Oswald",
    "Raleway",
    "Quicksand",
    "Comfortaa",
    "Josefin Sans",
    "Abril Fatface",
    # Monospace
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
"""Curated set of Google Fonts family names. Membership is case-sensitive.

Updates: append-only. Removing a family is a behavior change (brands
that rendered with the family suddenly fall back). Adding a family is
back-compat. Tests in ``test_library_web_fonts.py`` pin a representative
subset to prevent accidental removal.
"""

# Field names in the brand tokens dict that may carry font-family
# stacks. Both the bare and ``ds-``-prefixed forms cover the seed +
# organic shapes (the indexer's existing ``_ds_var_name`` normalization
# does NOT run on the values themselves, only on the keys).
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

# A CSS font-family value is a comma-separated list of family names,
# any of which may be quoted (single or double). Strip quotes + surrounding
# whitespace to get a clean family name for allowlist lookup.
_FAMILY_QUOTE_RE: Final[re.Pattern[str]] = re.compile(r"^['\"]|['\"]$")


def _parse_family_stack(value: str) -> tuple[str, ...]:
    """Split a CSS font-family value into clean family names.

    Returns names in source order (first preference first), with all
    quoting and surrounding whitespace stripped. Empty entries (from
    trailing commas or doubled commas) are dropped.

    Generic families (``sans-serif``, ``serif``, ``monospace``,
    ``system-ui``, ``ui-monospace``) are preserved in the output so
    callers can decide whether to honor them; the allowlist filter
    happens in ``extract_google_font_families``.

    Pure-data. No I/O.
    """
    if not value:
        return ()
    parts = []
    for raw in value.split(","):
        stripped = raw.strip()
        # Strip a single leading and trailing quote of either kind.
        cleaned = _FAMILY_QUOTE_RE.sub("", stripped).strip()
        # The regex strips one quote pass; double-quoted values need a
        # second pass to land at the bare name.
        cleaned = _FAMILY_QUOTE_RE.sub("", cleaned).strip()
        if cleaned:
            parts.append(cleaned)
    return tuple(parts)


def extract_google_font_families(tokens: dict[str, str]) -> tuple[str, ...]:
    """Return Google-Fonts-allowlisted families referenced by the brand tokens.

    Walks every ``FONT_TOKEN_KEYS`` entry present in ``tokens``, splits
    each value on commas, normalizes the family name, and keeps only
    members of ``GOOGLE_FONT_ALLOWLIST``. The result is deduplicated by
    family name (case-sensitive) preserving first-seen order, so a
    caller can pass the tuple directly to a Google Fonts ``family=``
    URL builder.

    Returns an empty tuple if no allowlisted family is found - the
    caller's link-tag emitter will then produce ``None`` and the
    page renders with the brand's CSS fallback (current behaviour).

    Pure-data. No I/O. Deterministic.
    """
    if not tokens:
        return ()
    seen: set[str] = set()
    ordered: list[str] = []
    for key in FONT_TOKEN_KEYS:
        raw = tokens.get(key)
        if not raw:
            continue
        for family in _parse_family_stack(raw):
            if family in GOOGLE_FONT_ALLOWLIST and family not in seen:
                seen.add(family)
                ordered.append(family)
    return tuple(ordered)


def build_google_fonts_link_tag(tokens: dict[str, str]) -> str | None:
    """Build a single Google Fonts ``<link>`` tag covering every allowlisted family.

    Returns ``None`` when ``tokens`` carries no allowlisted family; the
    caller should treat ``None`` as "no font load needed, fall through
    to CSS fallback". Returns a single ``<link>`` tag string otherwise.

    The URL requests every detected family with the canonical weight
    range Google Fonts accepts (``wght@300..700``) so the
    ``font-weight`` token (which the indexer emits via the contract
    default ``--ds-font-weight-display: 600``) actually has a face
    available. ``display=swap`` lets the page paint immediately with
    the fallback face and swap to the loaded face when ready, so the
    library page never blocks on font load.

    The tag uses single-quoted attributes so the indexer can inline it
    inside a double-quoted HTML string without escaping (the rendered
    library article is itself an HTML fragment, not a template).

    Pure-data. No I/O.
    """
    families = extract_google_font_families(tokens)
    if not families:
        return None
    # Google Fonts URL form: family=Name:wght@300..700&family=Name2:wght@300..700
    # Spaces in family names encode as '+'.
    family_params = "&".join(
        f"family={family.replace(' ', '+')}:wght@300..700" for family in families
    )
    url = f"https://fonts.googleapis.com/css2?{family_params}&display=swap"
    return f'<link rel="stylesheet" href="{url}" crossorigin="anonymous">'
