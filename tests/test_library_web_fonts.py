"""Tests for ``app.library_web_fonts`` (v2 inspirado-no-copiado contract).

Pins the Phase 1 corrected L-20 contract (Frank, 2026-06-04 02:35 UTC):

- Every brand resolves to at least one free alternative via the
  registry; the link-tag emitter never returns ``None`` for a brand
  that declared at least one font slot.
- Brands whose first-preference fonts are private CDN-only faces
  (Aeon-shape: PP Right Grotesk Wide + Academica + Atlas Typewriter)
  now load the free alternatives picked by the registry rather than
  silently falling through to system fallbacks.
- The disclosure payload carries the brand's actual font name AND the
  free alternative; the rendered aside surfaces both.
- The font-alternative root block overrides the brand's
  ``--ds-font-*`` variables to point at the free alternative.
- The v1 alias ``extract_google_font_families`` keeps working but now
  returns free-alternative family names, not brand-declared families.

No I/O. Pure-data.
"""
from __future__ import annotations

from app.library_web_fonts import (
    FONT_TOKEN_KEYS,
    GOOGLE_FONT_ALLOWLIST,
    LIBRARY_WEB_FONTS_SCHEMA_VERSION,
    FontDisclosurePayload,
    SlotResolution,
    build_font_alternative_root_block,
    build_font_disclosure_payload,
    build_google_fonts_link_tag,
    extract_first_preference_families,
    extract_google_font_families,
    render_font_disclosure_html,
    resolve_free_alternatives,
)


def test_schema_version_is_v2() -> None:
    assert LIBRARY_WEB_FONTS_SCHEMA_VERSION == "library_web_fonts_v2"


def test_font_token_keys_cover_ds_underscore_and_bare_shapes() -> None:
    """Regression guard: parser must accept three key shapes per slot."""
    assert "ds-font-display" in FONT_TOKEN_KEYS
    assert "font-display" in FONT_TOKEN_KEYS
    assert "font_display" in FONT_TOKEN_KEYS


# ----------------------------------------------------------------------
# extract_first_preference_families
# ----------------------------------------------------------------------


def test_empty_tokens_yield_no_first_preference_families() -> None:
    assert extract_first_preference_families({}) == {}


def test_first_preference_skips_generic_keywords() -> None:
    tokens = {"ds-font-display": "Inter, sans-serif"}
    assert extract_first_preference_families(tokens) == {"display": "Inter"}


def test_first_preference_walks_multiple_slots() -> None:
    tokens = {
        "ds-font-display": "PP Right Grotesk Wide, sans-serif",
        "ds-font-body": "Lora, serif",
        "ds-font-mono": "JetBrains Mono, monospace",
    }
    result = extract_first_preference_families(tokens)
    assert result == {
        "display": "PP Right Grotesk Wide",
        "body": "Lora",
        "mono": "JetBrains Mono",
    }


def test_first_preference_handles_quoted_families() -> None:
    tokens = {"ds-font-display": "'PP Right Grotesk Wide', 'Helvetica Neue', sans-serif"}
    assert extract_first_preference_families(tokens) == {
        "display": "PP Right Grotesk Wide",
    }


def test_first_preference_handles_underscored_keys() -> None:
    tokens = {"font_body": "Lora, serif"}
    assert extract_first_preference_families(tokens) == {"body": "Lora"}


# ----------------------------------------------------------------------
# resolve_free_alternatives + link tag
# ----------------------------------------------------------------------


def test_aeon_shape_now_loads_a_free_alternative() -> None:
    """Aeon shape: PP Right Grotesk Wide -> Plus Jakarta Sans (v2 fix).

    Pre-v2 the link-tag returned ``None`` because none of Aeon's family
    stack entries were on the Google Fonts allowlist. Post-v2 the
    registry returns Plus Jakarta Sans and the link tag must surface it.
    """
    tokens = {
        "ds-font-display": (
            "'PP Right Grotesk Wide', 'Founders Grotesk', 'Helvetica Neue', "
            "Helvetica, Arial, sans-serif"
        ),
        "ds-font-body": "'Academica', Georgia, 'Times New Roman', 'Times', serif",
        "ds-font-mono": "'Atlas Typewriter', 'SFMono-Regular', Consolas, Menlo, monospace",
    }
    alternatives = resolve_free_alternatives(tokens)
    names = [alt["free_alternative_name"] for alt in alternatives]
    assert "Plus Jakarta Sans" in names
    assert "Lora" in names  # Academica row points at Lora


