"""Unit tests for the DTCG -> Tailwind v4 ``@theme`` exporter."""
from __future__ import annotations

from app.exporters.artifact import EXPORTER_SCHEMA_VERSION
from app.exporters.tailwind import dtcg_to_tailwind_theme, tailwind_artifact

SAMPLE: dict = {
    "color": {
        "bg": {"$value": "#ffffff", "$type": "color"},
        "accent": {"$value": "#ff3366", "$type": "color"},
    },
    "fontFamily": {
        "body": {"$value": "Inter, sans-serif", "$type": "fontFamily"},
        "display": {"$value": "Playfair Display, serif", "$type": "fontFamily"},
    },
    "dimension": {
        "space-1": {"$value": "4px", "$type": "dimension"},
        "radius-md": {"$value": "8px", "$type": "dimension"},
        "text-lg": {"$value": "1.125rem", "$type": "dimension"},
    },
    "shadow": {
        "sm": {"$value": "0 1px 2px rgb(0 0 0 / 0.1)", "$type": "shadow"},
    },
    "duration": {
        "fast": {"$value": "120ms", "$type": "duration"},
    },
}


def test_tailwind_wraps_output_in_theme_block() -> None:
    out = dtcg_to_tailwind_theme(SAMPLE)
    assert out.startswith("@theme {")
    assert out.rstrip().endswith("}")


def test_tailwind_emits_color_namespace() -> None:
    out = dtcg_to_tailwind_theme(SAMPLE)
    assert "--color-bg: #ffffff;" in out
    assert "--color-accent: #ff3366;" in out


def test_tailwind_emits_font_namespace() -> None:
    out = dtcg_to_tailwind_theme(SAMPLE)
    assert "--font-body: Inter, sans-serif;" in out
    assert "--font-display: Playfair Display, serif;" in out


def test_tailwind_routes_dimension_to_spacing_radius_text() -> None:
    out = dtcg_to_tailwind_theme(SAMPLE)
    assert "--spacing-space-1: 4px;" in out
    assert "--radius-radius-md: 8px;" in out
    assert "--text-text-lg: 1.125rem;" in out


def test_tailwind_emits_shadow_namespace() -> None:
    out = dtcg_to_tailwind_theme(SAMPLE)
    assert "--shadow-sm: 0 1px 2px rgb(0 0 0 / 0.1);" in out


def test_tailwind_omits_unmapped_groups() -> None:
    # Duration has no Tailwind v4 first-class namespace; we omit by design.
    out = dtcg_to_tailwind_theme(SAMPLE)
    assert "duration" not in out
    assert "120ms" not in out


def test_tailwind_empty_input_is_valid() -> None:
    out = dtcg_to_tailwind_theme({})
    assert out.startswith("@theme {")
    assert "}" in out


def test_tailwind_artifact_metadata() -> None:
    artifact = tailwind_artifact(99, SAMPLE)
    assert artifact.content_type == "text/css; charset=utf-8"
    assert artifact.filename == "resemblio-99-tailwind.css"
    assert artifact.schema_version == EXPORTER_SCHEMA_VERSION
