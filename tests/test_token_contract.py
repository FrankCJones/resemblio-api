"""Tests for ``extractor.token_contract.BRAND_TOKEN_CONTRACT`` shape.

Path C Phase 2 (per CTO sign-off
``projects/OptSus Team/cto-reviews/2026-06-03-resemblio-path-c-phase2-contract-signoff.md``):
the brand-token contract is data, not code. Every slot must carry the four
fields the downstream emitter + derivation modules depend on. These tests
pin the shape so a drive-by edit that drops a key never silently breaks
rendering or Phase 3 derivation.
"""
from __future__ import annotations

from extractor.token_contract import (
    BRAND_TOKEN_CONTRACT,
    TOKEN_CONTRACT_SCHEMA_VERSION,
    all_slot_names,
    slot_default,
    slots_for_group,
)


REQUIRED_SLOT_FIELDS = ("default", "source_field", "component_group", "docs")


def test_schema_version_is_present_and_matches_sentinel() -> None:
    """The contract carries a ``schema_version`` so downstream consumers can detect shape drift."""
    assert BRAND_TOKEN_CONTRACT["schema_version"] == TOKEN_CONTRACT_SCHEMA_VERSION
    assert isinstance(BRAND_TOKEN_CONTRACT["schema_version"], str)
    assert BRAND_TOKEN_CONTRACT["schema_version"].startswith("token_contract_v")


def test_every_slot_carries_all_required_fields() -> None:
    """Each ``TokenSlot`` must have default + source_field + component_group + docs."""
    missing: list[tuple[str, str]] = []
    for slot_name, slot in BRAND_TOKEN_CONTRACT["slots"].items():
        for field in REQUIRED_SLOT_FIELDS:
            if field not in slot:
                missing.append((slot_name, field))
    assert missing == [], f"slots missing fields: {missing}"


def test_every_slot_default_is_a_nonempty_string() -> None:
    """Defaults double as ``var()`` fallbacks in templates.py; empties would silently break rendering."""
    empties = [
        slot_name
        for slot_name, slot in BRAND_TOKEN_CONTRACT["slots"].items()
        if not isinstance(slot["default"], str) or not slot["default"].strip()
    ]
    assert empties == [], f"slots with empty defaults: {empties}"


def test_every_slot_docs_is_nonempty() -> None:
    """The docs string feeds the inventory generator; empty docs degrade the discoverability surface."""
    empties = [
        slot_name
        for slot_name, slot in BRAND_TOKEN_CONTRACT["slots"].items()
        if not isinstance(slot["docs"], str) or not slot["docs"].strip()
    ]
    assert empties == [], f"slots with empty docs: {empties}"


def test_no_duplicate_slot_names() -> None:
    """Python dict literals silently last-wins on duplicates; assert keys round-trip count."""
    names = list(BRAND_TOKEN_CONTRACT["slots"].keys())
    assert len(names) == len(set(names)), "duplicate slot names in contract"


def test_slot_names_are_kebab_case() -> None:
    """Every slot name normalizes to ``--<name>`` so kebab-case is the locked spelling."""
    offenders = [
        name
        for name in BRAND_TOKEN_CONTRACT["slots"]
        if "_" in name or not name.islower() or name.startswith("-") or name.endswith("-")
    ]
    assert offenders == [], f"non-kebab-case slot names: {offenders}"


def test_all_slot_names_returns_sorted_tuple() -> None:
    """Determinism guard: ``all_slot_names`` is the inventory snapshot."""
    names = all_slot_names()
    assert isinstance(names, tuple)
    assert list(names) == sorted(names)
    assert len(names) == len(BRAND_TOKEN_CONTRACT["slots"])


def test_slot_default_lookup_returns_contract_value() -> None:
    """``slot_default`` is the single-call API for var() fallback resolution."""
    assert slot_default("ds-bg") == BRAND_TOKEN_CONTRACT["slots"]["ds-bg"]["default"]


def test_slots_for_group_returns_only_matching_group() -> None:
    """``slots_for_group`` is what Phase 3 derivation modules call to scope their output."""
    button_slots = slots_for_group("button")
    assert len(button_slots) > 0
    for name in button_slots:
        assert BRAND_TOKEN_CONTRACT["slots"][name]["component_group"] == "button"


def test_contract_covers_the_eight_required_component_groups() -> None:
    """The contract must cover the eight component groups Phase 3 derivation modules target."""
    expected = {
        "spacing", "radius", "button", "card", "badge",
        "input", "section", "layout", "typography",
        "motion", "shadow", "color",
    }
    actual = {
        slot["component_group"]
        for slot in BRAND_TOKEN_CONTRACT["slots"].values()
    }
    missing = expected - actual
    assert missing == set(), f"component groups absent from contract: {missing}"
