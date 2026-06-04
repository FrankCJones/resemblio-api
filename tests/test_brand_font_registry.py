"""Tests for ``app.brand_font_registry``.

Pins the Phase 1 inspirado-no-copiado contract (Frank, 2026-06-04
02:35 UTC):

- Every registry row carries the full ``BrandFontMapping`` shape.
- Every free alternative is a Google Fonts family (verified by being
  present in ``library_web_fonts.GOOGLE_FONT_ALLOWLIST``).
- The resolver returns exact-match hits, falls back via stem-prefix
  and substring matching, and ultimately returns the Inter default
  for unknown input.
- The schema sentinel is stable.

Pure-data tests. No network. No I/O.
"""
from __future__ import annotations

import pytest

from app.brand_font_registry import (
    BRAND_FONT_REGISTRY,
    BRAND_FONT_REGISTRY_SCHEMA_VERSION,
    DEFAULT_FREE_ALTERNATIVE,
    BrandFontMapping,
    build_multi_family_google_fonts_url,
    build_single_family_google_fonts_url,
    lookup,
)
from app.library_web_fonts import GOOGLE_FONT_ALLOWLIST


_REQUIRED_FIELDS = (
    "brand_font_name",
    "brand_font_designer",
    "free_alternative_name",
    "free_alternative_designer",
    "free_alternative_google_fonts_url",
    "similarity_rationale_short_line",
)


def test_schema_version_is_stable_sentinel() -> None:
    assert BRAND_FONT_REGISTRY_SCHEMA_VERSION == "brand_font_registry_v1"


def test_registry_has_at_least_fifteen_rows() -> None:
    """The Phase 1 brief requires at least 15 seed mappings."""
    assert len(BRAND_FONT_REGISTRY) >= 15


def test_every_row_carries_full_brand_font_mapping_shape() -> None:
    """Every required field must be present and a non-empty string."""
    for key, mapping in BRAND_FONT_REGISTRY.items():
        for field in _REQUIRED_FIELDS:
            assert field in mapping, f"{key} missing field {field!r}"
            value = mapping[field]  # type: ignore[literal-required]
            assert isinstance(value, str), f"{key}.{field} is not a string"
            assert value.strip(), f"{key}.{field} is empty"


def test_every_free_alternative_is_on_google_fonts_allowlist() -> None:
    """A free alternative must be served by Google Fonts.

    The disclosure block links the user to the Google Fonts page; an
    alternative that is not on Google Fonts would 404 in the user's
    browser and break the attribution promise.
    """
    for key, mapping in BRAND_FONT_REGISTRY.items():
        alt = mapping["free_alternative_name"]
        assert alt in GOOGLE_FONT_ALLOWLIST, (
            f"registry row {key!r} points at {alt!r}, which is NOT on the "
            "Google Fonts allowlist; add it to the allowlist or pick a "
            "different free alternative."
        )


def test_default_free_alternative_carries_full_shape() -> None:
    for field in _REQUIRED_FIELDS:
        assert field in DEFAULT_FREE_ALTERNATIVE
        value = DEFAULT_FREE_ALTERNATIVE[field]  # type: ignore[literal-required]
        assert isinstance(value, str)
        assert value.strip()


def test_default_free_alternative_is_inter() -> None:
    assert DEFAULT_FREE_ALTERNATIVE["free_alternative_name"] == "Inter"


def test_google_fonts_url_carries_canonical_weight_range() -> None:
    """Every registry URL must carry the wght@300..700 range + display=swap."""
    for key, mapping in BRAND_FONT_REGISTRY.items():
        url = mapping["free_alternative_google_fonts_url"]
        assert url.startswith("https://fonts.googleapis.com/css2?"), key
        assert "wght@300..700" in url, key
        assert "display=swap" in url, key


def test_single_family_url_encodes_spaces_as_plus() -> None:
    url = build_single_family_google_fonts_url("Plus Jakarta Sans")
    assert "family=Plus+Jakarta+Sans:wght@300..700" in url


def test_multi_family_url_chains_with_ampersand() -> None:
    url = build_multi_family_google_fonts_url(("Inter", "Lora"))
    assert url is not None
    assert "family=Inter:wght@300..700" in url
    assert "family=Lora:wght@300..700" in url
    assert url.count("family=") == 2


def test_multi_family_url_returns_none_for_empty_input() -> None:
    assert build_multi_family_google_fonts_url(()) is None


