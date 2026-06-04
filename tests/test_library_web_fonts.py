"""Tests for app.library_web_fonts.

Pins the L-20 fix contract (Frank, 2026-06-04): library pages must emit
a Google Fonts <link> tag for every allowlisted brand-declared family,
silently skip families outside the allowlist, and produce no link tag
at all when no allowlisted family is present.
"""
from __future__ import annotations

from app.library_web_fonts import (
    GOOGLE_FONT_ALLOWLIST,
    LIBRARY_WEB_FONTS_SCHEMA_VERSION,
    build_google_fonts_link_tag,
    extract_google_font_families,
)


def test_schema_version_is_stable_sentinel() -> None:
    """Downstream tools key off this sentinel; treat any change as a contract bump."""
    assert LIBRARY_WEB_FONTS_SCHEMA_VERSION == "library_web_fonts_v1"


def test_empty_tokens_yield_no_families() -> None:
    assert extract_google_font_families({}) == ()


def test_brand_with_no_allowlisted_families_yields_none() -> None:
    """Aeon-shaped tokens: private CDN faces only -> no link tag.

    Pins the graceful-degrade contract. Aeon's stack
    (`PP Right Grotesk Wide`, `Academica`, `Atlas Typewriter`) is all
    private licensed faces; we must not emit a Google Fonts request
    that would 404 in the browser console.
    """
    tokens = {
        "ds-font-display": "'PP Right Grotesk Wide', 'Founders Grotesk', 'Helvetica Neue', Helvetica, Arial, sans-serif",
        "ds-font-body": "'Academica', Georgia, 'Times New Roman', 'Times', serif",
        "ds-font-mono": "'Atlas Typewriter', 'SFMono-Regular', Consolas, Menlo, monospace",
    }
    assert extract_google_font_families(tokens) == ()
    assert build_google_fonts_link_tag(tokens) is None


def test_single_allowlisted_family_is_detected() -> None:
    tokens = {"ds-font-display": "Inter, sans-serif"}
    assert extract_google_font_families(tokens) == ("Inter",)


def test_multiple_allowlisted_families_dedup_in_first_seen_order() -> None:
    """Two slots may quote the same family; the dedup is by name not by slot."""
    tokens = {
        "ds-font-display": "Inter, Helvetica, Arial, sans-serif",
        "ds-font-body": "Inter, Georgia, serif",
        "ds-font-mono": "JetBrains Mono, monospace",
    }
    assert extract_google_font_families(tokens) == ("Inter", "JetBrains Mono")


def test_quoted_families_with_spaces_parse_correctly() -> None:
    """`'Source Sans 3'` and `"Plus Jakarta Sans"` must both parse to bare names."""
    tokens = {
        "ds-font-display": "'Plus Jakarta Sans', 'Source Sans 3', sans-serif",
    }
    assert extract_google_font_families(tokens) == (
        "Plus Jakarta Sans",
        "Source Sans 3",
    )


def test_bare_token_keys_are_supported_alongside_ds_prefix() -> None:
    """Organic rows use bare `font-display`; the parser must walk both shapes."""
    tokens = {"font-display": "Manrope, sans-serif"}
    assert extract_google_font_families(tokens) == ("Manrope",)


def test_underscored_token_keys_are_supported() -> None:
    """Some seed shapes write `font_display`; parser handles that too."""
    tokens = {"font_body": "DM Sans, sans-serif"}
    assert extract_google_font_families(tokens) == ("DM Sans",)


def test_non_allowlisted_families_are_silently_dropped() -> None:
    """A brand mixing one allowlisted + one private face -> only the allowlisted one."""
    tokens = {
        "ds-font-display": "'Söhne', Inter, sans-serif",
    }
    assert extract_google_font_families(tokens) == ("Inter",)


def test_link_tag_shape_carries_weight_range_and_swap() -> None:
    """The emitted <link> must request the 300..700 weight range and display=swap."""
    tag = build_google_fonts_link_tag({"ds-font-display": "Inter, sans-serif"})
    assert tag is not None
    assert tag.startswith('<link rel="stylesheet"')
    assert "fonts.googleapis.com/css2" in tag
    assert "family=Inter:wght@300..700" in tag
    assert "display=swap" in tag
    assert 'crossorigin="anonymous"' in tag


def test_link_tag_encodes_spaces_as_plus() -> None:
    """Google Fonts URL syntax encodes spaces as `+` inside family names."""
    tag = build_google_fonts_link_tag({"ds-font-display": "Plus Jakarta Sans, sans-serif"})
    assert tag is not None
    assert "family=Plus+Jakarta+Sans:wght@300..700" in tag


def test_link_tag_chains_multiple_families_with_ampersand() -> None:
    """Two allowlisted families -> two `family=` params separated by `&`."""
    tag = build_google_fonts_link_tag({
        "ds-font-display": "Inter, sans-serif",
        "ds-font-body": "Lora, serif",
    })
    assert tag is not None
    assert "family=Inter:wght@300..700" in tag
    assert "family=Lora:wght@300..700" in tag
    # `&` must connect the two family= params, not `?` or `;`.
    assert "&family=Lora" in tag or "&family=Inter" in tag


def test_allowlist_includes_core_modern_brand_families() -> None:
    """Removal of any of these breaks a common library brand. Regression guard."""
    must_have = {
        "Inter",
        "Roboto",
        "Open Sans",
        "Lato",
        "Montserrat",
        "Source Sans 3",
        "Playfair Display",
        "Merriweather",
        "JetBrains Mono",
        "Fira Code",
    }
    missing = must_have - GOOGLE_FONT_ALLOWLIST
    assert missing == set(), f"Google Fonts allowlist regressed; missing: {sorted(missing)}"


def test_empty_string_value_is_safely_ignored() -> None:
    """A token key present but with an empty value must not raise."""
    tokens = {"ds-font-display": "", "ds-font-body": "Inter, sans-serif"}
    assert extract_google_font_families(tokens) == ("Inter",)


def test_trailing_and_double_commas_in_stack_are_tolerated() -> None:
    """Real-world tokens sometimes carry empty entries; the parser drops them."""
    tokens = {"ds-font-display": "Inter, , sans-serif,"}
    assert extract_google_font_families(tokens) == ("Inter",)
