"""Unit tests for resemblio_figma.converter.

Run from the package root::

    pytest

No network, no fixtures from disk - all manifests are inline so tests run
under any environment that has pydantic available.
"""
from __future__ import annotations

import pytest

from resemblio_figma import (
    dtcg_path_to_figma_name,
    dtcg_to_figma_variables,
    hex_to_rgba_floats,
)
from resemblio_figma.constants import (
    COLLECTION_COLORS,
    COLLECTION_NUMBERS,
    COLLECTION_SPACING,
    COLLECTION_TYPOGRAPHY,
    DEFAULT_MODE_ID,
    DEFAULT_MODE_NAME,
    FIGMA_SCHEMA_VERSION,
)


def _resemblio_manifest_sample() -> dict:
    """A representative DTCG manifest matching the Resemblio extractor's output."""
    return {
        "schema_version": 2,
        "color": {
            "bg": {"$value": "#ffffff", "$type": "color"},
            "text": {"$value": "#111111", "$type": "color"},
            "brand-primary": {"$value": "#cc3344", "$type": "color"},
            "brand-secondary": {"$value": "#3366cc", "$type": "color"},
        },
        "fontFamily": {
            "body": {"$value": "Inter", "$type": "fontFamily"},
            "mono": {"$value": "JetBrains Mono", "$type": "fontFamily"},
        },
        "dimension": {
            "radius-md": {"$value": "8px", "$type": "dimension"},
            "space-4": {"$value": "16px", "$type": "dimension"},
        },
        "number": {
            "line-height-tight": {"$value": 1.1, "$type": "number"},
        },
    }


# ---------------------------------------------------------------------- 1

def test_happy_path_produces_full_payload_with_all_routed_collections() -> None:
    """A full DTCG manifest yields a payload with Colors, Spacing, Typography, Numbers."""
    payload = dtcg_to_figma_variables(
        _resemblio_manifest_sample(),
        source_url="https://example.com",
    )

    collection_names = [c.name for c in payload.collections]
    # All four routed groups present in canonical order.
    assert collection_names == [
        COLLECTION_COLORS,
        COLLECTION_SPACING,
        COLLECTION_TYPOGRAPHY,
        COLLECTION_NUMBERS,
    ]
    assert len(payload.collections) >= 3  # spec asks for 3+

    # Each collection has the single Light mode.
    for collection in payload.collections:
        assert len(collection.modes) == 1
        assert collection.modes[0].modeId == DEFAULT_MODE_ID
        assert collection.modes[0].name == DEFAULT_MODE_NAME

    # Every Variable carries a value under the Light mode id and a slash-name.
    for variable in payload.variables:
        assert DEFAULT_MODE_ID in variable.valuesByMode
        # Names use slash-hierarchy convention.
        assert "/" in variable.name or variable.name.istitle()
        # No DTCG-flavored separators leaked through.
        assert "." not in variable.name
        assert "-" not in variable.name

    # Color count matches the routable hex leaves (all four parsed).
    color_vars = [v for v in payload.variables if v.resolvedType == "COLOR"]
    assert len(color_vars) == 4

    # Metadata stamped.
    assert payload.figma_schema_version == FIGMA_SCHEMA_VERSION
    assert payload.resemblio_schema_version == 2
    assert payload.source_url == "https://example.com"


# ---------------------------------------------------------------------- 2

def test_empty_manifest_returns_minimum_valid_payload() -> None:
    """An empty manifest yields zero collections and zero variables without raising."""
    payload = dtcg_to_figma_variables({})
    assert payload.collections == ()
    assert payload.variables == ()
    assert payload.figma_schema_version == FIGMA_SCHEMA_VERSION
    assert payload.resemblio_schema_version is None


# ---------------------------------------------------------------------- 3

def test_hex_to_rgba_round_trips_with_reasonable_precision() -> None:
    """Hex -> RGBAFloat -> hex preserves the 8-bit source values."""
    cases = [
        ("#ff0000", 1.0, 0.0, 0.0),
        ("#00ff00", 0.0, 1.0, 0.0),
        ("#0000ff", 0.0, 0.0, 1.0),
        ("#ffffff", 1.0, 1.0, 1.0),
        ("#000000", 0.0, 0.0, 0.0),
        ("#3366cc", 0.2, 0.4, 0.8),
    ]
    for hex_color, exp_r, exp_g, exp_b in cases:
        rgba = hex_to_rgba_floats(hex_color)
        assert rgba.r == pytest.approx(exp_r, abs=0.005), hex_color
        assert rgba.g == pytest.approx(exp_g, abs=0.005), hex_color
        assert rgba.b == pytest.approx(exp_b, abs=0.005), hex_color
        assert rgba.a == 1.0

        # Round-trip back to 8-bit ints.
        r8 = round(rgba.r * 255)
        g8 = round(rgba.g * 255)
        b8 = round(rgba.b * 255)
        assert f"#{r8:02x}{g8:02x}{b8:02x}" == hex_color

    # Three-digit shorthand equivalence.
    assert hex_to_rgba_floats("#f00") == hex_to_rgba_floats("#ff0000")

    # Invalid input raises.
    with pytest.raises(ValueError):
        hex_to_rgba_floats("rgb(1,2,3)")


