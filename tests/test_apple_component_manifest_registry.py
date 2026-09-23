"""Parity and scrub tests for the local Apple component manifest bridge."""
from __future__ import annotations

import json
from pathlib import Path

from app.apple_component_manifest_registry import (
    APPLE_COMPONENT_MANIFESTS,
    manifest_for_apple_category,
)
from app.library_component_manifest import validate_component_manifest


def test_registry_covers_every_matrix_family_with_local_structural_evidence() -> None:
    """Every approved family has a manifest and both private evidence files."""
    resemblio_root = Path(__file__).resolve().parents[3]
    matrix_path = resemblio_root / "02-prd" / "apple-pilot" / "2026-09-09-apple-component-classification-matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    families = matrix["component_family_records"]
    corpus_root = resemblio_root / "code" / "api" / "_vendored" / "drl_corpus"

    assert set(families) == set(APPLE_COMPONENT_MANIFESTS)
    for family_id, relative_source_path in families.items():
        evidence_path = corpus_root / relative_source_path
        assert (evidence_path / "asset.html").is_file(), family_id
        assert (evidence_path / "tokens.css").is_file(), family_id
        validate_component_manifest(APPLE_COMPONENT_MANIFESTS[family_id])


def test_registry_is_identity_scrubbed_and_forms_alias_resolves() -> None:
    """The public registry contains no raw source expressions or identifiers."""
    encoded = json.dumps(APPLE_COMPONENT_MANIFESTS).lower()
    for forbidden in ("apple", "http", "<", "font-family", "resemblio://", "drl-"):
        assert forbidden not in encoded
    assert manifest_for_apple_category("forms") is not None
    assert manifest_for_apple_category("form-fields") is not None
    assert manifest_for_apple_category(None) is None
