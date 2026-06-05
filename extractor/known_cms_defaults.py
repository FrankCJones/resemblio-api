"""Known-CMS-default constants for the S20 confidence rubric.

Centralizes the value sets used by ``extractor.confidence_rubric`` to detect
extractions whose declared tokens are likely to be CMS defaults the visible
brand never actually adopts. Reference cases:

- WordPress + Gutenberg ships ``#007cba`` (the WP "wp-block-button" default
  accent) and ``#313131`` (default body text) on a theme that has never been
  customized. The ENC Explorer 2026-06-04 extraction (`encexplorer.json`)
  returned both verbatim alongside trivial grayscale and Gutenberg's
  ``#f5f5f5`` surface.
- A site whose only font signal is ``system-ui`` / ``sans-serif`` / ``serif``
  almost certainly missed a real ``@font-face`` or Google Fonts link in the
  document head. The Susann finding (2026-05-31) is the canonical example.
- Shopify Dawn, Squarespace 7.1, Webflow's default style preset, and Wix's
  Editor X templates each ship a small set of palette + font defaults that
  show up verbatim on un-customized installs. Cycle #3 (2026-06-04) adds
  those four registries so the rubric can flag the broader CMS landscape,
  not just WordPress.

These sets are deliberately lowercase + dash-normalized so callers can match
without re-normalizing on each call. The hex set uses 6-digit ``#rrggbb`` form
with leading ``#``.

Schema: ``known_cms_defaults_v1@1.1`` (additive over @1.0; the @1.0 surface
- ``GUTENBERG_DEFAULT_ACCENTS``, ``TRIVIAL_GRAYSCALE``, ``GENERIC_SANS_FONTS``,
``normalize_hex``, ``normalize_font_stack`` - is unchanged. The @1.1 surface
adds per-CMS palette frozensets, per-CMS font frozensets, the aggregate
``ALL_CMS_DEFAULT_PALETTES`` / ``ALL_CMS_DEFAULT_FONTS`` unions, and
``identify_cms_match`` for human-readable provenance lookup.).
"""
from __future__ import annotations

from typing import Final


SCHEMA_VERSION: Final[str] = "known_cms_defaults_v1@1.1"


# WordPress + Gutenberg out-of-the-box accent colors that almost never survive
# a real brand engagement. ``#007cba`` is the canonical wp-block-button blue;
# ``#006ba1`` is its hover-state shade; ``#cf2e2e`` / ``#ff6900`` / ``#fcb900``
# / ``#7bdcb5`` / ``#00d084`` / ``#8ed1fc`` / ``#0693e3`` / ``#abb8c3`` /
# ``#eb144c`` / ``#f78da7`` / ``#9b51e0`` are the default theme.json palette
# slugs. Hits on any of these in the declared-token output strongly suggest
# the extractor read theme.json without confirming the rendered page uses it.
#
# Provenance: WordPress core ``theme.json`` schema (WP 6.x), default palette
# slugs ``vivid-cyan-blue`` through ``vivid-purple``.
GUTENBERG_DEFAULT_ACCENTS: Final[frozenset[str]] = frozenset({
    "#007cba",
    "#006ba1",
    "#cf2e2e",
    "#ff6900",
    "#fcb900",
    "#7bdcb5",
    "#00d084",
    "#8ed1fc",
    "#0693e3",
    "#abb8c3",
    "#eb144c",
    "#f78da7",
    "#9b51e0",
})


# Shopify Dawn theme (the Online Store 2.0 reference theme, currently
# shipped at v15.x as of 2026-06). Dawn's ``settings_data.json`` defaults
# carry these six palette entries verbatim. A storefront whose extracted
# palette is dominated by these hexes almost certainly never customized
# Dawn's color scheme; the theme editor's first step is overriding them.
#
# Provenance: Shopify Dawn 15.x ``config/settings_data.json`` defaults,
# specifically the ``colors_*`` and ``button_background`` schemes.
SHOPIFY_DAWN_DEFAULT_PALETTE: Final[frozenset[str]] = frozenset({
    "#121212",  # Dawn primary text / button background
    "#ffffff",  # Dawn page background
    "#fbfaf6",  # Dawn secondary background (subtle warm off-white)
    "#34495e",  # Dawn secondary text
    "#eeeeee",  # Dawn border / divider
    "#1773b0",  # Dawn legacy link blue (pre-v10; still surfaces on older installs)
})


# Squarespace 7.1 (the fluid-engine generation, the default editor for all
# new sites since 2020). Default site colors before the user runs the Color
# Palette picker. Squarespace 7.0 templates (Bedford, Brine, etc.) share
# most of these defaults; 7.1's editor narrows them but the same hexes
# appear as the "Basic" preset values.
#
# Provenance: Squarespace 7.1 default site styles + Bedford/Brine 7.0
# template defaults observed in the design-tokens output of un-customized
# trial sites (2026-05 sweep).
SQUARESPACE_DEFAULT_PALETTE: Final[frozenset[str]] = frozenset({
    "#000000",  # Default text
    "#ffffff",  # Default background
    "#f5f5f5",  # Default surface tint
    "#222222",  # Default dark surface
    "#0072e3",  # Default link / button blue (7.0 Bedford carryover)
    "#1a1a1a",  # Default heading on 7.1 Basic palette
})


