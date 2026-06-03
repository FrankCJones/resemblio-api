"""Tests for `extractor.button_override.inject_button_override` + the
`apply_button_tokens` seam wrapper.

Hybrid Path B fidelity fix per CTO decision packet
`projects/OptSus Team/cto-reviews/2026-06-02-resemblio-button-fidelity-fix.md`.

Layer 2 of the three-layer TDD shape: take a composed HTML body
fragment (DRL `.b-btn` block included) and assert the override
appends correctly, contains the derived tokens verbatim, is
idempotent across re-runs, and is a no-op when the input has no
`.b-btn` block or the tokens are None.
"""
from __future__ import annotations

from extractor.button_override import (
    OVERRIDE_MARKER,
    apply_button_tokens,
    inject_button_override,
)
from extractor.button_tokens import ButtonTokens


def _apple_tokens() -> ButtonTokens:
    """Return a synthetic ButtonTokens approximating Apple's pill."""
    return ButtonTokens(
        background_color="rgb(0, 113, 227)",
        color="rgb(255, 255, 255)",
        border_radius="980px",
        padding="17px 28px",
        padding_block="17px",
        padding_inline="28px",
        font_family='"SF Pro Text", Helvetica, Arial, sans-serif',
        font_size="17px",
        font_weight="400",
        border_width="0px",
        schema_version=1,
    )


def _drl_buttons_fragment() -> str:
    """Minimal HTML fragment carrying a DRL-style `.b-btn` block.

    Mirrors the shape `_compose_one_page` produces: an `<article>`
    wrapper holding a `<style>` element with the vendored DRL CSS,
    followed by the body markup. We keep only the relevant rules so the
    test is readable; the override regex doesn't care about siblings.
    """
    return (
        '<article class="rs-library-page" data-rs-class="buttons" data-rs-brand="apple">\n'
        "<style>\n"
        ":root { --ds-accent: #0066CC; --ds-bg: #FFFFFF; }\n"
        ".b-btn { display: inline-flex; padding: 10px 16px;\n"
        "         font-size: var(--ds-text-sm); font-weight: 500;\n"
        "         border-radius: var(--ds-radius-sm, 6px);\n"
        "         border: 1px solid transparent; }\n"
        ".b-btn--primary { background: var(--ds-accent); color: var(--ds-bg); }\n"
        "</style>\n"
        '<button class="b-btn b-btn--primary">Buy</button>\n'
        "</article>\n"
    )


# ---------------------------------------------------------------------------
# Injection mechanics.
# ---------------------------------------------------------------------------


def test_injection_appends_after_existing_block() -> None:
    out = inject_button_override(_drl_buttons_fragment(), _apple_tokens())
    # The original block stays untouched.
    assert ".b-btn { display: inline-flex" in out
    # The override block is present.
    assert OVERRIDE_MARKER in out
    # Order: original block comes before the override marker.
    original_idx = out.index(".b-btn { display: inline-flex")
    override_idx = out.index(OVERRIDE_MARKER)
    assert original_idx < override_idx


def test_injection_lands_inside_style_block() -> None:
    """Override should be inserted before the closing `</style>` tag."""
    out = inject_button_override(_drl_buttons_fragment(), _apple_tokens())
    override_idx = out.index(OVERRIDE_MARKER)
    closing_style_idx = out.index("</style>")
    assert override_idx < closing_style_idx


def test_injection_writes_derived_tokens_verbatim() -> None:
    out = inject_button_override(_drl_buttons_fragment(), _apple_tokens())
    assert "border-radius: 980px !important;" in out
    assert "padding: 17px 28px !important;" in out
    assert "font-size: 17px !important;" in out
    assert "font-weight: 400 !important;" in out
    assert "border-width: 0px !important;" in out
    assert "SF Pro Text" in out


def test_injection_is_idempotent() -> None:
    """Re-applying produces a single override block, not two."""
    once = inject_button_override(_drl_buttons_fragment(), _apple_tokens())
    twice = inject_button_override(once, _apple_tokens())
    assert twice.count(OVERRIDE_MARKER) == 1
    # And the rendered tokens are the same single set.
    assert twice.count("border-radius: 980px !important;") == 1


def test_injection_updates_on_re_run_with_new_tokens() -> None:
    """Re-running with different tokens replaces the prior override."""
    once = inject_button_override(_drl_buttons_fragment(), _apple_tokens())
    chiclet = ButtonTokens(
        background_color="rgb(13, 110, 253)",
        color="rgb(255, 255, 255)",
        border_radius="6px",
        padding="10px 16px",
        padding_block="10px",
        padding_inline="16px",
        font_family="Inter, sans-serif",
        font_size="14px",
        font_weight="500",
        border_width="1px",
        schema_version=1,
    )
    twice = inject_button_override(once, chiclet)
    assert twice.count(OVERRIDE_MARKER) == 1
    assert "border-radius: 6px !important;" in twice
    assert "border-radius: 980px !important;" not in twice


# ---------------------------------------------------------------------------
# No-op contract: missing block, missing closing style tag, empty tokens.
# ---------------------------------------------------------------------------


def test_no_b_btn_block_returns_input_unchanged() -> None:
    typography_fragment = (
        '<article class="rs-library-page" data-rs-class="typography">\n'
        "<style>.t-display { font-size: 64px; }</style>\n"
        '<h1 class="t-display">Heading</h1>\n'
        "</article>\n"
    )
    assert (
        inject_button_override(typography_fragment, _apple_tokens())
        == typography_fragment
    )


def test_apply_button_tokens_none_is_noop() -> None:
    fragment = _drl_buttons_fragment()
    assert apply_button_tokens(fragment, None) == fragment


def test_apply_button_tokens_with_tokens_matches_inject() -> None:
    fragment = _drl_buttons_fragment()
    tokens = _apple_tokens()
    assert apply_button_tokens(fragment, tokens) == inject_button_override(
        fragment, tokens
    )


def test_empty_token_strings_are_skipped() -> None:
    """An empty `font_family` does not emit a `font-family: ;` line."""
    sparse: ButtonTokens = ButtonTokens(
        background_color="",
        color="",
        border_radius="980px",
        padding="",
        padding_block="",
        padding_inline="",
        font_family="",
        font_size="",
        font_weight="",
        border_width="",
        schema_version=1,
    )
    out = inject_button_override(_drl_buttons_fragment(), sparse)
    assert "border-radius: 980px !important;" in out
    assert "font-family:" not in out.split(OVERRIDE_MARKER, 1)[1].split("</style>", 1)[0]


def test_injection_without_closing_style_tag_appends_to_end() -> None:
    """Defensive: malformed fragment still receives the override."""
    malformed = ".b-btn { padding: 10px 16px; border-radius: 6px; }"
    out = inject_button_override(malformed, _apple_tokens())
    assert OVERRIDE_MARKER in out
    assert "border-radius: 980px !important;" in out
