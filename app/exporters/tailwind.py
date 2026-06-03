"""DTCG -> Tailwind v4 ``@theme`` block export.

Tailwind v4 reads design tokens from a CSS file via ``@theme { ... }``,
NOT from a JS config file (that was the v3 surface; v4 deprecated it).
The token name format inside ``@theme`` is the wiring contract:

* ``--color-*``    -> generates ``bg-*``, ``text-*``, ``border-*`` utilities
* ``--font-*``     -> generates ``font-*`` utilities
* ``--spacing-*``  -> generates ``p-*``, ``m-*``, ``gap-*`` utilities
* ``--radius-*``   -> generates ``rounded-*`` utilities
* ``--shadow-*``   -> generates ``shadow-*`` utilities

We map DTCG groups to those slots and emit ONLY the categories Tailwind
v4 knows what to do with. Other DTCG groups (duration, cubicBezier,
number, "other") are dropped from the Tailwind output by design; they
still ship in the DTCG JSON and the plain CSS file. The motivation is
the working-product principle: a half-mapped Tailwind file forces the
user to debug "why is my ``duration-fast`` class not working." Better
to omit and let them use the DTCG payload directly for those slots.

Why one file vs config split: Tailwind v4 wants the ``@theme`` block
inline in the user's main CSS. Emitting a standalone file the user can
``@import`` is the cleanest seam.
"""
from __future__ import annotations

from typing import Any

from app.exporters.artifact import (
    EXPORTER_SCHEMA_VERSION,
    FORMAT_TAILWIND,
    ExporterArtifact,
    filename_for,
)

CONTENT_TYPE_TAILWIND: str = "text/css; charset=utf-8"

# DTCG-group -> Tailwind ``@theme`` namespace. Anything not in this
# table is omitted from the Tailwind output. The mapping is the
# integration contract with Tailwind v4's class generator.
_DTCG_GROUP_TO_TAILWIND_NAMESPACE: dict[str, str] = {
    "color": "color",
    "fontFamily": "font",
    "shadow": "shadow",
}

# DTCG dimension tokens split across three Tailwind namespaces by leaf
# prefix. The flat dimension group carries `space-*`, `radius-*`, and
# `text-*` size leaves; routing by leaf name to the right Tailwind
# namespace keeps the utility classes coherent.
_DIMENSION_LEAF_PREFIX_TO_TAILWIND: list[tuple[str, str]] = [
    ("space-", "spacing"),
    ("radius-", "radius"),
    ("text-", "text"),
    # Tracking lands in spacing-adjacent territory; Tailwind v4 has no
    # first-class tracking namespace, so we omit (covered by DTCG/CSS).
]


def _tailwind_value(value: Any) -> str:
    """Coerce a DTCG value to a Tailwind-property-safe string."""
    if isinstance(value, str):
        return value.replace("\n", " ").strip()
    if isinstance(value, (int, float)):
        return str(value)
    # Composite values are rare for the categories Tailwind cares about
    # (color/font/shadow); fall back to the JSON shape if encountered.
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", " "))


def _emit_simple_group(
    dtcg_group: dict[str, Any], tw_namespace: str
) -> list[str]:
    """Render a DTCG group whose leaves map 1:1 to a Tailwind namespace."""
    out: list[str] = []
    for leaf_name in sorted(dtcg_group.keys()):
        leaf = dtcg_group[leaf_name]
        if not isinstance(leaf, dict) or "$value" not in leaf:
            continue
        out.append(f"  --{tw_namespace}-{leaf_name}: {_tailwind_value(leaf['$value'])};")
    return out


def _emit_dimension_group(dtcg_group: dict[str, Any]) -> list[str]:
    """Route a DTCG dimension group across Tailwind's split namespaces.

    Leaves whose names match one of the prefix rules in
    ``_DIMENSION_LEAF_PREFIX_TO_TAILWIND`` land under the corresponding
    Tailwind namespace; the prefix is preserved on the leaf so a
    ``space-4`` DTCG token becomes ``--spacing-space-4`` (the leaf
    name stays self-describing inside the bigger token soup of the
    user's project).
    """
    out: list[str] = []
    for leaf_name in sorted(dtcg_group.keys()):
        leaf = dtcg_group[leaf_name]
        if not isinstance(leaf, dict) or "$value" not in leaf:
            continue
        tw_namespace: str | None = None
        for prefix, namespace in _DIMENSION_LEAF_PREFIX_TO_TAILWIND:
            if leaf_name.startswith(prefix):
                tw_namespace = namespace
                break
        if tw_namespace is None:
            continue
        out.append(
            f"  --{tw_namespace}-{leaf_name}: {_tailwind_value(leaf['$value'])};"
        )
    return out


def dtcg_to_tailwind_theme(dtcg: dict[str, Any]) -> str:
    """Render a DTCG payload as a Tailwind v4 ``@theme`` block.

    Edge cases:
    - DTCG groups Tailwind has no native namespace for (duration,
      cubicBezier, number, "other") are omitted. The user keeps them
      via the DTCG JSON / CSS exports.
    - An empty input returns a valid empty ``@theme {}`` block so the
      caller always receives parsable Tailwind input.
    """
    lines: list[str] = ["@theme {"]
    color = dtcg.get("color")
    if isinstance(color, dict) and color:
        lines.extend(_emit_simple_group(color, _DTCG_GROUP_TO_TAILWIND_NAMESPACE["color"]))
    font = dtcg.get("fontFamily")
    if isinstance(font, dict) and font:
        lines.extend(_emit_simple_group(font, _DTCG_GROUP_TO_TAILWIND_NAMESPACE["fontFamily"]))
    shadow = dtcg.get("shadow")
    if isinstance(shadow, dict) and shadow:
        lines.extend(_emit_simple_group(shadow, _DTCG_GROUP_TO_TAILWIND_NAMESPACE["shadow"]))
    dimension = dtcg.get("dimension")
    if isinstance(dimension, dict) and dimension:
        lines.extend(_emit_dimension_group(dimension))
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def tailwind_artifact(extraction_id: int, dtcg: dict[str, Any]) -> ExporterArtifact:
    """Wrap the Tailwind text in an ExporterArtifact for the route handler."""
    return ExporterArtifact(
        bytes=dtcg_to_tailwind_theme(dtcg).encode("utf-8"),
        content_type=CONTENT_TYPE_TAILWIND,
        filename=filename_for(extraction_id, FORMAT_TAILWIND),
        schema_version=EXPORTER_SCHEMA_VERSION,
    )