# Webflow's default style preset (the values present on a freshly-created
# Webflow project before the designer changes any swatch). The ``#3898ec``
# button blue is the most distinctive Webflow signal; it's the colour of
# the default Button class shipped with every new project.
#
# Provenance: Webflow default project template (2026-06 editor),
# specifically the Button, Body, and base style classes.
WEBFLOW_DEFAULT_PALETTE: Final[frozenset[str]] = frozenset({
    "#3898ec",  # Webflow default Button background (the canonical Webflow blue)
    "#333333",  # Webflow default Body text
    "#ffffff",  # Webflow default Body background
    "#f5f5f5",  # Webflow default Section background
    "#dddddd",  # Webflow default Divider
    "#e2e2e2",  # Webflow default Form border
})


# Wix's default editor + Editor X / Studio templates. The newer Studio
# editor (2024+) uses ``#116dff`` as the canonical Wix blue; the legacy
# editor used ``#3899ec`` (one digit off Webflow's by coincidence, not
# shared origin). Both appear in extracted palettes from un-customized
# Wix sites depending on which editor produced the site.
#
# Provenance: Wix Editor X default template + Wix Studio 2024+ default
# theme tokens.
WIX_DEFAULT_PALETTE: Final[frozenset[str]] = frozenset({
    "#116dff",  # Wix Studio default primary
    "#3899ec",  # Wix legacy editor default primary
    "#000000",  # Wix default text
    "#ffffff",  # Wix default background
    "#f5f5f5",  # Wix default surface
    "#fafafa",  # Wix default secondary surface
})


# Aggregate union of every known-CMS default palette plus the original
# Gutenberg set. The confidence rubric consults this single frozenset to
# detect "extraction matches a CMS default" without caring which CMS;
# ``identify_cms_match`` resolves the specific source for the flag string.
ALL_CMS_DEFAULT_PALETTES: Final[frozenset[str]] = (
    GUTENBERG_DEFAULT_ACCENTS
    | SHOPIFY_DAWN_DEFAULT_PALETTE
    | SQUARESPACE_DEFAULT_PALETTE
    | WEBFLOW_DEFAULT_PALETTE
    | WIX_DEFAULT_PALETTE
)


# Per-CMS provenance map. Order matters for ``identify_cms_match``: when a
# hex appears in multiple CMS sets (e.g. ``#ffffff`` is in essentially all
# of them but is also trivial grayscale), we report the FIRST matching CMS
# in this tuple. Gutenberg leads because it's the historical reference and
# also the most unambiguous (``#007cba`` is not a coincidence anywhere
# else).
_CMS_PALETTE_REGISTRY: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    ("WP Gutenberg", GUTENBERG_DEFAULT_ACCENTS),
    ("Shopify Dawn", SHOPIFY_DAWN_DEFAULT_PALETTE),
    ("Squarespace 7.1", SQUARESPACE_DEFAULT_PALETTE),
    ("Webflow", WEBFLOW_DEFAULT_PALETTE),
    ("Wix", WIX_DEFAULT_PALETTE),
)


# Trivial / dummy grayscale values present in essentially every starter theme
# and rarely a deliberate brand choice on their own. We flag when a palette
# is DOMINATED by these (paired with a CMS default), not when one appears
# alongside real brand colors. ``#313131`` is Gutenberg's default body text;
# ``#f5f5f5`` is its default surface tint (also Squarespace + Webflow +
# Wix's default surface).
TRIVIAL_GRAYSCALE: Final[frozenset[str]] = frozenset({
    "#000000",
    "#ffffff",
    "#313131",
    "#333333",
    "#cccccc",
    "#dddddd",
    "#eeeeee",
    "#f5f5f5",
    "#fafafa",
})


# Generic font stacks that signal the extractor never saw a real custom-font
# declaration. Stored lowercase, exact-match against the FULL declared stack
# (not just the primary family) so we catch both "system-ui" and
# "system-ui, -apple-system, sans-serif" variants.
#
# Provenance: Susann finding 2026-05-31 ("font_body": "system-ui, ..."),
# plus encexplorer.json 2026-06-04 ("font_mono": "monospace").
GENERIC_SANS_FONTS: Final[frozenset[str]] = frozenset({
    "system-ui",
    "sans-serif",
    "serif",
    "monospace",
    "arial, sans-serif",
    "helvetica, sans-serif",
    "helvetica neue, sans-serif",
    "times new roman, serif",
    "georgia, serif",
    "system-ui, sans-serif",
    "system-ui, -apple-system, sans-serif",
    "-apple-system, blinkmacsystemfont, sans-serif",
})


