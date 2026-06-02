"""Unit tests for resemblio_shadcn.converter.

Run from the package root::

    pytest

No network, no fixtures from disk - all manifests are inline so tests run
under any environment that has pydantic available.
"""
from __future__ import annotations

import pytest

from resemblio_shadcn import dtcg_to_shadcn, render_globals_css, render_tailwind_config
from resemblio_shadcn.constants import (
    SHADCN_COLOR_SLOTS,
    SHADCN_DEFAULT_LIGHT,
    SHADCN_DEFAULT_DARK,
)
from resemblio_shadcn.converter import hex_to_hsl_triple


def _resemblio_manifest_sample() -> dict:
    """A representative DTCG manifest matching the Resemblio extractor's output."""
    return {
        "schema_version": 1,
        "color": {
            "bg": {"$value": "#ffffff", "$type": "color"},
            "surface": {"$value": "#f7f7f5", "$type": "color"},
            "text": {"$value": "#111111", "$type": "color"},
            "text-muted": {"$value": "#666666", "$type": "color"},
            "accent": {"$value": "#cc3344", "$type": "color"},
            "accent-2": {"$value": "#3366cc", "$type": "color"},
            "border": {"$value": "#e5e5e5", "$type": "color"},
        },
        "fontFamily": {
            "body": {"$value": "Inter", "$type": "fontFamily"},
            "mono": {"$value": "JetBrains Mono", "$type": "fontFamily"},
        },
        "dimension": {
            "radius-md": {"$value": "8px", "$type": "dimension"},
            "space-4": {"$value": "16px", "$type": "dimension"},
        },
    }


# ---------------------------------------------------------------------- 1


def test_happy_path_produces_complete_shadcn_theme() -> None:
    """A full manifest yields a ShadcnTheme with every slot filled and primary set from the palette."""
    theme = dtcg_to_shadcn(_resemblio_manifest_sample(), source_url="https://example.com")

    # All 24 shadcn color slots present in light + dark via the model.
    light_pairs = dict(theme.light.as_ordered_pairs())
    dark_pairs = dict(theme.dark.as_ordered_pairs())
    assert set(light_pairs.keys()) == set(SHADCN_COLOR_SLOTS)
    assert set(dark_pairs.keys()) == set(SHADCN_COLOR_SLOTS)

    # Primary should be one of the saturated palette entries (red or blue),
    # not the shadcn default neutral.
    assert light_pairs["primary"] != SHADCN_DEFAULT_LIGHT["primary"]
    # Background should be light (the #ffffff or #f7f7f5 candidates).
    bg_l = float(light_pairs["background"].replace("%", "").split()[-1])
    assert bg_l >= 92.0

    # Font sans contains Inter; font mono detected from JetBrains Mono.
    assert "Inter" in theme.font_sans
    assert theme.font_mono is not None
    assert "JetBrains" in theme.font_mono

    # Radius parsed from 8px to 0.5 rem.
    assert theme.radius_rem == pytest.approx(0.5, abs=1e-6)

    # Metadata stamped.
    assert theme.shadcn_schema_version == 1
    assert theme.resemblio_schema_version == 1
    assert theme.source_url == "https://example.com"

    # Rendered CSS contains the :root and .dark blocks and references
    # the primary value picked above.
    css = render_globals_css(theme)
    assert ":root {" in css
    assert ".dark {" in css
    assert f"--primary: {light_pairs['primary']};" in css

    # Rendered Tailwind config contains the nested colors and references hsl(var(--primary)).
    config = render_tailwind_config(theme)
    assert "hsl(var(--primary))" in config
    assert "darkMode: ['class']" in config


# ---------------------------------------------------------------------- 2


