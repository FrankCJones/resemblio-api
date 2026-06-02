"""DTCG manifest -> Figma Variables payload conversion.

Pure-data transforms only; no I/O, no network. Every public function is
deterministic and round-trip stable (calling twice on the same input yields
identical output, byte-for-byte).

High-level path:

    Resemblio DTCG manifest
        -> walk top-level groups in routing order
        -> for each routed group, build a Collection (single Light mode)
        -> for each leaf, build a Variable with the right resolvedType
        -> map DTCG dotted-or-dashed names to Figma slash-hierarchy names
        -> wrap into FigmaVariablesPayload with schema metadata

The routing table (``DTCG_GROUP_ROUTING``) is the contract: only groups it
lists become Figma collections. Anything else is silently skipped - the
converter degrades rather than guessing at Figma-incompatible types.
"""
from __future__ import annotations

from typing import Any, Iterable

from resemblio_figma.constants import (
    COLLECTION_ORDER,
    DEFAULT_ALPHA,
    DEFAULT_MODE_ID,
    DEFAULT_MODE_NAME,
    DTCG_GROUP_ROUTING,
    FIGMA_SCHEMA_VERSION,
    FIGMA_TYPE_COLOR,
    FIGMA_TYPE_FLOAT,
    FIGMA_TYPE_STRING,
    REM_TO_PX_MULTIPLIER,
    RGBA_FLOAT_PRECISION,
)
from resemblio_figma.types import (
    DTCGManifest,
    FigmaCollection,
    FigmaMode,
    FigmaResolvedType,
    FigmaVariable,
    FigmaVariableValue,
    FigmaVariablesPayload,
    RGBAFloat,
)


# ----------------------------------------------------------------------
# Color-space helpers
# ----------------------------------------------------------------------

def _normalize_hex(value: str) -> str | None:
    """Normalize a hex color string to 6-digit ``#rrggbb`` form.

    Accepts ``#rgb``, ``#rrggbb``, and the same without the leading ``#``.
    Returns ``None`` for anything that does not parse as hex (``rgb()``,
    ``hsl()``, named colors). Out-of-scope formats are skipped by callers.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip().lstrip("#")
    if len(raw) == 3 and all(c in "0123456789abcdefABCDEF" for c in raw):
        raw = "".join(c * 2 for c in raw)
    if len(raw) != 6 or not all(c in "0123456789abcdefABCDEF" for c in raw):
        return None
    return f"#{raw.lower()}"


def hex_to_rgba_floats(hex_color: str, alpha: float = DEFAULT_ALPHA) -> RGBAFloat:
    """Convert a ``#rrggbb`` (or ``#rgb``) hex string to a Figma ``RGBAFloat``.

    Args:
        hex_color: The hex color, with or without leading ``#``. Three-digit
            shorthand is expanded (``#f00`` -> ``#ff0000``).
        alpha: Alpha channel, 0.0-1.0. Defaults to 1.0 (fully opaque).

    Returns:
        A frozen ``RGBAFloat`` with each channel rounded to
        ``RGBA_FLOAT_PRECISION`` decimal places.

    Raises:
        ValueError: If ``hex_color`` is not a recognized hex form.

    Example:
        >>> hex_to_rgba_floats("#ff0000")
        RGBAFloat(r=1.0, g=0.0, b=0.0, a=1.0)
    """
    normalized = _normalize_hex(hex_color)
    if normalized is None:
        raise ValueError(f"not a hex color: {hex_color!r}")
    r = round(int(normalized[1:3], 16) / 255.0, RGBA_FLOAT_PRECISION)
    g = round(int(normalized[3:5], 16) / 255.0, RGBA_FLOAT_PRECISION)
    b = round(int(normalized[5:7], 16) / 255.0, RGBA_FLOAT_PRECISION)
    a = round(max(0.0, min(1.0, alpha)), RGBA_FLOAT_PRECISION)
    return RGBAFloat(r=r, g=g, b=b, a=a)


# ----------------------------------------------------------------------
# Name / path mapping
# ----------------------------------------------------------------------

def dtcg_path_to_figma_name(leaf_name: str) -> str:
    """Map a DTCG leaf name to Figma's slash-hierarchy variable name.

    DTCG leaves in Resemblio's extractor are flat (one segment per leaf,
    using dashes or dots for visual nesting, e.g. ``brand-primary`` or
    ``brand.primary``). Figma's convention is ``Brand/Primary``.

    Rules:
        - Dots and dashes both become slashes.
        - Each segment is title-cased on its first character only; existing
          casing in the rest of the segment is preserved (so ``XL`` stays
          ``XL`` and ``primary`` becomes ``Primary``).
        - Empty segments are dropped (e.g. ``brand--primary`` collapses).

    Example:
        >>> dtcg_path_to_figma_name("color.brand.primary")
        'Color/Brand/Primary'
        >>> dtcg_path_to_figma_name("space-4")
        'Space/4'
    """
    if not isinstance(leaf_name, str) or not leaf_name.strip():
        return ""
    # Split on either '.' or '-'.
    parts: list[str] = []
    buffer: list[str] = []
    for ch in leaf_name.strip():
        if ch in (".", "-"):
            if buffer:
                parts.append("".join(buffer))
                buffer = []
        else:
            buffer.append(ch)
    if buffer:
        parts.append("".join(buffer))

    titled = []
    for seg in parts:
        if not seg:
            continue
        # Title-case first char only; preserve the rest verbatim.
        titled.append(seg[0].upper() + seg[1:] if len(seg) > 1 else seg.upper())
    return "/".join(titled)


# ----------------------------------------------------------------------
# Value parsing
# ----------------------------------------------------------------------

def _parse_dimension_value(raw: Any) -> float | None:
    """Parse a DTCG dimension ``$value`` into a unitless float (Figma FLOAT).

    Accepts ``"8px"``, ``"0.5rem"``, bare numbers, and numeric strings.
    Returns ``None`` for anything else; the caller skips the leaf.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return round(float(raw), RGBA_FLOAT_PRECISION)
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    try:
        if text.endswith("rem"):
            return round(float(text[:-3].strip()) * REM_TO_PX_MULTIPLIER, RGBA_FLOAT_PRECISION)
        if text.endswith("px"):
            return round(float(text[:-2].strip()), RGBA_FLOAT_PRECISION)
        return round(float(text), RGBA_FLOAT_PRECISION)
    except ValueError:
        return None


