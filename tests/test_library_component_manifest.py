"""Pure-data tests for the source-safe component manifest API boundary."""
from __future__ import annotations

from copy import deepcopy

import pytest

from app.library_component_manifest import (
    ComponentManifestError,
    RESEMBLIO_COMPONENT_MANIFEST_SCHEMA_VERSION,
    component_manifest_from_metadata,
    validate_component_manifest,
)


def _manifest() -> dict[str, object]:
    """Return one fully populated neutral component manifest fixture."""
    return {
        "schema_version": RESEMBLIO_COMPONENT_MANIFEST_SCHEMA_VERSION,
        "family_id": "primary-action",
        "role": "action trigger",
        "anatomy": [{"name": "label", "required": True}],
        "slots": [{"name": "label", "required": True}],
        "variants": [{"name": "tone", "values": ["emphasized", "quiet"]}],
        "states": ["default", "focus-visible", "disabled"],
        "semantics": ["invokes one named action"],
        "accessibility": {
            "semantic_role": "button",
            "requirements": ["expose an accessible name"],
        },
        "keyboard": ["Enter", "Space"],
        "responsive": ["allow full-width placement on narrow containers"],
        "motion": {
            "trigger": "press",
            "duration_ms": 180,
            "easing": "ease-out",
            "reduced_motion": "disable decorative transition",
            "loop": "none",
        },
        "relationships": {
            "foundations": ["color", "spacing"],
            "compositions": ["cta-block"],
            "dependent_components": [],
        },
        "token_references": ["tokens.color.action", "tokens.spacing.control"],
        "implementation_constraints": ["Re-author all target copy and visual identity."],
    }


def test_validate_component_manifest_returns_independent_safe_copy() -> None:
    """A valid manifest is copied so later caller mutation cannot alter it."""
    raw = _manifest()
    validated = validate_component_manifest(raw)
    raw["family_id"] = "changed-after-validation"

    assert validated["family_id"] == "primary-action"
    assert validated["motion"]["duration_ms"] == 180  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("motion", {"reduced_motion": "disable"}, "motion"),
        ("variants", [{"name": "tone", "values": []}], "variants values"),
        ("token_references", ["https://unsafe.example"], "token_references"),
        ("implementation_constraints", ["Apple product copy"], "implementation_constraints"),
    ],
)
def test_validate_component_manifest_rejects_malformed_or_identifying_content(
    field: str,
    value: object,
    message: str,
) -> None:
    """Malformed nested facets and source identity never reach the API payload."""
    raw = deepcopy(_manifest())
    raw[field] = value

    with pytest.raises(ComponentManifestError, match=message):
        validate_component_manifest(raw)


def test_component_manifest_from_metadata_fails_closed() -> None:
    """Bad private metadata is omitted instead of exposing an unsafe payload."""
    raw = _manifest()
    raw["role"] = "<button>raw markup</button>"

    assert component_manifest_from_metadata({"component_manifest": raw}) is None
    assert component_manifest_from_metadata({"component_manifest": _manifest()}) is not None