def test_empty_palette_falls_back_to_shadcn_defaults() -> None:
    """An empty manifest yields the shadcn neutral defaults rather than crashing."""
    theme = dtcg_to_shadcn({})

    light_pairs = dict(theme.light.as_ordered_pairs())
    dark_pairs = dict(theme.dark.as_ordered_pairs())

    for slot in SHADCN_COLOR_SLOTS:
        assert light_pairs[slot] == SHADCN_DEFAULT_LIGHT[slot], f"light {slot} drift"
        assert dark_pairs[slot] == SHADCN_DEFAULT_DARK[slot], f"dark {slot} drift"

    # Font sans falls back to a system stack (no project face).
    assert "system-ui" in theme.font_sans
    assert theme.font_mono is None


# ---------------------------------------------------------------------- 3


def test_hex_to_hsl_triple_uses_space_separated_no_wrapper_format() -> None:
    """shadcn HSL format is bare ``H S% L%`` - no ``hsl()``, no commas."""
    # Pure red -> hue 0, saturation 100%, lightness 50%.
    assert hex_to_hsl_triple("#ff0000") == "0.0 100.0% 50.0%"
    # Pure white -> hue 0, saturation 0%, lightness 100%.
    assert hex_to_hsl_triple("#ffffff") == "0.0 0.0% 100.0%"
    # Mid blue.
    triple = hex_to_hsl_triple("#3366cc")
    assert "hsl(" not in triple
    assert "," not in triple
    parts = triple.split()
    assert len(parts) == 3
    assert parts[1].endswith("%") and parts[2].endswith("%")
    # 3-digit hex equivalence: #f00 == #ff0000.
    assert hex_to_hsl_triple("#f00") == hex_to_hsl_triple("#ff0000")


# ---------------------------------------------------------------------- 4


def test_font_family_mapping_detects_monospace_and_sans_separately() -> None:
    """A manifest with one mono family and one sans family produces both vars."""
    manifest = {
        "fontFamily": {
            "display": {"$value": "Playfair Display", "$type": "fontFamily"},
            "code": {"$value": "Fira Code", "$type": "fontFamily"},
        },
    }
    theme = dtcg_to_shadcn(manifest)
    assert "Playfair Display" in theme.font_sans
    assert theme.font_mono is not None
    assert "Fira Code" in theme.font_mono

    # A manifest with ONLY a mono family should still produce font_sans
    # (system fallback) and font_mono.
    mono_only = {
        "fontFamily": {
            "mono": {"$value": "Consolas", "$type": "fontFamily"},
        },
    }
    theme_mono = dtcg_to_shadcn(mono_only)
    assert "system-ui" in theme_mono.font_sans  # system fallback
    assert theme_mono.font_mono is not None
    assert "Consolas" in theme_mono.font_mono


# ---------------------------------------------------------------------- 5


def test_round_trip_stability_converts_to_identical_output() -> None:
    """Calling the converter twice on the same manifest yields identical bytes."""
    manifest = _resemblio_manifest_sample()

    theme_a = dtcg_to_shadcn(manifest)
    theme_b = dtcg_to_shadcn(manifest)
    assert theme_a.model_dump() == theme_b.model_dump()

    css_a = render_globals_css(theme_a)
    css_b = render_globals_css(theme_b)
    assert css_a == css_b

    config_a = render_tailwind_config(theme_a)
    config_b = render_tailwind_config(theme_b)
    assert config_a == config_b


# ---------------------------------------------------------------------- bonus: invalid hex


def test_invalid_color_values_are_skipped_not_fatal() -> None:
    """Non-hex color leaves (rgb, named, gradient) must not crash extraction."""
    manifest = {
        "color": {
            "bg": {"$value": "#ffffff", "$type": "color"},
            "weird": {"$value": "rgb(10, 20, 30)", "$type": "color"},
            "named": {"$value": "cornflowerblue", "$type": "color"},
            "primary": {"$value": "#cc3344", "$type": "color"},
        },
    }
    # Should not raise.
    theme = dtcg_to_shadcn(manifest)
    light_pairs = dict(theme.light.as_ordered_pairs())
    # Primary still picked from the valid hex (red).
    assert light_pairs["primary"] != SHADCN_DEFAULT_LIGHT["primary"]
