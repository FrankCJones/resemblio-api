"""D18 regression guard: alphabet specimen .a-btn must bind to var(--ds-accent).

Library v5 Phase 1.C - TDD RED for Defect B.

Root cause: ALPHABET_STYLES in the vendored DRL templates carries:
    .a-btn { ... background: var(--ds-text); color: var(--ds-bg); ... }

``var(--ds-text)`` is the brand's primary text color - typically near-black
for dark-text brands like Apple (``--ds-text: #1d1d1f``) and near-white for
dark-background brands. Using it as a button background makes the button
visually broken (near-black on white body), misrepresenting the brand's actual
accent/CTA color.

Every other button template in the file already uses ``var(--ds-accent)``:
    .h-btn--primary, .n-btn--primary, .cta__btn--primary, .l-btn--primary,
    .b-btn--primary all bind to var(--ds-accent).

Decision reference: D18 in
    projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v5.md

Run command (from code/api/):
    python -m pytest tests/test_library_v5_button_fill.py -v
"""

from __future__ import annotations

import re

# Trigger the DRL sys.path install so _scripts.templates is importable.
from app import library_indexer as _  # noqa: F401 - side-effect only
from _scripts import templates as tpl  # noqa: PLC0415 - after sys.path install


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule_body(css: str, selector_pattern: str) -> str | None:
    """Return the declaration block for the first rule matching selector_pattern."""
    m = re.search(selector_pattern + r"\s*\{([^}]*)\}", css, re.DOTALL)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# D18 assertions - FAIL on current templates.py (RED state)
# ---------------------------------------------------------------------------


def test_a_btn_uses_ds_accent_background_v5_d18() -> None:
    """D18: .a-btn background must be var(--ds-accent), not var(--ds-text).

    The brand accent is already injected into :root as --ds-accent from the
    brand's real DTCG token set. Binding .a-btn to --ds-text makes the button
    paint the primary text color (near-black on most brands) instead of the
    brand's primary action color.
    """
    body = _rule_body(tpl.ALPHABET_STYLES, r"\.a-btn")
    assert body is not None, ".a-btn rule must exist in ALPHABET_STYLES"
    assert "var(--ds-accent)" in body, (
        "D18: .a-btn background must be var(--ds-accent). "
        f"Current declaration: {body.strip()!r}"
    )


def test_a_btn_does_not_use_ds_text_as_background_v5_d18() -> None:
    """D18: .a-btn must NOT use var(--ds-text) as background.

    var(--ds-text) is for readable text - using it as a button fill makes the
    button indistinguishable from plain text or renders as near-black on most
    brands, losing the brand's actual CTA color.
    """
    body = _rule_body(tpl.ALPHABET_STYLES, r"\.a-btn")
    assert body is not None, ".a-btn rule must exist in ALPHABET_STYLES"
    assert "background: var(--ds-text)" not in body, (
        "D18: .a-btn background is var(--ds-text); this paints the button "
        "the primary text color instead of the brand accent. "
        f"Declaration: {body.strip()!r}"
    )


def test_a_btn_legibility_color_is_ds_bg_v5_d18() -> None:
    """D18: .a-btn text color must be var(--ds-bg) for legibility on accent fill."""
    body = _rule_body(tpl.ALPHABET_STYLES, r"\.a-btn")
    assert body is not None, ".a-btn rule must exist in ALPHABET_STYLES"
    assert "color: var(--ds-bg)" in body, (
        "D18: .a-btn color must be var(--ds-bg) for legibility when the "
        f"background is var(--ds-accent). Declaration: {body.strip()!r}"
    )


def test_other_primary_buttons_use_ds_accent_for_consistency() -> None:
    """Regression guard: all other primary-button classes still bind to --ds-accent.

    Verifies that the D18 fix does not accidentally widen or narrow the
    set of button classes using var(--ds-accent). The sibling classes are the
    ground truth that .a-btn should match.
    """
    sibling_classes = [
        r"\.h-btn--primary",
        r"\.n-btn--primary",
        r"\.cta__btn--primary",
        r"\.l-btn--primary",
    ]
    all_styles = "".join([
        tpl.HERO_STYLES,
        tpl.NAV_STYLES,
        tpl.CTA_BLOCK_STYLES,
        tpl.LIBRARY_STYLES,
    ])
    for pat in sibling_classes:
        body = _rule_body(all_styles, pat)
        assert body is not None, f"{pat} rule not found in its template style block"
        assert "var(--ds-accent)" in body, (
            f"Regression: {pat} no longer uses var(--ds-accent); "
            "this is the anchor class the .a-btn fix must match"
        )
