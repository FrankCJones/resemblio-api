"""Checkpoint-two contract tests for Apple feedback and overlay manifests."""
from __future__ import annotations

from app.apple_system_manifest import component_manifest_slice, load_public_manifest


CHECKPOINT_TWO_FAMILIES = (
    "status-indicators",
    "loaders",
    "tooltips",
    "modal-sheet",
)
EXPECTED_STATES = {
    "status-indicators": ["default"],
    "loaders": ["default", "loading"],
    "tooltips": ["default"],
    "modal-sheet": ["default"],
}
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


def test_checkpoint_two_compatibility_slices_are_family_specific() -> None:
    """Compatibility slices retain the four canonical family identities."""
    slices = [component_manifest_slice(family) for family in CHECKPOINT_TWO_FAMILIES]

    assert all(component is not None for component in slices)
    assert [component["family_id"] for component in slices if component] == list(CHECKPOINT_TWO_FAMILIES)


def test_checkpoint_two_slices_pin_states_and_foundation_tokens() -> None:
    """Current evidence-derived states and shared foundation tokens cannot drift."""
    for family in CHECKPOINT_TWO_FAMILIES:
        component = component_manifest_slice(family)
        assert component is not None
        assert component["states"] == EXPECTED_STATES[family]
        assert set(component["token_references"]) == EXPECTED_TOKEN_REFERENCES
