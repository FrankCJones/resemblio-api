"""resemblio_figma - convert a Resemblio DTCG manifest into a Figma Variables payload.

Public surface:
    - ``dtcg_to_figma_variables(manifest)`` - pure-data converter, returns ``FigmaVariablesPayload``.
    - ``hex_to_rgba_floats(hex_color)`` - color-space helper.
    - ``dtcg_path_to_figma_name(path)`` - hierarchy mapping helper.

The output shape mirrors Figma's Variables REST import format: a payload of
collections, modes, and variables, each variable carrying its initial value
keyed by modeId. v1 emits a single Light mode per collection; Light/Dark
auto-inversion is a follow-up that belongs in the extractor, not the
converter.

Schema stability: this package emits ``figma_schema_version=1``. A future
``schema_version=2`` will add multi-mode (Light + Dark) emission once the
Resemblio extractor produces dark variants.
"""
from __future__ import annotations

from resemblio_figma.converter import (
    dtcg_path_to_figma_name,
    dtcg_to_figma_variables,
    hex_to_rgba_floats,
)
from resemblio_figma.types import (
    DTCGManifest,
    FigmaCollection,
    FigmaMode,
    FigmaVariable,
    FigmaVariablesPayload,
    RGBAFloat,
)

__all__ = [
    "dtcg_to_figma_variables",
    "hex_to_rgba_floats",
    "dtcg_path_to_figma_name",
    "DTCGManifest",
    "FigmaCollection",
    "FigmaMode",
    "FigmaVariable",
    "FigmaVariablesPayload",
    "RGBAFloat",
]

__version__ = "0.1.0"
FIGMA_SCHEMA_VERSION = 1
