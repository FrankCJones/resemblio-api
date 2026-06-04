"""Inject a per-brand ``.b-btn`` override into composed DRL HTML.

Hybrid Path B fidelity fix per CTO decision packet
`projects/OptSus Team/cto-reviews/2026-06-02-resemblio-button-fidelity-fix.md`.

The vendored DRL templates render a single generic `.b-btn` block for
every brand (6px corners, 10/16 padding, 14px / 500). The override
appends a second `.b-btn { ... !important }` block sourced from the
brand's R3.1-derived `ButtonTokens` so the cascade resolves to the
real shape (Apple: 980px pill, 17px 28px padding, 17px SF Pro 400).

The DRL block is left untouched. The override is appended below it in
source order so the cascade picks the override naturally, and the
`!important` belt-and-suspenders guards against later inline rules.
Both signals are documented as the temporary-override smell in
`projects/Resemblio/STATUS.md`; the override is retired the day DRL
ships the `--ds-button-*` contract on Path A.

Throwaway: NO. Quality floor applies. Tests at
`tests/test_button_override.py` exercise injection, idempotency,
no-op semantics, and the post-compose seam contract.
"""
from __future__ import annotations

import re

from extractor.button_tokens import ButtonTokens

SCHEMA_VERSION = 2
"""Bumped when the override block's CSS shape changes.

v2 (2026-06-04, P11-A): added a sibling-selector rule that propagates
the brand's ``border-radius`` to the other four button-shaped composed
class prefixes (``.h-btn`` hero CTA, ``.n-btn`` nav sign-up,
``.cta__btn`` cta-block, ``.a-btn`` alphabet). The 13 sibling library
categories previously fell through to the DRL chiclet default
(``var(--ds-radius-sm, 6px)``); the v2 override carries shape fidelity
to all of them. Padding and typography stay scoped to ``.b-btn`` only
because those values are calibrated to button-text glyphs and would
break composed-component layouts if propagated.
"""

# Stable marker pair that lets us detect a prior override in the same
# body and stay idempotent across re-runs. The markers are CSS comments
# so they have no visual effect and survive re-render through standard
# CSS tooling. v2 introduced ``OVERRIDE_END_MARKER`` so the idempotent
# strip can span the two rules the v2 override emits without relying on
# brittle "find the next ``}``" heuristics.
OVERRIDE_MARKER = "/* resemblio-button-override v1 */"
OVERRIDE_END_MARKER = "/* resemblio-button-override end */"

# Class prefixes used by every button-shaped composed component the DRL
# templates emit. ``.b-btn`` is the buttons-category surface that already
# carries full token fidelity (radius + padding + type + border); the
# remaining four are siblings the brand's border-radius is propagated to.
# Sourced by reading
# ``projects/Resemblio/code/api/_vendored/drl/drl/_scripts/templates.py``
# on 2026-06-04 and confirming each prefix's ``{`` selector is present.
_BUTTON_SHAPED_PREFIXES: tuple[str, ...] = (
    ".b-btn",
    ".h-btn",
    ".n-btn",
    ".cta__btn",
    ".a-btn",
)

# Regex used to detect "is there ANY of the button-shaped class blocks
# in this HTML fragment at all?". The DRL templates emit each block
# inline inside the fragment's `<style>` element; the indexer wraps the
# fragment in its own `<article>` (see `_compose_one_page`). We do NOT
# attempt to rewrite existing blocks in place - that is fragile against
# template whitespace and CSS edits; instead we append below them. The
# pattern matches any of the five prefixes followed by an opening brace,
# possibly with intermediate modifier or pseudo-class characters
# (e.g. ``.b-btn--primary``, ``.h-btn:hover``) so we trigger on any of
# the DRL rule families.
_BUTTON_BLOCK_RE = re.compile(
    r"\.(?:b-btn|h-btn|n-btn|cta__btn|a-btn)\b[^{]*\{",
    re.IGNORECASE,
)

# Closing `</style>` is where we inject the override block - just before
# the closing tag so the override participates in the same style scope
# the DRL block opened. Case-insensitive to match `</style>` /
# `</STYLE>` variants the templates may emit.
_CLOSING_STYLE_RE = re.compile(r"</style>", re.IGNORECASE)


