"""Checkpoint one contract tests for Apple content and feedback manifests."""
from __future__ import annotations

from app.apple_system_manifest import component_manifest_slice, load_public_manifest


CHECKPOINT_ONE_FAMILIES = (
    "product-cards",
    "editorial-cards",
    "comparison-tiles",
    "promo-panels",
    "empty-states",
)
EXPECTED_STATES = {
    "product-cards": ["default"],
    "editorial-cards": ["default"],
    "comparison-tiles": ["default"],
    "promo-panels": ["default"],
    "empty-states": ["default", "empty"],
}
EXPECTED_TOKEN_REFERENCES = {
    "tokens.color.system",
    "tokens.layout.system",
    "tokens.motion.system",
    "tokens.spacing.system",
    "tokens.typography.system",
}


def test_checkpoint_one_families_keep_distinct_manifest_records() -> None:
    """Each checkpoint-one family keeps one canonical component identity and route."""
    records = {record["slug"]: record for record in load_public_manifest()["records"]}

    for family in CHECKPOINT_ONE_FAMILIES:
        record = records[family]
        assert record["record_id"] == f"component:{family}"
        assert record["classification"] == "component"
        assert record["route"] == f"/library/apple/{family}/"
        assert record["facets"]["family"] == family


def test_checkpoint_one_compatibility_slices_are_family_specific() -> None:
    """Compatibility slices retain the five canonical family identities."""
    slices = [component_manifest_slice(family) for family in CHECKPOINT_ONE_FAMILIES]

    assert all(component is not None for component in slices)
    assert [component["family_id"] for component in slices if component] == list(CHECKPOINT_ONE_FAMILIES)


def test_checkpoint_one_slices_pin_states_and_foundation_tokens() -> None:
    """Current evidence-derived states and shared foundation tokens cannot drift."""
    for family in CHECKPOINT_ONE_FAMILIES:
        component = component_manifest_slice(family)
        assert component is not None
        assert component["states"] == EXPECTED_STATES[family]
        assert set(component["token_references"]) == EXPECTED_TOKEN_REFERENCES
