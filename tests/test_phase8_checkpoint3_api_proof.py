"""Focused tests for the Phase 8 checkpoint-three API proof generator."""
from __future__ import annotations

import json

from app.apple_export_product import build_export, validate_export
from scripts.generate_phase8_checkpoint3_api_proof import (
    PROOF_CASES,
    PROOF_SCHEMA_VERSION,
    build_api_proof,
    canonical_proof_bytes,
)


def test_proof_contains_every_required_selection_and_exact_api_values() -> None:
    """Every named case must contain the unmodified production builder output."""
    proof = build_api_proof()
    assert proof["schema_version"] == PROOF_SCHEMA_VERSION
    assert proof["source"] == "api-owned-build-export"
    assert [case["name"] for case in proof["cases"]] == [
        "complete",
        "classification-foundation",
        "classification-component",
        "classification-composition",
        "record-component-buttons",
    ]

    for spec, case in zip(PROOF_CASES, proof["cases"], strict=True):
        expected = build_export(spec.api_selection)
        assert case["api_selection"] == spec.api_selection
        assert case["mcp_arguments"] == spec.mcp_arguments
        assert case["tokens"] == expected["tokens"]
        assert case["manifest"] == expected["manifest"]
        validate_export({"tokens": case["tokens"], "manifest": case["manifest"]})


def test_mcp_arguments_are_normalized_and_match_manifest_selection() -> None:
    """MCP arguments must represent the same normalized selection as the API."""
    cases = {case["name"]: case for case in build_api_proof()["cases"]}
    assert cases["complete"]["mcp_arguments"] == {"selection": "complete"}
    for classification in ("foundation", "component", "composition"):
        case = cases[f"classification-{classification}"]
        assert case["mcp_arguments"] == {
            "selection": "classification",
            "classification": classification,
        }
        assert case["manifest"]["selection"]["classification"] == classification
    record_case = cases["record-component-buttons"]
    assert record_case["mcp_arguments"] == {
        "selection": "record",
        "record_ids": ["component:buttons"],
    }
    assert record_case["manifest"]["selection"]["selected_record_ids"] == [
        "component:buttons"
    ]


def test_canonical_proof_bytes_are_repeatable_and_round_trip() -> None:
    """Equivalent builds serialize to byte-identical valid JSON with one LF."""
    first = canonical_proof_bytes(build_api_proof())
    second = canonical_proof_bytes(build_api_proof())
    assert first == second
    assert first.endswith(b"\n")
    assert not first.endswith(b"\n\n")
    assert json.loads(first) == build_api_proof()
