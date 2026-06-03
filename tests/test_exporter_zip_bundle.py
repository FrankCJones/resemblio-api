"""Unit tests for the DTCG -> multi-format ZIP bundle exporter."""
from __future__ import annotations

import json
from io import BytesIO
from zipfile import ZipFile

from app.exporters.artifact import EXPORTER_SCHEMA_VERSION
from app.exporters.zip_bundle import (
    ZipBundleInputs,
    dtcg_to_zip_bundle,
    zip_artifact,
)

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
    },
    "shadow": {
        "sm": {"$value": "0 1px 2px rgb(0 0 0 / 0.1)", "$type": "shadow"},
    },
}

INPUTS = ZipBundleInputs(
    extraction_id=42, source_url="https://example.com", screenshot_bytes=None
)


def _bundle_names(payload: bytes) -> list[str]:
    with ZipFile(BytesIO(payload)) as zf:
        return sorted(zf.namelist())


def test_zip_contains_three_format_files_plus_readme() -> None:
    payload = dtcg_to_zip_bundle(SAMPLE, INPUTS)
    names = _bundle_names(payload)
    assert names == ["README.md", "tailwind.css", "tokens.css", "tokens.json"]


def test_zip_tokens_json_round_trips_to_input() -> None:
    payload = dtcg_to_zip_bundle(SAMPLE, INPUTS)
    with ZipFile(BytesIO(payload)) as zf:
        tokens = json.loads(zf.read("tokens.json").decode("utf-8"))
    assert tokens == SAMPLE


def test_zip_css_entry_includes_root_selector() -> None:
    payload = dtcg_to_zip_bundle(SAMPLE, INPUTS)
    with ZipFile(BytesIO(payload)) as zf:
        css = zf.read("tokens.css").decode("utf-8")
    assert ":root {" in css
    assert "--color-bg: #ffffff;" in css


def test_zip_tailwind_entry_includes_theme_block() -> None:
    payload = dtcg_to_zip_bundle(SAMPLE, INPUTS)
    with ZipFile(BytesIO(payload)) as zf:
        tw = zf.read("tailwind.css").decode("utf-8")
    assert tw.startswith("@theme {")
    assert "--color-accent: #ff3366;" in tw


def test_zip_readme_references_source_url_and_extraction_id() -> None:
    payload = dtcg_to_zip_bundle(SAMPLE, INPUTS)
    with ZipFile(BytesIO(payload)) as zf:
        readme = zf.read("README.md").decode("utf-8")
    assert "https://example.com" in readme
    assert "42" in readme


def test_zip_includes_screenshot_when_provided() -> None:
    screenshot = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    inputs = ZipBundleInputs(
        extraction_id=42,
        source_url="https://example.com",
        screenshot_bytes=screenshot,
    )
    payload = dtcg_to_zip_bundle(SAMPLE, inputs)
    with ZipFile(BytesIO(payload)) as zf:
        assert "screenshot.png" in zf.namelist()
        assert zf.read("screenshot.png") == screenshot


def test_zip_is_byte_deterministic_across_runs() -> None:
    a = dtcg_to_zip_bundle(SAMPLE, INPUTS)
    b = dtcg_to_zip_bundle(SAMPLE, INPUTS)
    assert a == b


def test_zip_artifact_metadata() -> None:
    artifact = zip_artifact(SAMPLE, INPUTS)
    assert artifact.content_type == "application/zip"
    assert artifact.filename == "resemblio-42-bundle.zip"
    assert artifact.schema_version == EXPORTER_SCHEMA_VERSION
