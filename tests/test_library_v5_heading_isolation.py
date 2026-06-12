"""D15 regression guard: alphabet heading classes must own color at class specificity.

Library v5 Phase 1.B - TDD RED for Defect A.

Root cause: the chrome rule `.library-content h2, .library-content h3` in
globals.css paints every brand heading Resemblio navy (``--deep-blue``) at
specificity 0-1-1. The ALPHABET_STYLES rules `.a-h2` / `.a-h3` sit at 0-1-0
(one class), which after ``scope_style_block`` become `.rs-library-page .a-h2`
at 0-2-0 - beating the chrome rule in specificity order. But specificity alone
is not enough: if the 0-2-0 rule does not declare a ``color`` property, the
cascade falls through to the next matching rule, which IS the chrome rule at
0-1-1. The fix (D15) requires both halves:

1. The chrome rule must not set ``color`` (handled in globals.css - web repo).
2. The brand fragment's heading classes must declare ``color: var(--ds-text)``
   so the class-level rule takes ownership regardless of what comes after in
   the global sheet.

Decision reference: D15 in
    projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v5.md

Run command (from code/api/):
    python -m pytest tests/test_library_v5_heading_isolation.py -v
"""

from __future__ import annotations

import re

import pytest


# ---------------------------------------------------------------------------
# Vendored templates module - requires extractor_bridge's sys.path install.
# Importing app.library_indexer triggers that install at module load time.
# ---------------------------------------------------------------------------

from app import library_indexer as _  # noqa: F401 - side-effect: DRL sys.path install
from _scripts import templates as tpl  # noqa: PLC0415 - after sys.path install
from app.library_style_scope import scope_style_block


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule_body(css: str, selector_pattern: str) -> str | None:
    """Return the declaration block (inside braces) for the first rule whose
    selector matches ``selector_pattern``.  Returns ``None`` if not found.

    Handles multi-line rule bodies via DOTALL. Stops at the first ``}``.
    """
    pattern = selector_pattern + r"\s*\{([^}]*)\}"
    m = re.search(pattern, css, re.DOTALL)
    if m is None:
        return None
    return m.group(1)


# ---------------------------------------------------------------------------
# D15 assertions - these FAIL on the current templates.py (RED state)
# ---------------------------------------------------------------------------


def test_a_h2_class_declares_color_v5_d15() -> None:
    """D15: .a-h2 rule in ALPHABET_STYLES must declare color: var(--ds-text).

    Without a color declaration the rule at specificity 0-2-0 (after scoping)
    owns the font/size properties but not the color property, so the cascade
    falls through to the chrome rule which sets --deep-blue. The brand heading
    would paint navy regardless of the brand's own token.
    """
    body = _rule_body(tpl.ALPHABET_STYLES, r"\.a-h2")
    assert body is not None, ".a-h2 rule must exist in ALPHABET_STYLES"
    assert "color:" in body, (
        "D15: .a-h2 must declare 'color: var(--ds-text)' so the class-level "
        "rule owns heading color. Currently the rule has no color property; "
        f"declaration block: {body.strip()!r}"
    )
    assert "var(--ds-text)" in body, (
        "D15: .a-h2 color must be var(--ds-text) (brand text token), "
        f"not a hardcoded value. Declaration block: {body.strip()!r}"
    )


def test_a_h3_class_declares_color_v5_d15() -> None:
    """D15: .a-h3 rule in ALPHABET_STYLES must declare color: var(--ds-text).

    Same rationale as test_a_h2_class_declares_color_v5_d15.
    """
    body = _rule_body(tpl.ALPHABET_STYLES, r"\.a-h3")
    assert body is not None, ".a-h3 rule must exist in ALPHABET_STYLES"
    assert "color:" in body, (
        "D15: .a-h3 must declare 'color: var(--ds-text)'. Currently no color "
        f"property. Declaration block: {body.strip()!r}"
    )
    assert "var(--ds-text)" in body, (
        "D15: .a-h3 color must be var(--ds-text). "
        f"Declaration block: {body.strip()!r}"
    )


def test_scoped_a_h2_rule_wins_over_chrome_rule_v5_d15() -> None:
    """D15: after scope_style_block, .rs-library-page .a-h2 must have color.

    Specificity of .rs-library-page .a-h2 is 0-2-0.
    Specificity of .library-content h3 (the chrome rule) is 0-1-1.
    0-2-0 beats 0-1-1 in all three components (class+class > class+element).
    But the rule must HAVE a color property or specificity is moot.

    This test composes the scoped ALPHABET_STYLES and verifies that the
    resulting `.rs-library-page .a-h2` rule block contains a color declaration.
    """
    scoped = scope_style_block(tpl.ALPHABET_STYLES)
    # After scoping, .a-h2 becomes .rs-library-page .a-h2
    body = _rule_body(scoped, r"\.rs-library-page \.a-h2")
    assert body is not None, (
        ".rs-library-page .a-h2 rule must exist in scoped ALPHABET_STYLES. "
        "Check that scope_style_block correctly prefixes class selectors."
    )
    assert "color:" in body, (
        "D15: scoped .rs-library-page .a-h2 rule (0-2-0) must carry color: var(--ds-text) "
        "to beat the chrome .library-content h3 rule (0-1-1) on the color property. "
        f"Scoped declaration block: {body.strip()!r}"
    )


def test_scoped_a_h3_rule_wins_over_chrome_rule_v5_d15() -> None:
    """D15: after scope_style_block, .rs-library-page .a-h3 must have color."""
    scoped = scope_style_block(tpl.ALPHABET_STYLES)
    body = _rule_body(scoped, r"\.rs-library-page \.a-h3")
    assert body is not None, (
        ".rs-library-page .a-h3 rule must exist in scoped ALPHABET_STYLES."
    )
    assert "color:" in body, (
        "D15: scoped .rs-library-page .a-h3 rule (0-2-0) must carry color: var(--ds-text). "
        f"Scoped declaration block: {body.strip()!r}"
    )
