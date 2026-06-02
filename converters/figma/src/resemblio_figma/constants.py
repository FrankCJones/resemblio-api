"""Named constants for the Figma Variables converter.

Centralized so collection names, type enums, and DTCG-group routing live in
one place. Reference: https://www.figma.com/developers/api#variables (REST
Variables import format).
"""
from __future__ import annotations

from typing import Final

# Output schema version stamped into the rendered ``FigmaVariablesPayload``.
FIGMA_SCHEMA_VERSION: Final[int] = 1

# Figma Variable resolved-type enum (REST API). Only these four types exist
# in the Variables API surface as of 2026.
FIGMA_TYPE_COLOR: Final[str] = "COLOR"
FIGMA_TYPE_FLOAT: Final[str] = "FLOAT"
FIGMA_TYPE_STRING: Final[str] = "STRING"
FIGMA_TYPE_BOOLEAN: Final[str] = "BOOLEAN"

# Canonical collection names. Each DTCG top-level group routes to exactly
# one collection. v1 emits a single Light mode per collection.
COLLECTION_COLORS: Final[str] = "Colors"
COLLECTION_SPACING: Final[str] = "Spacing"
COLLECTION_TYPOGRAPHY: Final[str] = "Typography"
COLLECTION_NUMBERS: Final[str] = "Numbers"

# Default single-mode name and stable id. v1 ships Light only; Dark is
# follow-up work on the extractor side.
DEFAULT_MODE_NAME: Final[str] = "Light"
DEFAULT_MODE_ID: Final[str] = "mode-light"

# DTCG-group -> (collection-name, figma-type) routing table. Groups not
# present here are skipped (the converter degrades gracefully rather than
# guessing at types Figma cannot represent).
DTCG_GROUP_ROUTING: Final[dict[str, tuple[str, str]]] = {
    "color": (COLLECTION_COLORS, FIGMA_TYPE_COLOR),
    "dimension": (COLLECTION_SPACING, FIGMA_TYPE_FLOAT),
    "fontFamily": (COLLECTION_TYPOGRAPHY, FIGMA_TYPE_STRING),
    "number": (COLLECTION_NUMBERS, FIGMA_TYPE_FLOAT),
}

# Order in which collections appear in the output payload. Stable across
# runs for diff-friendliness.
COLLECTION_ORDER: Final[tuple[str, ...]] = (
    COLLECTION_COLORS,
    COLLECTION_SPACING,
    COLLECTION_TYPOGRAPHY,
    COLLECTION_NUMBERS,
)

# Default alpha for hex colors lacking an alpha channel. Figma stores
# alpha as a 0.0-1.0 float per channel.
DEFAULT_ALPHA: Final[float] = 1.0

# Rounding precision for RGBA float channels. Six decimal places exceeds
# the precision of the 8-bit source and keeps round-trip stable.
RGBA_FLOAT_PRECISION: Final[int] = 6

# Px-to-rem and px-passthrough handling. Figma Variables of FLOAT type
# are unitless; we pass the numeric value through and Figma applies it as
# the unit the consuming style demands. For ``Xpx`` we emit ``X``; for
# ``Xrem`` we emit ``X * 16`` so spacing tokens stay in the same numeric
# universe whether the source DTCG used rem or px.
REM_TO_PX_MULTIPLIER: Final[float] = 16.0
