"""Static scan: token-bound declarations in templates.py must reference ``var(--ds-*)``.

Path C Phase 2 (per CTO sign-off
``projects/OptSus Team/cto-reviews/2026-06-03-resemblio-path-c-phase2-contract-signoff.md``):
every visual decision in the vendored DRL templates is supposed to flow
through ``--ds-*`` slots so the per-brand ``:root`` block can override it.

This test scans the ``*_STYLES`` string constants in
``_vendored/drl/drl/_scripts/templates.py`` and asserts that every
declaration in the watched-property set either uses ``var(--ds-...)`` or
is in the allowlist (CSS-reset values that intentionally stay literal,
e.g. ``box-sizing: border-box``).

This is the regression guard: a future template edit that adds
``border-radius: 8px`` instead of ``border-radius: var(--ds-radius-md, 8px)``
fails the test immediately.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


TEMPLATES_PATH = (
    Path(__file__).resolve().parents[1]
    / "_vendored"
    / "drl"
    / "drl"
    / "_scripts"
    / "templates.py"
)

# Properties whose values are universally token-bound by the
# BRAND_TOKEN_CONTRACT - every declaration must use var(--ds-*) or be
# in the allowlist. ``padding`` and ``margin`` are intentionally OUT
# because the contract only covers component-level paddings (button,
# card, badge) - structural-rhythm paddings (``padding: 28px 0`` inside
# a row container) stay literal by design (per CTO packet Q5: no
# per-element rhythm slots).
TOKEN_BOUND_PROPERTIES = {
    "border-radius",
    "font-weight",
    "font-family",
    "border-width",
}

# Declarations that intentionally stay literal. CSS-reset values the
# contract has no slot for, or deliberate emphasis overrides (e.g.
# the recommended-tier border-width bump in PRICING_TABLE_STYLES).
ALLOWLIST_VALUES = {
    "0",
    "0 auto",
    "0 0 0 20px",
    "border-box",
    "none",
    "transparent",
    "currentColor",
    "inherit",
    "initial",
    "unset",
    # The recommended-tier pricing card intentionally bumps its border
    # to 2px; this is a per-template emphasis override, not a token
    # slot the contract owns.
    "2px",
    # Underline weights stay literal in DRL byline + footer rules.
    "italic",
    "normal",
    "bold",
}


_DECLARATION_RE = re.compile(
    r"(?P<prop>[a-z-]+)\s*:\s*(?P<value>[^;{}\"\n]+?)\s*;",
)

_STYLES_BLOCK_RE = re.compile(
    r"^(?P<name>[A-Z_]+_STYLES)\s*=\s*\"\"\"\\\n(?P<body>.*?)\n\"\"\"",
    re.DOTALL | re.MULTILINE,
)


def _extract_styles_blocks() -> dict[str, str]:
    """Return ``{constant_name: body}`` for every ``*_STYLES`` triple-quoted block."""
    source = TEMPLATES_PATH.read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    for match in _STYLES_BLOCK_RE.finditer(source):
        blocks[match.group("name")] = match.group("body")
    return blocks


def _bare_literal_offenders(body: str) -> list[tuple[str, str]]:
    """Return list of ``(property, value)`` declarations that violate the token-bound rule."""
    offenders: list[tuple[str, str]] = []
    for match in _DECLARATION_RE.finditer(body):
        prop = match.group("prop").lower()
        value = match.group("value").strip()
        if prop not in TOKEN_BOUND_PROPERTIES:
            continue
        if "var(--ds-" in value:
            continue
        if value in ALLOWLIST_VALUES:
            continue
        offenders.append((prop, value))
    return offenders


def test_templates_file_is_present() -> None:
    """Sanity guard: the vendored DRL templates.py exists at the expected path."""
    assert TEMPLATES_PATH.is_file(), f"templates.py missing: {TEMPLATES_PATH}"


def test_styles_blocks_are_discoverable() -> None:
    """The static-scan regex must find at least 10 ``*_STYLES`` blocks (DRL ships ~14)."""
    blocks = _extract_styles_blocks()
    assert len(blocks) >= 10, f"expected >=10 *_STYLES blocks, found {len(blocks)}"


@pytest.mark.parametrize(
    "block_name",
    sorted(_extract_styles_blocks().keys()),
)
def test_token_bound_declarations_use_var_or_allowlist(block_name: str) -> None:
    """Each ``*_STYLES`` block: token-bound declarations must use var() or sit in the allowlist."""
    blocks = _extract_styles_blocks()
    body = blocks[block_name]
    offenders = _bare_literal_offenders(body)
    assert offenders == [], (
        f"{block_name}: {len(offenders)} bare-literal declarations "
        f"(first 5): {offenders[:5]}"
    )