def test_link_tag_loads_free_alternatives_for_aeon_shape() -> None:
    tokens = {
        "ds-font-display": "'PP Right Grotesk Wide', sans-serif",
        "ds-font-body": "'Academica', serif",
    }
    tag = build_google_fonts_link_tag(tokens)
    assert tag is not None
    assert "fonts.googleapis.com/css2" in tag
    assert "family=Plus+Jakarta+Sans:wght@300..700" in tag
    assert "family=Lora:wght@300..700" in tag
    assert "display=swap" in tag
    assert 'crossorigin="anonymous"' in tag


def test_link_tag_for_inter_brand_loads_inter() -> None:
    tag = build_google_fonts_link_tag({"ds-font-display": "Inter, sans-serif"})
    assert tag is not None
    assert "family=Inter:wght@300..700" in tag


def test_link_tag_for_empty_tokens_still_loads_default_inter() -> None:
    """v2 contract: even an empty tokens dict resolves to the Inter default."""
    tag = build_google_fonts_link_tag({})
    assert tag is not None
    assert "family=Inter:wght@300..700" in tag


def test_link_tag_dedupes_free_alternatives_across_slots() -> None:
    """If display + body both resolve to Inter, the URL must list it once."""
    tokens = {
        "ds-font-display": "Sohne, sans-serif",  # -> Inter
        "ds-font-body": "ABC Diatype, sans-serif",  # -> Inter
    }
    tag = build_google_fonts_link_tag(tokens)
    assert tag is not None
    assert tag.count("family=Inter") == 1


# ----------------------------------------------------------------------
# disclosure payload + HTML rendering
# ----------------------------------------------------------------------


def test_disclosure_payload_shape_is_stable() -> None:
    payload = build_font_disclosure_payload(
        {"ds-font-display": "PP Right Grotesk Wide, sans-serif"}
    )
    assert isinstance(payload, FontDisclosurePayload)
    assert isinstance(payload.primary_resolution, SlotResolution)
    assert payload.primary_resolution.slot == "display"
    assert payload.primary_resolution.brand_font_first_preference == "PP Right Grotesk Wide"
    assert payload.primary_resolution.mapping["free_alternative_name"] == "Plus Jakarta Sans"
    assert "Plus Jakarta Sans" in payload.free_alternative_families
    assert payload.google_fonts_url is not None
    assert payload.schema_version == "brand_font_registry_v1"


def test_disclosure_payload_for_empty_tokens_uses_default() -> None:
    payload = build_font_disclosure_payload({})
    assert payload.primary_resolution.brand_font_first_preference is None
    assert payload.primary_resolution.mapping["free_alternative_name"] == "Inter"


def test_render_font_disclosure_html_surfaces_brand_and_free_alternative() -> None:
    payload = build_font_disclosure_payload(
        {"ds-font-display": "PP Right Grotesk Wide, sans-serif"}
    )
    html = render_font_disclosure_html(payload, brand_display_name="Aeon")
    assert "<aside" in html and "rs-font-attribution" in html
    assert "Aeon uses PP Right Grotesk Wide" in html
    assert "Plus Jakarta Sans" in html
    assert "Tokotype" in html
    assert "free, designed by" in html


def test_render_font_disclosure_html_handles_empty_brand_tokens_gracefully() -> None:
    payload = build_font_disclosure_payload({})
    html = render_font_disclosure_html(payload, brand_display_name="Mystery")
    assert "ships no captured brand font" in html
    assert "Inter" in html


