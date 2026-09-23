"""Validate neutral component intelligence before it reaches a public API.

The module owns the runtime contract for ``resemblio_component_manifest_v1``.
It deliberately accepts plain JSON-shaped mappings because manifests can arrive
from a reviewed local registry or a future indexer payload. Raw corpus evidence
is never returned from this boundary.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Final, TypedDict, cast


RESEMBLIO_COMPONENT_MANIFEST_SCHEMA_VERSION: Final = "resemblio_component_manifest_v1"


class ComponentManifestError(ValueError):
    """Raised when a manifest is incomplete, malformed, or source-identifying."""


class ComponentFacetNotApplicable(TypedDict):
    """Explicit, documented outcome for a facet that does not apply."""

    status: str
    reason: str


class ComponentManifest(TypedDict):
    """JSON-safe public contract for one neutral component family."""

    schema_version: str
    family_id: str
    role: str
    anatomy: list[dict[str, Any]]
    slots: list[dict[str, Any]] | ComponentFacetNotApplicable
    variants: list[dict[str, Any]] | ComponentFacetNotApplicable
    states: list[str] | ComponentFacetNotApplicable
    semantics: list[str] | ComponentFacetNotApplicable
    accessibility: dict[str, Any] | ComponentFacetNotApplicable
    keyboard: list[str] | ComponentFacetNotApplicable
    responsive: list[str] | ComponentFacetNotApplicable
    motion: dict[str, Any] | ComponentFacetNotApplicable
    relationships: dict[str, list[str]]
    token_references: list[str]
    implementation_constraints: list[str]


_SOURCE_EXPRESSION_RE: Final = re.compile(
    r"(?:\bapple\b|\biphone\b|\bipad\b|\bmacos\b|\bresemblio://|\bdrl[-_/]|"
    r"data-rs-source|https?://|www\.|<[^>]+>|@font-face|font-family|[{};])",
    re.IGNORECASE,
)
_TOKEN_REFERENCE_RE: Final = re.compile(r"^tokens\.[a-z0-9][a-z0-9_.-]*$", re.IGNORECASE)
_MOTION_KEYS: Final[frozenset[str]] = frozenset(
    {"trigger", "duration_ms", "easing", "reduced_motion", "loop"}
)
_RELATIONSHIP_KEYS: Final[frozenset[str]] = frozenset(
    {"foundations", "compositions", "dependent_components"}
)


def _require_safe_string(value: object, label: str) -> str:
    """Return a nonempty neutral string or raise without echoing unsafe text."""
    if not isinstance(value, str) or not value.strip():
        raise ComponentManifestError(f"{label} must be a nonempty string")
    cleaned = value.strip()
    if _SOURCE_EXPRESSION_RE.search(cleaned):
        raise ComponentManifestError(f"{label} contains source-identifying or raw expression content")
    return cleaned


def _require_safe_string_list(value: object, label: str, *, allow_empty: bool = False) -> list[str]:
    """Validate a JSON string list while preserving explicit empty-list policy."""
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ComponentManifestError(f"{label} must be a populated list")
    return [_require_safe_string(item, f"{label} entry") for item in value]


def _not_applicable(value: object, label: str) -> ComponentFacetNotApplicable | None:
    """Return a validated explicit non-applicable facet, otherwise ``None``."""
    if not isinstance(value, Mapping) or value.get("status") != "not_applicable":
        return None
    if set(value) != {"status", "reason"}:
        raise ComponentManifestError(f"{label} not_applicable facet has unsupported keys")
    return {"status": "not_applicable", "reason": _require_safe_string(value.get("reason"), f"{label} reason")}


def _require_named_items(value: object, label: str, *, variant: bool = False) -> list[dict[str, Any]]:
    """Validate populated anatomy, slot, or variant objects with useful values."""
    if not isinstance(value, list) or not value:
        raise ComponentManifestError(f"{label} must be a populated list")
    items: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ComponentManifestError(f"{label} entry must be an object")
        expected = {"name", "values"} if variant else {"name", "required"}
        if set(raw) != expected:
            raise ComponentManifestError(f"{label} entry has unsupported keys")
        item: dict[str, Any] = {"name": _require_safe_string(raw.get("name"), f"{label} name")}
        if variant:
            item["values"] = _require_safe_string_list(raw.get("values"), f"{label} values")
        else:
            if not isinstance(raw.get("required"), bool):
                raise ComponentManifestError(f"{label} required must be boolean")
            item["required"] = raw["required"]
        items.append(item)
    return items


def _require_list_facet(value: object, label: str) -> list[str] | ComponentFacetNotApplicable:
    """Validate a populated list facet or an explicit non-applicable outcome."""
    return _not_applicable(value, label) or _require_safe_string_list(value, label)


def _require_accessibility(value: object) -> dict[str, Any] | ComponentFacetNotApplicable:
    """Validate semantic role and nonempty accessibility requirements."""
    not_applicable = _not_applicable(value, "accessibility")
    if not_applicable:
        return not_applicable
    if not isinstance(value, Mapping) or set(value) != {"semantic_role", "requirements"}:
        raise ComponentManifestError("accessibility must contain semantic_role and requirements")
    return {
        "semantic_role": _require_safe_string(value.get("semantic_role"), "accessibility semantic_role"),
        "requirements": _require_safe_string_list(value.get("requirements"), "accessibility requirements"),
    }


def _require_motion(value: object) -> dict[str, Any] | ComponentFacetNotApplicable:
    """Validate applicable motion detail, including safe reduced-motion behavior."""
    not_applicable = _not_applicable(value, "motion")
    if not_applicable:
        return not_applicable
    if not isinstance(value, Mapping) or set(value) != _MOTION_KEYS:
        raise ComponentManifestError("motion must contain trigger, duration_ms, easing, reduced_motion, and loop")
    duration = value.get("duration_ms")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
        raise ComponentManifestError("motion duration_ms must be a positive integer")
    return {
        "trigger": _require_safe_string(value.get("trigger"), "motion trigger"),
        "duration_ms": duration,
        "easing": _require_safe_string(value.get("easing"), "motion easing"),
        "reduced_motion": _require_safe_string(value.get("reduced_motion"), "motion reduced_motion"),
        "loop": _require_safe_string(value.get("loop"), "motion loop"),
    }


def _require_relationships(value: object) -> dict[str, list[str]]:
    """Validate neutral relationships without permitting implicit empty defaults."""
    if not isinstance(value, Mapping) or set(value) != _RELATIONSHIP_KEYS:
        raise ComponentManifestError("relationships must contain foundations, compositions, and dependent_components")
    foundations = _require_safe_string_list(value.get("foundations"), "relationships foundations")
    return {
        "foundations": foundations,
        "compositions": _require_safe_string_list(value.get("compositions"), "relationships compositions", allow_empty=True),
        "dependent_components": _require_safe_string_list(
            value.get("dependent_components"), "relationships dependent_components", allow_empty=True
        ),
    }


def validate_component_manifest(raw_manifest: Mapping[str, object]) -> ComponentManifest:
    """Validate and deep-copy one safe manifest before API serialization.

    The function rejects raw source expression, incomplete facets, and unknown
    keys. Returning a copy prevents downstream callers from mutating the
    validated object before it is serialized.
    """
    required_keys = {
        "schema_version", "family_id", "role", "anatomy", "slots", "variants", "states",
        "semantics", "accessibility", "keyboard", "responsive", "motion", "relationships",
        "token_references", "implementation_constraints",
    }
    if set(raw_manifest) != required_keys:
        raise ComponentManifestError("component manifest has missing or unsupported keys")
    if raw_manifest.get("schema_version") != RESEMBLIO_COMPONENT_MANIFEST_SCHEMA_VERSION:
        raise ComponentManifestError("component manifest schema_version is unsupported")
    token_references = _require_safe_string_list(raw_manifest.get("token_references"), "token_references")
    if any(_TOKEN_REFERENCE_RE.fullmatch(reference) is None for reference in token_references):
        raise ComponentManifestError("token_references must use neutral tokens.* references")
    manifest: ComponentManifest = {
        "schema_version": RESEMBLIO_COMPONENT_MANIFEST_SCHEMA_VERSION,
        "family_id": _require_safe_string(raw_manifest.get("family_id"), "family_id"),
        "role": _require_safe_string(raw_manifest.get("role"), "role"),
        "anatomy": _require_named_items(raw_manifest.get("anatomy"), "anatomy"),
        "slots": _not_applicable(raw_manifest.get("slots"), "slots") or _require_named_items(raw_manifest.get("slots"), "slots"),
        "variants": _not_applicable(raw_manifest.get("variants"), "variants") or _require_named_items(raw_manifest.get("variants"), "variants", variant=True),
        "states": _require_list_facet(raw_manifest.get("states"), "states"),
        "semantics": _require_list_facet(raw_manifest.get("semantics"), "semantics"),
        "accessibility": _require_accessibility(raw_manifest.get("accessibility")),
        "keyboard": _require_list_facet(raw_manifest.get("keyboard"), "keyboard"),
        "responsive": _require_list_facet(raw_manifest.get("responsive"), "responsive"),
        "motion": _require_motion(raw_manifest.get("motion")),
        "relationships": _require_relationships(raw_manifest.get("relationships")),
        "token_references": token_references,
        "implementation_constraints": _require_safe_string_list(
            raw_manifest.get("implementation_constraints"), "implementation_constraints"
        ),
    }
    return cast(ComponentManifest, deepcopy(manifest))


def component_manifest_from_metadata(metadata: object) -> ComponentManifest | None:
    """Return a validated optional manifest from private page metadata.

    Invalid or absent values fail closed to ``None`` so a bad internal record
    cannot expose unreviewed component intelligence through the public route.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw_manifest = metadata.get("component_manifest")
    if not isinstance(raw_manifest, Mapping):
        return None
    try:
        return validate_component_manifest(raw_manifest)
    except ComponentManifestError:
        return None
