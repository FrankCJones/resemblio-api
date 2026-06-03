"""Unit tests for the DTCG -> CSS custom-properties exporter."""
from __future__ import annotations

from app.exporters.artifact import EXPORTER_SCHEMA_VERSION
from app.exporters.css import css_artifact, dtcg_to_css

SAMPLE: dict = {
    "schema_version": 1,
    "color": {
        "bg": {"$value": "#ffffff", "$type": "color"},
        "accent": {"$value": "#ff3366", "$type": "color"},
    },
    "fontFamily": {
        "body": {"$value": "Inter, sans-serif", "$type": "fontFamily"},
    },
    "dimension": {
        "space-1": {"$value": "4px", "$type": "dimension"},
        "radius-md": {"$value": "8px", "$type": "dimension"},
    },
    "shadow": {
        "sm": {"$value": "0 1px 2px rgb(0 0 0 / 0.1)", "$type": "shadow"},
    },
}


def test_css_starts_with_root_selector() -> None:
    out = dtcg_to_css(SAMPLE)
    assert out.startswith(":root {")
    assert out.rstrip().endswith("}")


def test_css_emits_color_properties() -> None:
    out = dtcg_to_css(SAMPLE)
    assert "--color-bg: #ffffff;" in out
    assert "--color-accent: #ff3366;" in out


def test_css_kebab_cases_camel_group_names() -> None:
    out = dtcg_to_css(SAMPLE)
    assert "--font-family-body: Inter, sans-serif;" in out
    assert "--fontFamily" not in out


def test_css_emits_dimension_and_shadow() -> None:
    out = dtcg_to_css(SAMPLE)
    assert "--dimension-space-1: 4px;" in out
    assert "--dimension-radius-md: 8px;" in out
    assert "--shadow-sm: 0 1px 2px rgb(0 0 0 / 0.1);" in out


def test_css_skips_schema_version_sibling() -> None:
    out = dtcg_to_css(SAMPLE)
    assert "--schema-version" not in out


def test_css_is_deterministic_sorted() -> None:
    a = dtcg_to_css(SAMPLE)
    b = dtcg_to_css(SAMPLE)
    assert a == b
    # accent sorts before bg alphabetically inside the color group.
    assert a.index("--color-accent") < a.index("--color-bg")


def test_css_empty_input_returns_empty_root_block() -> None:
    out = dtcg_to_css({})
    assert out.startswith(":root {")
    assert "--" not in out


def test_css_artifact_metadata() -> None:
    artifact = css_artifact(7, SAMPLE)
    assert artifact.content_type == "text/css; charset=utf-8"
    assert artifact.filename == "resemblio-7-tokens.css"
    assert artifact.schema_version == EXPORTER_SCHEMA_VERSION
