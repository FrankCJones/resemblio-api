"""DTCG -> CSS custom-properties export.

Emits a single ``:root { ... }`` block of CSS custom properties so a
designer can drop the file into any stylesheet via ``@import`` or a
plain ``<link>`` tag without a build step. This is the format with the
lowest integration cost; everything else assumes tooling.

Naming
------

Each token name follows ``--<group>-<leaf>``. Examples::

    --color-bg: #ffffff;
    --color-accent: #ff3366;
    --font-family-body: Inter, sans-serif;
    --dimension-space-1: 4px;
    --shadow-sm: 0 1px 2px rgb(0 0 0 / 0.1);

The group prefix preserves DTCG semantics without forcing the user to
namespace tokens themselves. Group names are lower-kebab-cased
(``fontFamily`` -> ``font-family``) so they match CSS conventions.

Skipped groups
--------------

The DTCG ``schema_version`` key (a sibling of the real token groups in
the persisted payload) is filtered out; emitting it as a custom
property would produce nonsense like ``--schema-version: 1;``.
"""
from __future__ import annotations

import re
from typing import Any

from app.exporters.artifact import (
    EXPORTER_SCHEMA_VERSION,
    FORMAT_CSS,
    ExporterArtifact,
    filename_for,
)

CONTENT_TYPE_CSS: str = "text/css; charset=utf-8"

# Non-token sibling keys that may sit at the top level of the DTCG dict
# (provenance metadata, schema version). Skipping by allowlist would be
# brittle; skipping by denylist keeps us forward-compatible with any
# future metadata fields the extractor adds.
_NON_TOKEN_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"schema_version"})

# ``camelCase`` -> ``kebab-case`` for group names (``fontFamily`` ->
# ``font-family``, ``cubicBezier`` -> ``cubic-bezier``). The leaf names
# are already kebab-cased by ``to_dtcg_json`` so no second pass needed.
_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _kebab(name: str) -> str:
    """Convert camelCase to kebab-case for the CSS namespace prefix."""
    return _CAMEL_BOUNDARY.sub("-", name).lower()


def _css_safe_value(value: Any) -> str:
    """Coerce a DTCG ``$value`` payload to a CSS-property-safe string.

    DTCG composite types (shadow, gradient, transition) can carry a
    structured ``$value`` (dict or list) per spec. The extractor today
    emits string values for every type Resemblio captures (color hex,
    font stack, dimension with unit, shadow declaration); we coerce
    anything else to JSON so the property is at least well-formed and
    a downstream consumer sees the drift. Stripping newlines guards
    against any future leaf that breaks the one-property-per-line
    parser invariant.
    """
    if isinstance(value, str):
        return value.replace("\n", " ").strip()
    if isinstance(value, (int, float)):
        return str(value)
    # Composite types: serialize as JSON so the property stays valid
    # (the consumer's CSS parser will treat the dict as a string).
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", " "))


def dtcg_to_css(dtcg: dict[str, Any]) -> str:
    """Render a DTCG payload as a CSS ``:root { ... }`` block.

    Output is sorted (group name, then leaf name) so two runs against
    the same DTCG produce byte-identical text. Empty groups are
    omitted; an empty input returns the empty selector
    ``":root {\\n}\\n"`` so callers always receive a valid CSS file.
    """
    lines: list[str] = [":root {"]
    for group_name in sorted(dtcg.keys()):
        if group_name in _NON_TOKEN_TOP_LEVEL_KEYS:
            continue
        group_payload = dtcg[group_name]
        if not isinstance(group_payload, dict) or not group_payload:
            continue
        kebab_group = _kebab(group_name)
        for leaf_name in sorted(group_payload.keys()):
            leaf = group_payload[leaf_name]
            if not isinstance(leaf, dict) or "$value" not in leaf:
                continue
            css_value = _css_safe_value(leaf["$value"])
            lines.append(f"  --{kebab_group}-{leaf_name}: {css_value};")
    lines.append("}")
    lines.append("")  # trailing newline so POSIX-friendly editors are happy
    return "\n".join(lines)


def css_artifact(extraction_id: int, dtcg: dict[str, Any]) -> ExporterArtifact:
    """Wrap the CSS text in an ExporterArtifact for the route handler."""
    return ExporterArtifact(
        bytes=dtcg_to_css(dtcg).encode("utf-8"),
        content_type=CONTENT_TYPE_CSS,
        filename=filename_for(extraction_id, FORMAT_CSS),
        schema_version=EXPORTER_SCHEMA_VERSION,
    )
