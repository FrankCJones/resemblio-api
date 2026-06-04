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
    OVERRIDE_END_MARKER,
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
    """Re-applying produces a single override block span, not two.

    v2 emits two rules per inject (``.b-btn`` block + sibling rule), so
    the radius literal appears twice per inject; the idempotency
    assertion is that the COUNT does not grow across reruns - one
    inject = two radius emissions; two injects = still two emissions.
    """
    once = inject_button_override(_drl_buttons_fragment(), _apple_tokens())
    once_radius_count = once.count("border-radius: 980px !important;")
    twice = inject_button_override(once, _apple_tokens())
    assert twice.count(OVERRIDE_MARKER) == 1
    assert twice.count(OVERRIDE_END_MARKER) == 1
    # Re-injection does not grow the radius emission count.
    assert twice.count("border-radius: 980px !important;") == once_radius_count


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


# ---------------------------------------------------------------------------
# P11-A (2026-06-04): sibling button-shaped-prefix propagation.
#
# The v2 override propagates the brand's ``border-radius`` to four sibling
# composed-component prefixes (``.h-btn`` hero, ``.n-btn`` nav,
# ``.cta__btn`` cta-block, ``.a-btn`` alphabet) so the 13 non-buttons
# library categories pick up brand shape fidelity instead of falling
# through to the DRL chiclet default ``var(--ds-radius-sm, 6px)``.
# ---------------------------------------------------------------------------


def _stripe_tokens() -> ButtonTokens:
    """Stripe's subtle 4px chiclet (per 2026-06-03 audit memo prediction)."""
    return ButtonTokens(
        background_color="rgb(99, 91, 255)",
        color="rgb(255, 255, 255)",
        border_radius="4px",
        padding="8px 16px",
        padding_block="8px",
        padding_inline="16px",
        font_family='"Sohne", "Helvetica Neue", sans-serif',
        font_size="14px",
        font_weight="500",
        border_width="0px",
        schema_version=1,
    )


def _figma_tokens() -> ButtonTokens:
    """Figma's sharp 0px corner (per 2026-06-03 audit memo prediction)."""
    return ButtonTokens(
        background_color="rgb(15, 15, 15)",
        color="rgb(255, 255, 255)",
        border_radius="0",
        padding="6px 12px",
        padding_block="6px",
        padding_inline="12px",
        font_family='"Inter", sans-serif',
        font_size="13px",
        font_weight="500",
        border_width="0px",
        schema_version=1,
    )


def _hero_fragment_no_b_btn() -> str:
    """Hero category fragment carrying only ``.h-btn`` (no ``.b-btn``).

    Mirrors what ``_compose_one_page`` emits for the ``/hero/`` category:
    the hero CTA button is styled by ``.h-btn`` only; the buttons-category
    ``.b-btn`` block is not present on this page.
    """
    return (
        '<article class="rs-library-page" data-rs-class="hero" data-rs-brand="stripe">\n'
        "<style>\n"
        ":root { --ds-accent: #635bff; --ds-radius-sm: 6px; }\n"
        ".h-btn { display: inline-flex; padding: 12px 20px;\n"
        "         border-radius: var(--ds-button-radius, var(--ds-radius-sm, 6px)); }\n"
        "</style>\n"
        '<section class="hero"><button class="h-btn">Start now</button></section>\n'
        "</article>\n"
    )


def _all_prefixes_fragment() -> str:
    """Synthetic fragment carrying every button-shaped prefix at once.

    Lets a single assertion sweep confirm all five rules emit and the
    cascade order is correct (original DRL blocks first, override last).
    """
    return (
        '<article class="rs-library-page" data-rs-class="kitchen-sink" data-rs-brand="apple">\n'
        "<style>\n"
        ".b-btn { border-radius: var(--ds-radius-sm, 6px); }\n"
        ".h-btn { border-radius: var(--ds-radius-sm, 6px); }\n"
        ".n-btn { border-radius: var(--ds-radius-sm, 6px); }\n"
        ".cta__btn { border-radius: var(--ds-radius-sm, 6px); }\n"
        ".a-btn { border-radius: var(--ds-radius-sm, 6px); }\n"
        "</style>\n"
        '<button class="b-btn">x</button>\n'
        "</article>\n"
    )


def test_sibling_prefixes_receive_radius_apple_pill() -> None:
    """Apple's 980px pill propagates to all four sibling prefixes."""
    out = inject_button_override(_all_prefixes_fragment(), _apple_tokens())
    # Sibling-selector rule carries every prefix.
    assert ".b-btn, .h-btn, .n-btn, .cta__btn, .a-btn {" in out
    # The radius value is emitted exactly once in the sibling rule.
    sibling_segment = out.split(
        ".b-btn, .h-btn, .n-btn, .cta__btn, .a-btn {", 1
    )[1].split("}", 1)[0]
    assert "border-radius: 980px !important;" in sibling_segment


