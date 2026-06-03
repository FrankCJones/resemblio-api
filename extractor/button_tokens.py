"""Derive button-shape tokens from an R3.1 computed-style report.

Hybrid Path B fidelity fix per CTO decision packet
`projects/OptSus Team/cto-reviews/2026-06-02-resemblio-button-fidelity-fix.md`.

Background
----------
The DRL `.b-btn` template (vendored at
`code/api/_vendored/drl/drl/_scripts/templates.py:823-841`) renders a
single generic button shape for every brand: 6px corners, 10/16 padding,
14px / 500 weight. Apple's pill ends up as a Bootstrap chiclet. The
Path A fix is an upstream change to DRL's TOKEN_CONTRACT; Path B is a
Resemblio-side override that consumes brand-specific button tokens
derived from R3.1's computed-style capture and rewrites the `.b-btn`
block at compose time.

This module is the pure-data derivation half of Path B. It reads the
`cta` slot (selector ``button, .cta, [role=button]``) from a
``ComputedStyleReport`` and returns a typed `ButtonTokens` dict. The
caller pairs the result with `button_override.inject_button_override`
to rewrite the composed HTML body.

Graceful degradation: returns `None` when the report is unavailable,
errored, has no `cta` slot, or carries no useful properties. The
override layer treats `None` as "leave the DRL default in place" so
existing brands without an R3.1 snapshot continue rendering today's
output untouched.

Throwaway: NO. Quality floor applies. Tests at
`tests/test_button_tokens.py` exercise the pure-data derivation.
"""
from __future__ import annotations

from typing import TypedDict

from extractor.computed_styles import ComputedStyleReport

SCHEMA_VERSION = 1
"""Bumped when the ``ButtonTokens`` shape changes."""

CTA_SLOT = "cta"
"""Slot name in ``ComputedStyleReport.signals`` carrying button data."""

# The seven button-shape slots Resemblio writes into its override and
# (per the CTO packet) intends to upstream as the DRL ``--ds-button-*``
# contract on Path A.
BUTTON_TOKEN_KEYS: tuple[str, ...] = (
    "--ds-button-radius",
    "--ds-button-padding-block",
    "--ds-button-padding-inline",
    "--ds-button-font-size",
    "--ds-button-font-weight",
    "--ds-button-font-family",
    "--ds-button-border-width",
)
"""Stable contract: the CSS custom-property names the override emits."""

# Default border width when the browser reports "none" / no border on
# the primary CTA. Apple's primary button has no visible border; we keep
# the default as `0px` so the override does not synthesize a hairline.
DEFAULT_BORDER_WIDTH = "0px"


class ButtonTokens(TypedDict):
    """Derived shape tokens for a brand's primary CTA.

    Fields:
    - background_color: e.g. ``"#0071e3"`` (Apple blue).
    - color: foreground / label color, e.g. ``"#ffffff"``.
    - border_radius: as the browser reports it; pill values come back
      as a large px (Apple: ``"980px"``). The override block writes this
      verbatim so the visual signature is preserved.
    - padding: full padding shorthand (e.g. ``"17px 28px"``). The
      override writes the shorthand directly; the indexer does not need
      to split block / inline here because CSS handles the shorthand.
    - padding_block / padding_inline: convenience-split values for
      consumers that prefer logical-property output (DTCG extension
      under ``$extensions.resemblio.button.*`` per CTO packet section
      "Integration seam"). May be empty strings if the input padding
      could not be parsed.
    - font_family: as the browser reports it.
    - font_size: e.g. ``"17px"``.
    - font_weight: numeric weight as a string (the browser reports
      ``"400"`` not ``"normal"`` for explicit weights).
    - border_width: parsed from the ``border`` shorthand; ``"0px"``
      when no visible border.
    - schema_version: matches ``SCHEMA_VERSION``.
    """

    background_color: str
    color: str
    border_radius: str
    padding: str
    padding_block: str
    padding_inline: str
    font_family: str
    font_size: str
    font_weight: str
    border_width: str
    schema_version: int


def _find_cta_signal(report: ComputedStyleReport) -> dict[str, str] | None:
    """Return the ``cta`` slot's properties dict, or None if missing."""
    if report.get("status") != "ok":
        return None
    for signal in report.get("signals") or ():
        if signal.get("slot") == CTA_SLOT:
            props = signal.get("properties") or {}
            if props:
                return dict(props)
    return None


def _split_padding(padding: str) -> tuple[str, str]:
    """Split a CSS ``padding`` shorthand into (block, inline) px strings.

    The browser normalizes to the 1-, 2-, 3-, or 4-value form. We collapse
    the 1/2/3/4-value variants into the (block, inline) pair the override
    advertises. Returns ``("", "")`` when the input can't be parsed; the
    caller falls back to the unsplit ``padding`` shorthand in that case.
    """
    parts = padding.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 3:
        # top / inline / bottom -> use top for block
        return parts[0], parts[1]
    # 4-value form: top right bottom left -> use top for block, right for inline
    return parts[0], parts[1]


def _parse_border_width(border: str) -> str:
    """Pull the width token out of a CSS ``border`` shorthand.

    The shorthand reports as e.g. ``"1px solid rgb(0, 0, 0)"`` or
    ``"0px none rgb(0, 0, 0)"``. We return the first whitespace-delimited
    token if it ends in a unit; otherwise ``DEFAULT_BORDER_WIDTH``. When
    style is ``none`` we also collapse to ``"0px"`` because the visible
    border is zero regardless of the width token.
    """
    if not border:
        return DEFAULT_BORDER_WIDTH
    parts = border.split()
    if not parts:
        return DEFAULT_BORDER_WIDTH
    if "none" in parts:
        return DEFAULT_BORDER_WIDTH
    width = parts[0]
    # crude unit check; the browser always reports a unit for the width.
    if width.endswith(("px", "em", "rem", "%")):
        return width
    return DEFAULT_BORDER_WIDTH


def derive_button_tokens(report: ComputedStyleReport) -> ButtonTokens | None:
    """Derive a ``ButtonTokens`` dict from an R3.1 computed-style report.

    Returns ``None`` when:
    - the report status is not ``ok``, or
    - the report has no ``cta`` slot, or
    - the ``cta`` slot has no useful properties.

    Never raises on malformed input; the override layer treats ``None``
    as "no override, keep DRL default" and the page continues to render.
    """
    props = _find_cta_signal(report)
    if props is None:
        return None

    padding = props.get("padding", "").strip()
    padding_block, padding_inline = _split_padding(padding) if padding else ("", "")

    return ButtonTokens(
        background_color=props.get("background-color", "").strip(),
        color=props.get("color", "").strip(),
        border_radius=props.get("border-radius", "").strip(),
        padding=padding,
        padding_block=padding_block,
        padding_inline=padding_inline,
        font_family=props.get("font-family", "").strip(),
        font_size=props.get("font-size", "").strip(),
        font_weight=props.get("font-weight", "").strip(),
        border_width=_parse_border_width(props.get("border", "")),
        schema_version=SCHEMA_VERSION,
    )
