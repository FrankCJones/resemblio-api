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

SCHEMA_VERSION = 1
"""Bumped when the override block's CSS shape changes."""

# Stable marker that lets us detect a prior override in the same body and
# stay idempotent across re-runs. The marker is a CSS comment so it has
# no visual effect and survives re-render through standard CSS tooling.
OVERRIDE_MARKER = "/* resemblio-button-override v1 */"

# Regex used to detect "is there a `.b-btn { ... }` block in this HTML
# fragment at all?". The DRL templates emit the block inline inside the
# fragment's `<style>` element; the indexer wraps the fragment in its own
# `<article>` (see `_compose_one_page`). We do NOT attempt to rewrite the
# existing block in-place because that is fragile against template
# whitespace and CSS edits; instead we append below it.
_B_BTN_BLOCK_RE = re.compile(r"\.b-btn\s*\{", re.IGNORECASE)

# Closing `</style>` is where we inject the override block - just before
# the closing tag so the override participates in the same style scope
# the DRL block opened. Case-insensitive to match `</style>` /
# `</STYLE>` variants the templates may emit.
_CLOSING_STYLE_RE = re.compile(r"</style>", re.IGNORECASE)


def _format_override_css(tokens: ButtonTokens) -> str:
    """Render the override `.b-btn { ... !important }` block from tokens.

    Emits every property the diagnosis identified as a fidelity miss:
    border-radius, padding, font-family, font-size, font-weight, and the
    border width. Each carries `!important` because the temporary
    contract requires the override to win over any sibling DRL rule.

    Empty tokens are skipped (no `font-family: ;` lines). This keeps the
    output minimal when the source page only reports a subset of values.
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
    return "\n".join(lines)


def _strip_existing_override(html: str) -> str:
    """Remove any prior override block to keep injection idempotent.

    The override block starts at ``OVERRIDE_MARKER`` and runs through
    the next ``}`` (it's a single rule). If the marker isn't present we
    return the input unchanged.
    """
    idx = html.find(OVERRIDE_MARKER)
    if idx < 0:
        return html
    end = html.find("}", idx)
    if end < 0:
        # Malformed prior override; safest action is to leave it alone.
        return html
    # Also strip an immediately-leading newline so we don't accumulate
    # blank lines on repeated runs.
    start = idx
    if start > 0 and html[start - 1] == "\n":
        start -= 1
    return html[:start] + html[end + 1 :]


def inject_button_override(html: str, tokens: ButtonTokens) -> str:
    """Append a `.b-btn` override block sourced from ``tokens``.

    Behavior:

    - If ``html`` contains no `.b-btn { ... }` block at all, returns the
      input unchanged (no-op for non-buttons pages).
    - If ``html`` already carries an override (detected via
      ``OVERRIDE_MARKER``), the prior override is stripped and the new
      one appended - idempotent across re-runs.
    - The override is injected just before the first ``</style>`` tag
      so it participates in the same scoped style block the DRL template
      opened. If no `</style>` is present (unexpected for the composed
      fragment), the override is appended to the end of the document.

    Never raises on malformed input; the worst case is a no-op return.
    """
    if not _B_BTN_BLOCK_RE.search(html):
        return html

    cleaned = _strip_existing_override(html)
    override_css = _format_override_css(tokens)
    if not override_css.strip().endswith("}"):  # defensive guard
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