def _parse_number_value(raw: Any) -> float | None:
    """Parse a DTCG number ``$value`` into a float. Returns ``None`` on failure."""
    if isinstance(raw, bool):  # bool is a subclass of int in Python; reject.
        return None
    if isinstance(raw, (int, float)):
        return round(float(raw), RGBA_FLOAT_PRECISION)
    if isinstance(raw, str):
        try:
            return round(float(raw.strip()), RGBA_FLOAT_PRECISION)
        except ValueError:
            return None
    return None


def _convert_leaf_value(
    raw_value: Any,
    figma_type: str,
) -> FigmaVariableValue | None:
    """Convert one DTCG leaf ``$value`` to its Figma representation.

    Returns ``None`` for unparseable values; the caller skips such leaves
    rather than emitting a broken Variable.
    """
    if figma_type == FIGMA_TYPE_COLOR:
        if not isinstance(raw_value, str):
            return None
        normalized = _normalize_hex(raw_value)
        if normalized is None:
            return None
        return hex_to_rgba_floats(normalized)
    if figma_type == FIGMA_TYPE_FLOAT:
        # Dimension and number both land here; dimension carries units.
        result = _parse_dimension_value(raw_value)
        if result is not None:
            return result
        return _parse_number_value(raw_value)
    if figma_type == FIGMA_TYPE_STRING:
        if isinstance(raw_value, str) and raw_value.strip():
            return raw_value.strip()
        return None
    return None


# ----------------------------------------------------------------------
# Group iteration
# ----------------------------------------------------------------------

def _iter_group_leaves(manifest: DTCGManifest, group_name: str) -> Iterable[tuple[str, Any]]:
    """Yield ``(leaf_name, raw_value)`` for every leaf in a top-level group."""
    group = manifest.get(group_name) or {}
    if not isinstance(group, dict):
        return
    for leaf_name, leaf in group.items():
        if not isinstance(leaf, dict):
            continue
        raw_value = leaf.get("$value")
        if raw_value is None:
            continue
        yield leaf_name, raw_value


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

