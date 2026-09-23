"""Deterministic compiler, mutation, compatibility, and endpoint tests."""
from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.apple_component_manifest_registry import APPLE_COMPONENT_MANIFESTS
from app.apple_system_manifest import (
    ARTIFACT_ID,
    DEFAULT_CORPUS_ROOT,
    DEFAULT_PRIVATE_LEDGER_PATH,
    DEFAULT_PUBLIC_ARTIFACT_PATH,
    SCHEMA_VERSION,
    ManifestCompileError,
    ManifestValidationError,
    artifact_sha256,
    canonical_json_bytes,
    compile_apple_system_manifest,
    component_manifest_slice,
    load_public_manifest,
    normalize_inventory,
    readiness_projection,
    sha256_bytes,
    validate_private_ledger,
    validate_public_manifest,
)
from app.routes.library import get_apple_system_manifest


def _manifest() -> dict[str, Any]:
    """Return a mutable copy of the checked-in canonical artifact."""
    return deepcopy(dict(load_public_manifest()))


def _resign(manifest: dict[str, Any]) -> None:
    """Recompute the outer artifact digest after a deliberate mutation."""
    manifest["artifact_sha256"] = artifact_sha256(manifest)


def _rebind_record(record: dict[str, Any]) -> None:
    """Recompute one record facet binding after a deliberate mutation."""
    record["facet_evidence_sha256"] = sha256_bytes(canonical_json_bytes({
        "evidence_sha256": record["evidence"]["evidence_sha256"],
        "facets": record["facets"],
        "record_id": record["record_id"],
    }))


def _component(manifest: dict[str, Any], slug: str) -> dict[str, Any]:
    """Return one mutable component record from a test manifest."""
    return next(record for record in manifest["records"] if record["record_id"] == f"component:{slug}")


def test_checked_in_products_match_clean_compiler_bytes() -> None:
    """Two clean compiles and both checked-in products are byte-identical."""
    first = compile_apple_system_manifest()
    second = compile_apple_system_manifest()

    assert canonical_json_bytes(first.public_manifest) == canonical_json_bytes(second.public_manifest)
    assert canonical_json_bytes(first.private_ledger) == canonical_json_bytes(second.private_ledger)
    assert DEFAULT_PUBLIC_ARTIFACT_PATH.read_bytes() == canonical_json_bytes(first.public_manifest)
    assert DEFAULT_PRIVATE_LEDGER_PATH.read_bytes() == canonical_json_bytes(first.private_ledger)


def test_canonical_inventory_and_private_resolution_are_exact() -> None:
    """The artifact has 41 stable IDs and every opaque ID resolves privately."""
    result = compile_apple_system_manifest()
    manifest = result.public_manifest

    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["artifact_id"] == ARTIFACT_ID
    assert manifest["record_count"] == 41
    assert manifest["breakdown"] == {"foundations": 11, "components": 17, "compositions": 13}
    assert len({record["record_id"] for record in manifest["records"]}) == 41
    assert len({record["evidence"]["evidence_id"] for record in manifest["records"]}) == 41
    validate_private_ledger(result.private_ledger, manifest)


def test_shuffled_matrix_containers_produce_identical_artifact(tmp_path: Path) -> None:
    """Input container order cannot change normalized inventory or output bytes."""
    matrix_path = Path(__file__).resolve().parents[3] / "02-prd" / "apple-pilot" / "2026-09-09-apple-component-classification-matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    shuffled = deepcopy(matrix)
    shuffled["records"] = list(reversed(shuffled["records"]))
    shuffled["component_family_records"] = dict(reversed(list(shuffled["component_family_records"].items())))
    shuffled["composition_records"] = dict(reversed(list(shuffled["composition_records"].items())))
    shuffled_path = tmp_path / "shuffled-matrix.json"
    shuffled_path.write_text(json.dumps(shuffled), encoding="utf-8")

    assert normalize_inventory(matrix) == normalize_inventory(shuffled)
    canonical = compile_apple_system_manifest(matrix_path=matrix_path)
    reordered = compile_apple_system_manifest(matrix_path=shuffled_path)
    assert canonical_json_bytes(canonical.public_manifest) == canonical_json_bytes(reordered.public_manifest)
    assert canonical_json_bytes(canonical.private_ledger) == canonical_json_bytes(reordered.private_ledger)


def test_all_component_facets_are_evidence_bound_and_family_specific() -> None:
    """No two component records share unchanged generic facets or bindings."""
    components = [record for record in load_public_manifest()["records"] if record["classification"] == "component"]

    assert len(components) == 17
    assert len({canonical_json_bytes(record["facets"]) for record in components}) == 17
    assert len({record["facet_evidence_sha256"] for record in components}) == 17


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "renamed", "substitution"])
def test_validator_rejects_inventory_mutations(mutation: str) -> None:
    """Missing, extra, duplicate, renamed, and still-41 substitutions fail."""
    manifest = _manifest()
    if mutation == "missing":
        manifest["records"].pop()
        manifest["record_count"] = 40
    elif mutation == "extra":
        manifest["records"].append(deepcopy(manifest["records"][-1]))
        manifest["records"][-1]["record_id"] = "component:invented"
        manifest["record_count"] = 42
    elif mutation == "duplicate":
        manifest["records"][-1] = deepcopy(manifest["records"][0])
    elif mutation == "renamed":
        manifest["records"][0]["record_id"] = "component:renamed"
        _rebind_record(manifest["records"][0])
    else:
        manifest["records"][0]["record_id"] = "component:substitute"
        _rebind_record(manifest["records"][0])
    _resign(manifest)

    with pytest.raises(ManifestValidationError):
        validate_public_manifest(manifest)