def test_render_font_disclosure_html_links_to_google_fonts_url() -> None:
    payload = build_font_disclosure_payload({"ds-font-display": "Inter, sans-serif"})
    html = render_font_disclosure_html(payload, brand_display_name="Stripe")
    assert "https://fonts.googleapis.com/css2" in html
    assert 'href="https://fonts.googleapis.com/css2' in html


# ----------------------------------------------------------------------
# font alternative root block
# ----------------------------------------------------------------------


def test_root_block_overrides_each_declared_slot() -> None:
    tokens = {
        "ds-font-display": "PP Right Grotesk Wide, sans-serif",
        "ds-font-body": "Academica, serif",
        "ds-font-mono": "Atlas Typewriter, monospace",
    }
    block = build_font_alternative_root_block(tokens)
    assert "--ds-font-display: 'Plus Jakarta Sans'" in block
    assert "--ds-font-body: 'Lora'" in block
    # The Atlas Typewriter row is not in the registry; it falls back to Inter.
    assert "--ds-font-mono: 'Inter'" in block


def test_root_block_for_empty_tokens_emits_default_inter_overrides() -> None:
    block = build_font_alternative_root_block({})
    assert "--ds-font-display: 'Inter'" in block
    assert "--ds-font-body: 'Inter'" in block


def test_root_block_ends_with_newline() -> None:
    block = build_font_alternative_root_block(
        {"ds-font-display": "Inter, sans-serif"}
    )
    assert block.endswith("\n")


def test_root_block_only_emits_declared_slots_when_some_present() -> None:
    """Brand declares display only; root block should override display."""
    tokens = {"ds-font-display": "Inter, sans-serif"}
    block = build_font_alternative_root_block(tokens)
    assert "--ds-font-display" in block
    # No body override because the brand did not declare body
    assert "--ds-font-body" not in block


# ----------------------------------------------------------------------
# v1 back-compat alias
# ----------------------------------------------------------------------


def test_extract_google_font_families_v1_alias_returns_free_alternatives() -> None:
    """v1 alias now returns free-alternative family names."""
    tokens = {"ds-font-display": "PP Right Grotesk Wide, sans-serif"}
    families = extract_google_font_families(tokens)
    assert families == ("Plus Jakarta Sans",)


def test_extract_google_font_families_v1_alias_for_empty_returns_default() -> None:
    """v1 alias now returns ``("Inter",)`` instead of empty for empty input."""
    assert extract_google_font_families({}) == ("Inter",)


def test_allowlist_back_compat_includes_core_modern_families() -> None:
    """The retained allowlist constant still carries the documented core families."""
    must_have = {
        "Inter",
        "Roboto",
        "Plus Jakarta Sans",
        "Lora",
        "Playfair Display",
        "JetBrains Mono",
        "Manrope",
    }
    missing = must_have - GOOGLE_FONT_ALLOWLIST
    assert missing == set(), f"allowlist regressed; missing: {sorted(missing)}"


# ----------------------------------------------------------------------
# tolerance + edge cases
# ----------------------------------------------------------------------


def test_trailing_and_double_commas_in_stack_are_tolerated() -> None:
    tokens = {"ds-font-display": "Inter, , sans-serif,"}
    assert extract_first_preference_families(tokens) == {"display": "Inter"}


def test_empty_string_value_is_safely_ignored() -> None:
    tokens = {
        "ds-font-display": "",
        "ds-font-body": "Inter, sans-serif",
    }
    families = extract_first_preference_families(tokens)
    assert families == {"body": "Inter"}


def test_quoted_families_with_spaces_parse_correctly() -> None:
    tokens = {"ds-font-display": "'Plus Jakarta Sans', sans-serif"}
    assert extract_first_preference_families(tokens) == {
        "display": "Plus Jakarta Sans",
    }


def test_bare_token_keys_supported() -> None:
    tokens = {"font-display": "Manrope, sans-serif"}
    assert extract_first_preference_families(tokens) == {"display": "Manrope"}