def dtcg_to_figma_variables(
    manifest: DTCGManifest,
    source_url: str | None = None,
) -> FigmaVariablesPayload:
    """Convert a Resemblio DTCG manifest into a ``FigmaVariablesPayload``.

    Args:
        manifest: A DTCG manifest dict as emitted by Resemblio's extractor.
            Top-level shape is ``{group: {leaf: {"$value": ..., "$type": ...}}}``
            with an optional ``schema_version`` int at the root.
        source_url: Optional source URL stamped into the payload metadata for
            provenance. Does not affect Variable output.

    Returns:
        A frozen ``FigmaVariablesPayload`` containing one Collection per
        DTCG group that routes (Colors / Spacing / Typography / Numbers),
        each with a single Light mode in v1. Variables carry the source
        leaf name mapped to Figma's slash-hierarchy convention.

    Edge cases:
        - Empty manifest yields a payload with no Variables and no
          Collections (the minimum valid shape). No exception is raised.
        - Non-routable groups in the manifest are skipped silently.
        - Unparseable leaf values (named colors, ``rgb()``, malformed
          dimensions) are skipped; they do not contribute to the output.
        - Output is deterministic: leaves within a group emit in source
          dict-iteration order (Python 3.7+ insertion order).
    """
    collections_out: list[FigmaCollection] = []
    variables_out: list[FigmaVariable] = []

    # Build a Mode + Collection per routed group, but only emit the
    # collection if it ends up with at least one Variable. Empty
    # collections are noise in Figma.
    pending: dict[str, tuple[FigmaCollection, list[FigmaVariable]]] = {}

    for group_name in _ordered_present_groups(manifest):
        routing = DTCG_GROUP_ROUTING.get(group_name)
        if routing is None:
            continue
        collection_name, figma_type = routing

        mode = FigmaMode(modeId=DEFAULT_MODE_ID, name=DEFAULT_MODE_NAME)
        collection_id = f"collection-{collection_name.lower()}"
        collection = FigmaCollection(
            id=collection_id,
            name=collection_name,
            modes=(mode,),
        )

        bucket = pending.setdefault(collection_id, (collection, []))
        _, var_list = bucket

        for leaf_name, raw_value in _iter_group_leaves(manifest, group_name):
            converted = _convert_leaf_value(raw_value, figma_type)
            if converted is None:
                continue
            figma_name = dtcg_path_to_figma_name(f"{collection_name.lower()}.{leaf_name}")
            variable_id = f"{collection_id}::{leaf_name}"
            var_list.append(
                FigmaVariable(
                    id=variable_id,
                    name=figma_name,
                    resolvedType=_cast_resolved_type(figma_type),
                    collectionId=collection_id,
                    valuesByMode={DEFAULT_MODE_ID: converted},
                )
            )

    # Emit in canonical collection order so output is diff-stable across
    # input dicts whose group order varies.
    for collection_name in COLLECTION_ORDER:
        collection_id = f"collection-{collection_name.lower()}"
        bucket = pending.get(collection_id)
        if bucket is None:
            continue
        collection, var_list = bucket
        if not var_list:
            continue
        collections_out.append(collection)
        variables_out.extend(var_list)

    resemblio_schema = (
        manifest.get("schema_version")
        if isinstance(manifest.get("schema_version"), int)
        else None
    )

    return FigmaVariablesPayload(
        collections=tuple(collections_out),
        variables=tuple(variables_out),
        source_url=source_url,
        figma_schema_version=FIGMA_SCHEMA_VERSION,
        resemblio_schema_version=resemblio_schema,
    )


def _ordered_present_groups(manifest: DTCGManifest) -> list[str]:
    """Return the routed-group names present in the manifest, in canonical order.

    Canonical order follows ``DTCG_GROUP_ROUTING`` insertion order so output
    is deterministic regardless of input dict key order.
    """
    return [g for g in DTCG_GROUP_ROUTING.keys() if isinstance(manifest.get(g), dict)]


def _cast_resolved_type(figma_type: str) -> FigmaResolvedType:
    """Narrow the stringly-typed routing value to the Literal type."""
    if figma_type == FIGMA_TYPE_COLOR:
        return "COLOR"
    if figma_type == FIGMA_TYPE_FLOAT:
        return "FLOAT"
    if figma_type == FIGMA_TYPE_STRING:
        return "STRING"
    return "BOOLEAN"
