"""Checkpoint two contract tests for Apple interactive control manifests."""
from __future__ import annotations

from app.apple_component_manifest_registry import manifest_for_apple_category
from app.apple_system_manifest import component_manifest_slice, load_public_manifest


CHECKPOINT_TWO_FAMILIES = ("inputs", "search", "selection-controls", "forms")
EXPECTED_TOKEN_REFERENCES = {
    "tokens.color.system",
    "tokens.layout.system",
    "tokens.motion.system",
    "tokens.spacing.system",
    "tokens.typography.system",
}


def test_checkpoint_two_families_keep_distinct_manifest_records() -> None:
    """Each checkpoint-two family keeps one canonical component identity and route."""
    records = {record["slug"]: record for record in load_public_manifest()["records"]}

    for family in CHECKPOINT_TWO_FAMILIES:
        record = records[family]
        assert record["record_id"] == f"component:{family}"
        assert record["classification"] == "component"
        assert record["route"] == f"/library/apple/{family}/"
        assert record["facets"]["family"] == family


def test_checkpoint_two_compatibility_slices_preserve_evidence_claims() -> None:
    """Compatibility slices retain family identity, tokens, and evidence-derived states."""
    slices = [component_manifest_slice(family) for family in CHECKPOINT_TWO_FAMILIES]

    assert all(component is not None for component in slices)
    assert [component["family_id"] for component in slices if component] == list(CHECKPOINT_TWO_FAMILIES)
    assert all(set(component["token_references"]) == EXPECTED_TOKEN_REFERENCES for component in slices if component)
    states = {family: component_manifest_slice(family)["states"] for family in CHECKPOINT_TWO_FAMILIES}  # type: ignore[index]
    assert states["search"] == ["default", "loading"]
    assert all(states[family] == ["default"] for family in ("inputs", "selection-controls", "forms"))


def test_forms_alias_resolves_to_the_canonical_forms_slice() -> None:
    """The historical form-fields alias cannot drift from the forms family."""
    assert component_manifest_slice("form-fields") == component_manifest_slice("forms")
    assert manifest_for_apple_category("form-fields") == manifest_for_apple_category("forms")