def test_sibling_prefixes_receive_radius_stripe_subtle() -> None:
    """Stripe's 4px chiclet propagates to all four sibling prefixes."""
    out = inject_button_override(_all_prefixes_fragment(), _stripe_tokens())
    sibling_segment = out.split(
        ".b-btn, .h-btn, .n-btn, .cta__btn, .a-btn {", 1
    )[1].split("}", 1)[0]
    assert "border-radius: 4px !important;" in sibling_segment


def test_sibling_prefixes_receive_radius_figma_sharp() -> None:
    """Figma's 0 sharp corner propagates to all four sibling prefixes."""
    out = inject_button_override(_all_prefixes_fragment(), _figma_tokens())
    sibling_segment = out.split(
        ".b-btn, .h-btn, .n-btn, .cta__btn, .a-btn {", 1
    )[1].split("}", 1)[0]
    assert "border-radius: 0 !important;" in sibling_segment


def test_override_triggers_on_h_btn_only_page() -> None:
    """Hero category (no ``.b-btn`` block) still receives the override.

    Pre-P11-A this fragment was a no-op because the gate keyed on
    ``.b-btn``; the audit memo flagged it as the dominant fidelity gap
    across the 13 non-buttons categories. The v2 gate keys on any of the
    five button-shaped prefixes so hero / nav / cta-block / alphabet
    pages receive the brand's corner-radius.
    """
    out = inject_button_override(_hero_fragment_no_b_btn(), _stripe_tokens())
    assert OVERRIDE_MARKER in out
    assert OVERRIDE_END_MARKER in out
    assert ".b-btn, .h-btn, .n-btn, .cta__btn, .a-btn {" in out
    assert "border-radius: 4px !important;" in out


def test_override_no_op_on_page_without_any_button_prefix() -> None:
    """Pages with none of the five prefixes (e.g. ``/colors/``) no-op."""
    colors_fragment = (
        '<article class="rs-library-page" data-rs-class="colors">\n'
        "<style>.c-swatch { width: 64px; height: 64px; }</style>\n"
        '<div class="c-swatch"></div>\n'
        "</article>\n"
    )
    assert (
        inject_button_override(colors_fragment, _apple_tokens())
        == colors_fragment
    )


def test_v2_override_is_idempotent_across_reruns() -> None:
    """Re-applying produces a single override span, not two stacked spans."""
    once = inject_button_override(_all_prefixes_fragment(), _apple_tokens())
    twice = inject_button_override(once, _apple_tokens())
    assert twice.count(OVERRIDE_MARKER) == 1
    assert twice.count(OVERRIDE_END_MARKER) == 1
    # And the rendered sibling rule appears once.
    assert (
        twice.count(".b-btn, .h-btn, .n-btn, .cta__btn, .a-btn {") == 1
    )


def test_v2_override_replaces_v1_override_in_place() -> None:
    """v1 override (no end marker) is stripped cleanly by v2 re-inject.

    Backward-compatibility seam: a fragment carrying a v1 override block
    (single ``.b-btn`` rule, no ``OVERRIDE_END_MARKER``) gets the v1
    block stripped and replaced with the v2 two-rule emission. No
    leftover v1 lines, no double override.
    """
    v1_fragment = (
        '<article class="rs-library-page" data-rs-class="buttons" data-rs-brand="apple">\n'
        "<style>\n"
        ".b-btn { padding: 10px 16px; border-radius: var(--ds-radius-sm, 6px); }\n"
        f"{OVERRIDE_MARKER}\n"
        ".b-btn {\n"
        "  border-radius: 980px !important;\n"
        "}\n"
        "</style>\n"
        '<button class="b-btn">x</button>\n'
        "</article>\n"
    )
    out = inject_button_override(v1_fragment, _stripe_tokens())
    # Exactly one start marker survives, and it is the v2 one (paired
    # with an end marker).
    assert out.count(OVERRIDE_MARKER) == 1
    assert out.count(OVERRIDE_END_MARKER) == 1
    # v1 Apple radius gone, v2 Stripe radius present.
    assert "border-radius: 980px !important;" not in out
    assert "border-radius: 4px !important;" in out


def test_sibling_rule_omitted_when_radius_empty() -> None:
    """No sibling rule when tokens lack a border_radius (prevents cascade
    pollution with an empty rule)."""
    no_radius: ButtonTokens = ButtonTokens(
        background_color="",
        color="",
        border_radius="",
        padding="10px 16px",
        padding_block="10px",
        padding_inline="16px",
        font_family="",
        font_size="14px",
        font_weight="500",
        border_width="",
        schema_version=1,
    )
    out = inject_button_override(_drl_buttons_fragment(), no_radius)
    assert OVERRIDE_MARKER in out
    assert ".b-btn, .h-btn, .n-btn, .cta__btn, .a-btn {" not in out