# Per-CMS default font stacks. These are the stacks that appear on a stock
# install before the designer picks a real typeface. Stored in the same
# normalized form as ``GENERIC_SANS_FONTS`` (lowercase, single-spaced,
# unquoted).
#
# Provenance: each platform's default theme typography settings as of
# 2026-06.

SHOPIFY_DAWN_DEFAULT_FONTS: Final[frozenset[str]] = frozenset({
    "assistant, sans-serif",       # Dawn's default body + heading family
    "inter, sans-serif",            # Dawn's secondary default (newer installs)
})

SQUARESPACE_DEFAULT_FONTS: Final[frozenset[str]] = frozenset({
    "lato, sans-serif",             # Squarespace 7.0 default body
    "helvetica neue, sans-serif",   # Squarespace 7.0 default heading
    "proxima nova, sans-serif",     # Squarespace 7.1 default heading family
})

WEBFLOW_DEFAULT_FONTS: Final[frozenset[str]] = frozenset({
    "inter, sans-serif",            # Webflow 2026 default Body class
    "arial, helvetica, sans-serif", # Webflow's older default Body stack
})

WIX_DEFAULT_FONTS: Final[frozenset[str]] = frozenset({
    "avenir, sans-serif",                # Wix legacy editor default
    "madefor display, sans-serif",       # Wix Studio default heading
    "madefor text, sans-serif",          # Wix Studio default body
})


# Aggregate of every known-CMS default font stack PLUS the original
# generic-stack set. ``confidence_rubric`` consults this when scoring
# font-specificity so a stock Squarespace ``lato, sans-serif`` reads as
# "generic" just like a bare ``system-ui`` does.
ALL_CMS_DEFAULT_FONTS: Final[frozenset[str]] = (
    GENERIC_SANS_FONTS
    | SHOPIFY_DAWN_DEFAULT_FONTS
    | SQUARESPACE_DEFAULT_FONTS
    | WEBFLOW_DEFAULT_FONTS
    | WIX_DEFAULT_FONTS
)


_CMS_FONT_REGISTRY: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    ("WP Gutenberg / generic", GENERIC_SANS_FONTS),
    ("Shopify Dawn", SHOPIFY_DAWN_DEFAULT_FONTS),
    ("Squarespace 7.1", SQUARESPACE_DEFAULT_FONTS),
    ("Webflow", WEBFLOW_DEFAULT_FONTS),
    ("Wix", WIX_DEFAULT_FONTS),
)


def identify_cms_match(value: str) -> str | None:
    """Return the CMS label whose default-palette set contains ``value``.

    Used by the confidence rubric to render human-readable flag strings
    like ``"matches Shopify Dawn default neutral"``. Returns ``None`` when
    the value matches no registered CMS default palette. Order is the
    ``_CMS_PALETTE_REGISTRY`` order so ambiguous hexes (e.g. ``#ffffff``)
    resolve to the most-historically-canonical CMS first.

    ``value`` MUST already be normalized via ``normalize_hex``; this
    function does not re-normalize because callers typically already hold
    a normalized list and re-normalizing per-call wastes cycles inside
    the rubric's hot path.
    """
    for label, palette in _CMS_PALETTE_REGISTRY:
        if value in palette:
            return label
    return None


def identify_cms_font_match(value: str) -> str | None:
    """Return the CMS label whose default-font set contains ``value``.

    Companion to ``identify_cms_match``. ``value`` MUST already be
    normalized via ``normalize_font_stack``. Returns ``None`` when the
    stack matches no registered default.
    """
    for label, fonts in _CMS_FONT_REGISTRY:
        if value in fonts:
            return label
    return None


def normalize_hex(value: str | None) -> str | None:
    """Return a lowercase 6-digit ``#rrggbb`` hex or ``None`` for unrecognized input.

    Accepts ``#rgb`` shorthand, with or without leading ``#``, mixed case.
    Returns ``None`` for ``None``, empty string, or any value that does not
    parse as a hex color (rgb() / hsl() / named colors are out of scope here
    because the extractor normalizes those to hex before they reach this
    layer; see ``extractor.css_root_parser`` for the upstream normalization).
    """
    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned.startswith("#"):
        cleaned = cleaned[1:]
    if len(cleaned) == 3 and all(ch in "0123456789abcdef" for ch in cleaned):
        cleaned = "".join(ch * 2 for ch in cleaned)
    if len(cleaned) != 6 or not all(ch in "0123456789abcdef" for ch in cleaned):
        return None
    return f"#{cleaned}"


def normalize_font_stack(value: str | None) -> str | None:
    """Return a lowercase, single-spaced, unquoted font-stack string.

    Strips wrapping single and double quotes from each family name, collapses
    runs of whitespace, lowercases for case-insensitive comparison. Returns
    ``None`` for ``None`` or empty input.
    """
    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    families = [family.strip().strip("'").strip('"') for family in cleaned.split(",")]
    families = [family for family in families if family]
    if not families:
        return None
    return ", ".join(families)