def _format_override_css(tokens: ButtonTokens) -> str:
    """Render the override block(s) from ``tokens``.

    Emits two CSS rules:

    1. ``.b-btn { ... !important }`` carrying every property the
       diagnosis identified as a fidelity miss on the buttons-category
       surface: border-radius, padding, font-family, font-size,
       font-weight, border-width. Each carries ``!important`` because
       the temporary contract requires the override to win over any
       sibling DRL rule.
    2. ``.b-btn, .h-btn, .n-btn, .cta__btn, .a-btn { border-radius: ... !important }``
       propagates only the brand's corner-radius to the four sibling
       button-shaped composed-component prefixes (P11-A 2026-06-04).
       Padding and typography stay scoped to ``.b-btn`` because those
       values are calibrated to button-text glyphs; applying them to
       hero CTAs / nav sign-ups / cta-block / alphabet buttons would
       break the composed-component layouts at sizes the buttons-
       category render never exercises. Shape is brand identity;
       padding-and-type is button-text-specific. Confirmed against the
       2026-06-03 library-category audit memo.

    Empty token strings are skipped (no ``font-family: ;`` lines). When
    ``border_radius`` is empty the sibling rule is omitted entirely so
    we never emit a no-op selector list.
    """
    lines: list[str] = [f"{OVERRIDE_MARKER}", ".b-btn {"]
    # Order chosen for human review: shape first, then padding, then type.
    if tokens["border_radius"]:
        lines.append(f"  border-radius: {tokens['border_radius']} !important;")
    if tokens["padding"]:
        lines.append(f"  padding: {tokens['padding']} !important;")
    if tokens["font_family"]:
        lines.append(f"  font-family: {tokens['font_family']} !important;")
    if tokens["font_size"]:
        lines.append(f"  font-size: {tokens['font_size']} !important;")
    if tokens["font_weight"]:
        lines.append(f"  font-weight: {tokens['font_weight']} !important;")
    if tokens["border_width"]:
        lines.append(f"  border-width: {tokens['border_width']} !important;")
    lines.append("}")
    # Sibling-shape propagation: only emit when we have a radius to
    # carry, otherwise the rule is a no-op and pollutes the cascade.
    if tokens["border_radius"]:
        sibling_selector = ", ".join(_BUTTON_SHAPED_PREFIXES)
        lines.append(f"{sibling_selector} {{")
        lines.append(
            f"  border-radius: {tokens['border_radius']} !important;"
        )
        lines.append("}")
    lines.append(OVERRIDE_END_MARKER)
    return "\n".join(lines)


def _strip_existing_override(html: str) -> str:
    """Remove any prior override block to keep injection idempotent.

    v2 strip strategy: locate ``OVERRIDE_MARKER`` and the matching
    ``OVERRIDE_END_MARKER`` and remove the span between them inclusive.
    This handles the v2 two-rule emission (``.b-btn`` block + sibling
    border-radius block) without relying on "find the next ``}``" which
    only spans one rule.

    Backward compatibility: a v1 override (single ``.b-btn`` block, no
    end marker) is detected by the absence of ``OVERRIDE_END_MARKER``
    after the start marker, and we fall back to the v1 "strip to next
    ``}``" behavior. This keeps already-injected v1 fragments
    re-injectable into v2 without duplication.
    """
    idx = html.find(OVERRIDE_MARKER)
    if idx < 0:
        return html
    end_marker_idx = html.find(OVERRIDE_END_MARKER, idx)
    if end_marker_idx >= 0:
        end = end_marker_idx + len(OVERRIDE_END_MARKER)
    else:
        # v1 fragment: strip through the closing brace of the single
        # rule that follows the start marker.
        end = html.find("}", idx)
        if end < 0:
            # Malformed prior override; safest action is to leave it alone.
            return html
        end += 1
    # Also strip an immediately-leading newline so we don't accumulate
    # blank lines on repeated runs.
    start = idx
    if start > 0 and html[start - 1] == "\n":
        start -= 1
    return html[:start] + html[end:]


def inject_button_override(html: str, tokens: ButtonTokens) -> str:
    """Append button override block(s) sourced from ``tokens``.

    Behavior:

    - If ``html`` contains none of the button-shaped class blocks
      (``.b-btn``, ``.h-btn``, ``.n-btn``, ``.cta__btn``, ``.a-btn``),
      returns the input unchanged (no-op for non-button pages such as
      ``/typography/`` or ``/colors/``).
    - If ``html`` already carries an override (detected via
      ``OVERRIDE_MARKER``), the prior override is stripped and the new
      one appended - idempotent across re-runs, and compatible with v1
      overrides via the strip fallback (see ``_strip_existing_override``).
    - The override is injected just before the first ``</style>`` tag
      so it participates in the same scoped style block the DRL template
      opened. If no ``</style>`` is present (unexpected for the composed
      fragment), the override is appended to the end of the document.

    Never raises on malformed input; the worst case is a no-op return.
    """
    if not _BUTTON_BLOCK_RE.search(html):
        return html

    cleaned = _strip_existing_override(html)
    override_css = _format_override_css(tokens)
    # Defensive guard: the emitted block must end with the v2 end marker
    # (or, pre-v2 fragments emitted by older code, a ``}`` closing brace).
    # If neither sentinel is present the emitter produced garbage; refuse
    # to inject rather than corrupt the cascade.
    stripped = override_css.strip()
    if not (
        stripped.endswith(OVERRIDE_END_MARKER) or stripped.endswith("}")
    ):
        return cleaned

    injection = f"\n{override_css}\n"

    match = _CLOSING_STYLE_RE.search(cleaned)
    if match is None:
        return cleaned + injection
    insert_at = match.start()
    return cleaned[:insert_at] + injection + cleaned[insert_at:]


def apply_button_tokens(html: str, tokens: ButtonTokens | None) -> str:
    """Public seam entry point used by the library indexer.

    Convenience wrapper that handles the ``tokens is None`` case
    explicitly so the call site at the post-compose seam stays a
    single line. When tokens are missing (no R3.1 snapshot for the
    brand yet), the function is a no-op and the DRL default ships.
    """
    if tokens is None:
        return html
    return inject_button_override(html, tokens)