# ----------------------------------------------------------------------
# lookup() resolver
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_input, expected_brand_font_name",
    [
        ("PP Right Grotesk Wide", "PP Right Grotesk Wide"),
        ("pp right grotesk wide", "PP Right Grotesk Wide"),
        ("PP-Right-Grotesk-Wide", "PP Right Grotesk Wide"),
        ("PP_Right_Grotesk_Wide", "PP Right Grotesk Wide"),
        ("Sohne", "Sohne"),
        ("sohne", "Sohne"),
        ("ABC Diatype", "ABC Diatype"),
        ("Atlas Grotesk", "Atlas Grotesk"),
        ("San Francisco", "San Francisco"),
        ("Inter", "Inter"),
        ("Roboto", "Roboto"),
        ("GT America", "GT America"),
        ("Untitled Sans", "Untitled Sans"),
        ("Suisse", "Suisse"),
        ("Larsseit", "Larsseit"),
        ("Whyte", "Whyte"),
        ("Tiempos", "Tiempos"),
        ("Tiempos Headline", "Tiempos Headline"),
    ],
)
def test_lookup_exact_and_normalized_matches(
    raw_input: str, expected_brand_font_name: str
) -> None:
    """Exact, case-insensitive, and punctuation-normalized hits resolve correctly."""
    mapping = lookup(raw_input)
    assert mapping["brand_font_name"] == expected_brand_font_name


def test_lookup_unknown_font_returns_default() -> None:
    """An entirely unfamiliar family falls back to the Inter default."""
    mapping = lookup("Completely Made Up Font Name 9000")
    assert mapping["free_alternative_name"] == "Inter"
    assert mapping["brand_font_name"] == "Default free type system"


def test_lookup_empty_string_returns_default() -> None:
    assert lookup("") is DEFAULT_FREE_ALTERNATIVE


def test_lookup_none_like_input_returns_default() -> None:
    """Whitespace-only input normalizes to empty and returns the default."""
    assert lookup("   ") is DEFAULT_FREE_ALTERNATIVE


def test_lookup_fuzzy_stem_match_resolves_to_parent_row() -> None:
    """``PP Right Grotesk Wide Narrow`` should fuzzy-match the Wide row."""
    mapping = lookup("PP Right Grotesk Wide Narrow")
    assert mapping["brand_font_name"] == "PP Right Grotesk Wide"


def test_lookup_substring_match_resolves_to_registered_row() -> None:
    """``PP Right Grotesk`` (no Wide suffix) substring-matches the Wide row."""
    mapping = lookup("PP Right Grotesk")
    # Either the bare "PP Right Grotesk" row (registered) or the wider Wide row.
    assert "PP Right Grotesk" in mapping["brand_font_name"]


def test_lookup_returns_brand_font_mapping_typed_dict_shape() -> None:
    """The resolver's return value must satisfy the BrandFontMapping shape."""
    mapping = lookup("Inter")
    for field in _REQUIRED_FIELDS:
        assert field in mapping


def test_lookup_passthrough_for_inter_is_self_mapping() -> None:
    """Inter -> Inter; brand_font_name == free_alternative_name."""
    mapping = lookup("Inter")
    assert mapping["brand_font_name"] == "Inter"
    assert mapping["free_alternative_name"] == "Inter"


def test_lookup_passthrough_for_roboto_is_self_mapping() -> None:
    mapping = lookup("Roboto")
    assert mapping["brand_font_name"] == "Roboto"
    assert mapping["free_alternative_name"] == "Roboto"


def test_default_url_is_inter_url() -> None:
    """The default mapping's URL must point at Inter on Google Fonts."""
    assert "family=Inter" in DEFAULT_FREE_ALTERNATIVE["free_alternative_google_fonts_url"]


def test_brand_font_mapping_is_typed_dict() -> None:
    """BrandFontMapping is a TypedDict so static type checkers see field names."""
    # TypedDict declares __required_keys__ on 3.11+ and __annotations__ always.
    assert hasattr(BrandFontMapping, "__annotations__")
    for field in _REQUIRED_FIELDS:
        assert field in BrandFontMapping.__annotations__


def test_registry_keys_are_all_normalized() -> None:
    """Registry keys must be lowercase and free of punctuation runs.

    The resolver normalizes input before lookup; if a key is not also
    normalized, the lookup will silently miss it.
    """
    import re

    bad: list[str] = []
    for key in BRAND_FONT_REGISTRY:
        if key != key.lower():
            bad.append(key)
            continue
        if re.search(r"[^a-z0-9 ]", key):
            bad.append(key)
            continue
        if "  " in key:
            bad.append(key)
    assert bad == [], f"registry keys not normalized: {bad}"


def test_pp_right_grotesk_wide_resolves_to_plus_jakarta_sans() -> None:
    """Spec-canonical mapping from the Phase 1 brief."""
    mapping = lookup("PP Right Grotesk Wide")
    assert mapping["free_alternative_name"] == "Plus Jakarta Sans"
    assert mapping["free_alternative_designer"] == "Tokotype"


def test_tiempos_headline_resolves_to_playfair_display() -> None:
    mapping = lookup("Tiempos Headline")
    assert mapping["free_alternative_name"] == "Playfair Display"
