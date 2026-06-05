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

These sets are deliberately lowercase + dash-normalized so callers can match
without re-normalizing on each call. The hex set uses 6-digit ``#rrggbb`` form
with leading ``#``.

Schema: ``known_cms_defaults_v1``.
"""
from __future__ import annotations

from typing import Final


SCHEMA_VERSION: Final[str] = "known_cms_defaults_v1@1.0"


# WordPress + Gutenberg out-of-the-box accent colors that almost never survive
# a real brand engagement. ``#007cba`` is the canonical wp-block-button blue;
# ``#006ba1`` is its hover-state shade; ``#cf2e2e`` / ``#ff6900`` / ``#fcb900``
# / ``#7bdcb5`` / ``#00d084`` / ``#8ed1fc`` / ``#0693e3`` / ``#abb8c3`` /
# ``#eb144c`` / ``#f78da7`` / ``#9b51e0`` are the default theme.json palette
# slugs. Hits on any of these in the declared-token output strongly suggest
# the extractor read theme.json without confirming the rendered page uses it.
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


# Trivial / dummy grayscale values present in essentially every starter theme
# and rarely a deliberate brand choice on their own. We flag when a palette
# is DOMINATED by these (paired with a Gutenberg accent), not when one
# appears alongside real brand colors. ``#313131`` is Gutenberg's default
# body text; ``#f5f5f5`` is its default surface tint.
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
