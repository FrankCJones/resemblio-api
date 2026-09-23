"""Backward-compatible component slices derived from the Apple system artifact.

The canonical authority is app/data/apple_system_manifest.json. This module
preserves the older component registry import surface while removing its
hand-authored family inventory and generic manifest factory.
"""
from __future__ import annotations

from typing import Final

from app.apple_system_manifest import component_manifest_slice, load_public_manifest
from app.library_component_manifest import ComponentManifest, validate_component_manifest


def _build_component_manifests() -> dict[str, ComponentManifest]:
    """Build validated compatibility slices for every canonical component."""
    manifests: dict[str, ComponentManifest] = {}
    for record in load_public_manifest()["records"]:
        if record["classification"] != "component":
            continue
        raw_manifest = component_manifest_slice(record["slug"])
        if raw_manifest is None:
            raise RuntimeError("canonical component record lacks a compatibility slice")
        manifests[record["slug"]] = validate_component_manifest(raw_manifest)
    return manifests


APPLE_COMPONENT_MANIFESTS: Final[dict[str, ComponentManifest]] = _build_component_manifests()
"""Compatibility slices keyed by canonical component slug."""


def manifest_for_apple_category(category_slug: str | None) -> ComponentManifest | None:
    """Return a validated component slice for a canonical slug or forms alias."""
    if category_slug is None:
        return None
    canonical = "forms" if category_slug == "form-fields" else category_slug
    manifest = APPLE_COMPONENT_MANIFESTS.get(canonical)
    return dict(manifest) if manifest is not None else None
