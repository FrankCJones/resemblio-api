"""Typed data shapes for the Resemblio -> Figma Variables converter.

Two boundaries are typed:

1. **Input** - a Resemblio DTCG manifest, the same ``dtcg_json`` produced by
   ``resemblio.code.extractor.drl_adapter.to_dtcg_json``. Top-level shape:
   ``{group_name: {leaf_name: {"$value": ..., "$type": ...}}}`` plus an
   optional ``schema_version`` int at the root.

2. **Output** - a ``FigmaVariablesPayload`` shaped to match Figma's REST
   Variables import format: ``collections`` + ``modes`` + ``variables``,
   with each variable carrying a ``valuesByMode`` dict keyed by mode id.

Frozen Pydantic v2 models so callers can pass the payload into a JSON
serializer (or POST it directly to ``/v1/files/:file_key/variables``) without
worrying about mutation.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


# ----------------------------------------------------------------------
# Input: DTCG manifest (loose dict; ``$``-prefixed keys make TypedDict
# attribute access impractical, so we keep the shape documented but the
# runtime type as ``dict[str, Any]``).
# ----------------------------------------------------------------------

class DTCGLeaf(TypedDict, total=False):
    """One DTCG token leaf: ``{"$value": ..., "$type": ...}``.

    Keys are ``$``-prefixed per the DTCG spec; consumers access via
    ``leaf["$value"]`` rather than attribute syntax.
    """


DTCGGroup = dict[str, dict[str, Any]]
DTCGManifest = dict[str, Any]


# ----------------------------------------------------------------------
# Output: Figma Variables payload
# ----------------------------------------------------------------------

class RGBAFloat(BaseModel):
    """A Figma COLOR value: RGBA components as 0.0-1.0 floats.

    Figma stores all colors as float-channel RGBA. The converter rounds to
    six decimal places (see ``RGBA_FLOAT_PRECISION``) which preserves the
    full precision of an 8-bit source and keeps output diff-stable.
    """

    r: float = Field(ge=0.0, le=1.0)
    g: float = Field(ge=0.0, le=1.0)
    b: float = Field(ge=0.0, le=1.0)
    a: float = Field(ge=0.0, le=1.0)

    model_config = ConfigDict(frozen=True)


class FigmaMode(BaseModel):
    """A Figma Variable Mode (e.g. "Light", "Dark"). v1 emits one per collection."""

    modeId: str
    name: str

    model_config = ConfigDict(frozen=True)


class FigmaCollection(BaseModel):
    """A Figma Variable Collection - the container for a set of Variables.

    Each collection has at least one Mode; Variables inside the collection
    carry one value per mode (``valuesByMode[modeId] = value``).
    """

    id: str
    name: str
    modes: tuple[FigmaMode, ...]

    model_config = ConfigDict(frozen=True)


# Figma Variable's resolvedType is constrained to the four enum strings.
FigmaResolvedType = Literal["COLOR", "FLOAT", "STRING", "BOOLEAN"]

# A Variable's per-mode value can be any of the four representations.
FigmaVariableValue = RGBAFloat | float | str | bool


class FigmaVariable(BaseModel):
    """One Figma Variable: name, type, parent collection, value-per-mode.

    The ``name`` uses Figma's slash-hierarchy convention (e.g.
    ``Brand/Primary``) so the imported variables nest in the Figma UI's
    grouping tree the same way the source DTCG path nested.
    """

    id: str
    name: str
    resolvedType: FigmaResolvedType
    collectionId: str
    valuesByMode: dict[str, FigmaVariableValue]

    model_config = ConfigDict(frozen=True)


class FigmaVariablesPayload(BaseModel):
    """The full Figma Variables import payload.

    Mirrors the REST API import shape: ``collections`` + ``variables``
    arrays at the top level, with mode definitions carried inside each
    collection. Schema metadata fields (``figma_schema_version``,
    ``resemblio_schema_version``, ``source_url``) are Resemblio additions
    that Figma ignores on import but downstream tooling reads.
    """

    collections: tuple[FigmaCollection, ...]
    variables: tuple[FigmaVariable, ...]
    source_url: str | None = None
    figma_schema_version: int = 1
    resemblio_schema_version: int | None = None

    model_config = ConfigDict(frozen=True)