def test_validator_rejects_tampered_body_with_unchanged_digest() -> None:
    """Any artifact body edit fails when the outer digest is not updated."""
    manifest = _manifest()
    manifest["records"][0]["readiness"]["next_phase"] = "tampered"

    with pytest.raises(ManifestValidationError, match="artifact body digest"):
        validate_public_manifest(manifest)


@pytest.mark.parametrize("mutation", ["empty_facet", "empty_reason", "copied_family", "malformed_motion", "missing_state"])
def test_validator_rejects_facet_mutations(mutation: str) -> None:
    """Applicable facets, explicit gaps, motion, states, and family binding fail closed."""
    manifest = _manifest()
    button = _component(manifest, "buttons")
    if mutation == "empty_facet":
        button["facets"]["anatomy"] = []
    elif mutation == "empty_reason":
        foundation = next(record for record in manifest["records"] if record["classification"] == "foundation")
        foundation["facets"]["component_anatomy"]["reason"] = ""
        _rebind_record(foundation)
    elif mutation == "copied_family":
        links = _component(manifest, "links")
        links["facets"] = deepcopy(button["facets"])
        links["facet_evidence_sha256"] = button["facet_evidence_sha256"]
    elif mutation == "malformed_motion":
        button["facets"]["motion"] = {"duration_ms": 0}
    else:
        button["facets"]["states"] = ["hover"]
    if mutation not in {"empty_reason", "copied_family"}:
        _rebind_record(button)
    _resign(manifest)

    with pytest.raises(ManifestValidationError):
        validate_public_manifest(manifest)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("relationship", "relationship"),
        ("alias", "alias"),
        ("path", "private"),
        ("url", "private"),
        ("markup", "private"),
        ("selector", "private"),
        ("marker", "private"),
        ("identity", "proprietary"),
        ("font", "proprietary"),
    ],
)
def test_validator_rejects_relationship_alias_and_leak_mutations(mutation: str, expected: str) -> None:
    """Broken references, collisions, and every forbidden public leak fail."""
    manifest = _manifest()
    button = _component(manifest, "buttons")
    if mutation == "relationship":
        button["relationships"]["foundations"] = ["foundation:invented"]
    elif mutation == "alias":
        button["aliases"] = ["/library/apple/form-fields/"]
    else:
        leaks = {
            "path": "assets/atoms/private",
            "url": "https://private.example",
            "markup": "<button>copied</button>",
            "selector": "data-rs-source",
            "marker": "drl-bootstrap",
            "identity": "iPhone control",
            "font": "SF Pro Display",
        }
        button["facets"]["family"] = leaks[mutation]
        _rebind_record(button)
    _resign(manifest)

    with pytest.raises(ManifestValidationError, match=expected):
        validate_public_manifest(manifest)


def test_compiler_rejects_one_byte_evidence_mutation(tmp_path: Path) -> None:
    """A one-byte vendored evidence change stops compilation before output."""
    corpus_root = tmp_path / "drl_corpus"
    shutil.copytree(DEFAULT_CORPUS_ROOT, corpus_root)
    evidence_path = corpus_root / "assets" / "atoms" / "buttons" / "apple-button-system-001" / "asset.html"
    evidence_path.write_bytes(evidence_path.read_bytes() + b"x")

    with pytest.raises(ManifestCompileError, match="hash does not match"):
        compile_apple_system_manifest(corpus_root=corpus_root)


def test_missing_evidence_file_stops_compilation(tmp_path: Path) -> None:
    """A missing evidence file stops compilation before output."""
    corpus_root = tmp_path / "drl_corpus"
    shutil.copytree(DEFAULT_CORPUS_ROOT, corpus_root)
    evidence_path = corpus_root / "assets" / "atoms" / "buttons" / "apple-button-system-001" / "asset.html"
    evidence_path.unlink()

    with pytest.raises(ManifestCompileError, match="file is missing"):
        compile_apple_system_manifest(corpus_root=corpus_root)


def test_compatibility_slices_and_readiness_derive_from_artifact() -> None:
    """Legacy component and readiness projections have no separate inventory."""
    assert set(APPLE_COMPONENT_MANIFESTS) == {
        record["slug"] for record in load_public_manifest()["records"] if record["classification"] == "component"
    }
    assert component_manifest_slice("forms") == component_manifest_slice("form-fields")
    assert component_manifest_slice("settings") is None
    assert set(readiness_projection()) == {record["record_id"] for record in load_public_manifest()["records"]}


def test_manifest_endpoint_returns_canonical_artifact_and_never_private_ledger() -> None:
    """The read-only endpoint wraps exact public data and excludes private paths."""
    response = get_apple_system_manifest()
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body == {"schema_version": 2, "data": load_public_manifest()}
    encoded = response.body.decode("utf-8").lower()
    assert "source_path" not in encoded
    assert "asset_html_path" not in encoded
    assert "tokens_css_path" not in encoded

