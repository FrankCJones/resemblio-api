"""Checkpoint one contract tests for Apple interactive control manifests."""
from __future__ import annotations

from app.apple_system_manifest import component_manifest_slice, load_public_manifest


CHECKPOINT_ONE_FAMILIES = ("buttons", "links", "icon-buttons", "segmented-controls")


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
    """Public compatibility slices retain family identity and token domains."""
    slices = [component_manifest_slice(family) for family in CHECKPOINT_ONE_FAMILIES]

    assert all(component is not None for component in slices)
    assert [component["family_id"] for component in slices if component] == list(CHECKPOINT_ONE_FAMILIES)
    assert len({tuple(component["token_references"]) for component in slices if component}) == 1
    assert all("tokens.color.system" in component["token_references"] for component in slices if component)
