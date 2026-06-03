"""Unit tests for the DTCG canonical-bytes exporter."""
from __future__ import annotations

import json

from app.exporters.artifact import EXPORTER_SCHEMA_VERSION
from app.exporters.dtcg import dtcg_artifact, dtcg_to_canonical_bytes

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


def test_dtcg_bytes_round_trip_parses_to_input() -> None:
    payload = json.loads(dtcg_to_canonical_bytes(SAMPLE).decode("utf-8"))
    assert payload == SAMPLE


def test_dtcg_bytes_are_deterministic_across_runs() -> None:
    assert dtcg_to_canonical_bytes(SAMPLE) == dtcg_to_canonical_bytes(SAMPLE)


def test_dtcg_bytes_sort_top_level_keys() -> None:
    text = dtcg_to_canonical_bytes(SAMPLE).decode("utf-8")
    # color sorts before dimension sorts before fontFamily sorts before
    # schema_version sorts before shadow when keys are sorted.
    assert text.index('"color"') < text.index('"dimension"')
    assert text.index('"dimension"') < text.index('"fontFamily"')


def test_dtcg_bytes_preserve_non_ascii_glyphs() -> None:
    payload = {"color": {"acentué": {"$value": "#000", "$type": "color"}}}
    out = dtcg_to_canonical_bytes(payload).decode("utf-8")
    assert "acentué" in out


def test_dtcg_artifact_metadata() -> None:
    artifact = dtcg_artifact(42, SAMPLE)
    assert artifact.content_type == "application/json"
    assert artifact.filename == "resemblio-42-tokens.json"
    assert artifact.schema_version == EXPORTER_SCHEMA_VERSION


def test_dtcg_bytes_handle_empty_payload() -> None:
    assert dtcg_to_canonical_bytes({}) == b"{}"


def test_dtcg_pretty_printed_for_human_readability() -> None:
    text = dtcg_to_canonical_bytes(SAMPLE).decode("utf-8")
    # Pretty-printed output has indented lines (vs minified one-liner).
    assert "\n" in text
    assert "  " in text