# ---------------------------------------------------------------------- 4

def test_hierarchy_path_mapping_uses_figma_slash_convention() -> None:
    """DTCG flat-with-dots/dashes names become Figma slash-hierarchy names."""
    assert dtcg_path_to_figma_name("color.brand.primary") == "Color/Brand/Primary"
    assert dtcg_path_to_figma_name("brand-primary") == "Brand/Primary"
    assert dtcg_path_to_figma_name("space-4") == "Space/4"
    # Mixed separators collapse cleanly.
    assert dtcg_path_to_figma_name("brand--primary") == "Brand/Primary"
    # First-char only is title-cased; existing casing preserved elsewhere.
    assert dtcg_path_to_figma_name("XL.size") == "XL/Size"
    # Empty / whitespace input.
    assert dtcg_path_to_figma_name("") == ""
    assert dtcg_path_to_figma_name("   ") == ""


# ---------------------------------------------------------------------- 5

def test_mixed_type_manifest_routes_each_group_to_correct_collection() -> None:
    """Color, dimension, fontFamily, number leaves each land in the right Collection."""
    manifest = {
        "color": {"primary": {"$value": "#abcdef", "$type": "color"}},
        "dimension": {"radius-sm": {"$value": "4px", "$type": "dimension"}},
        "fontFamily": {"display": {"$value": "Playfair", "$type": "fontFamily"}},
        "number": {"scale": {"$value": 1.25, "$type": "number"}},
    }
    payload = dtcg_to_figma_variables(manifest)

    by_collection: dict[str, list] = {}
    collection_by_id = {c.id: c.name for c in payload.collections}
    for variable in payload.variables:
        by_collection.setdefault(collection_by_id[variable.collectionId], []).append(variable)

    assert COLLECTION_COLORS in by_collection
    assert COLLECTION_SPACING in by_collection
    assert COLLECTION_TYPOGRAPHY in by_collection
    assert COLLECTION_NUMBERS in by_collection

    # Type assertions per collection.
    assert by_collection[COLLECTION_COLORS][0].resolvedType == "COLOR"
    assert by_collection[COLLECTION_SPACING][0].resolvedType == "FLOAT"
    assert by_collection[COLLECTION_TYPOGRAPHY][0].resolvedType == "STRING"
    assert by_collection[COLLECTION_NUMBERS][0].resolvedType == "FLOAT"

    # Dimension parsing: 4px -> 4.0.
    spacing_value = by_collection[COLLECTION_SPACING][0].valuesByMode[DEFAULT_MODE_ID]
    assert spacing_value == pytest.approx(4.0, abs=1e-6)

    # Number parsing: 1.25 -> 1.25.
    number_value = by_collection[COLLECTION_NUMBERS][0].valuesByMode[DEFAULT_MODE_ID]
    assert number_value == pytest.approx(1.25, abs=1e-6)

    # Typography string passthrough.
    typo_value = by_collection[COLLECTION_TYPOGRAPHY][0].valuesByMode[DEFAULT_MODE_ID]
    assert typo_value == "Playfair"


# ---------------------------------------------------------------------- 6

def test_round_trip_stability_two_calls_produce_identical_output() -> None:
    """Calling the converter twice on the same manifest yields identical bytes."""
    manifest = _resemblio_manifest_sample()

    payload_a = dtcg_to_figma_variables(manifest)
    payload_b = dtcg_to_figma_variables(manifest)
    assert payload_a.model_dump() == payload_b.model_dump()

    import json
    json_a = json.dumps(payload_a.model_dump(mode="json"), sort_keys=True)
    json_b = json.dumps(payload_b.model_dump(mode="json"), sort_keys=True)
    assert json_a == json_b


# ---------------------------------------------------------------------- bonus

def test_invalid_color_values_are_skipped_not_fatal() -> None:
    """Non-hex color leaves (rgb, named, gradient) must not crash extraction."""
    manifest = {
        "color": {
            "ok": {"$value": "#ffffff", "$type": "color"},
            "weird": {"$value": "rgb(10, 20, 30)", "$type": "color"},
            "named": {"$value": "cornflowerblue", "$type": "color"},
        },
    }
    payload = dtcg_to_figma_variables(manifest)
    color_vars = [v for v in payload.variables if v.resolvedType == "COLOR"]
    # Only the one valid hex landed.
    assert len(color_vars) == 1
    assert color_vars[0].name == "Colors/Ok"
