"""Typed data shapes for the Resemblio -> shadcn converter.

Two boundaries are typed:

1. **Input** - a Resemblio DTCG manifest, which is the ``dtcg_json`` payload
   produced by ``resemblio.code.extractor.drl_adapter.to_dtcg_json``. The
   top-level shape is ``{group_name: {leaf_name: {"$value": ..., "$type": ...}}}``
   plus an optional ``schema_version`` int at the root.

2. **Output** - a ``ShadcnTheme`` carrying the rendered HSL-triple color
   variables for both ``:root`` (light) and ``.dark``, the font family
   strings, the radius value, and the metadata needed to round-trip.

The output is deliberately a frozen Pydantic model so callers can pass it
into a Jinja template or write it to JSON without worrying about mutation.
"""
from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class DTCGLeaf(TypedDict, total=False):
    """One DTCG token leaf: ``{"$value": ..., "$type": ...}``.

    ``$type`` is optional in the DTCG spec (a parent group may set it) but
    Resemblio's extractor emits it on every leaf for downstream simplicity.
    """

    # ``$``-prefixed keys are illegal Python attribute names, hence the
    # TypedDict via dict-literal usage. Consumers access via ``leaf["$value"]``.
    # Pyright respects the literal-key form.


# A DTCG group is a dict of leaf-name -> leaf object. The top-level manifest
# is a dict of group-name -> group dict, plus a possible ``schema_version``.
DTCGGroup = dict[str, dict[str, Any]]
DTCGManifest = dict[str, Any]


class ShadcnColorVariables(BaseModel):
    """One mode's worth of shadcn CSS variables in HSL-triple form.

    Each value is a string like ``"222.2 47.4% 11.2%"`` - the bare HSL
    components separated by spaces, no ``hsl()`` wrapper. shadcn's CSS
    then wraps it: ``background-color: hsl(var(--primary))``.
    """

    background: str
    foreground: str
    card: str
    card_foreground: str = Field(alias="card-foreground")
    popover: str
    popover_foreground: str = Field(alias="popover-foreground")
    primary: str
    primary_foreground: str = Field(alias="primary-foreground")
    secondary: str
    secondary_foreground: str = Field(alias="secondary-foreground")
    muted: str
    muted_foreground: str = Field(alias="muted-foreground")
    accent: str
    accent_foreground: str = Field(alias="accent-foreground")
    destructive: str
    destructive_foreground: str = Field(alias="destructive-foreground")
    border: str
    input: str
    ring: str
    chart_1: str = Field(alias="chart-1")
    chart_2: str = Field(alias="chart-2")
    chart_3: str = Field(alias="chart-3")
    chart_4: str = Field(alias="chart-4")
    chart_5: str = Field(alias="chart-5")

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    def as_ordered_pairs(self) -> list[tuple[str, str]]:
        """Return ``[(slot, hsl_triple), ...]`` in the canonical slot order."""
        from resemblio_shadcn.constants import SHADCN_COLOR_SLOTS
        dumped = self.model_dump(by_alias=True)
        return [(slot, dumped[slot]) for slot in SHADCN_COLOR_SLOTS]


class ShadcnTheme(BaseModel):
    """Full shadcn theme bundle: light + dark colors, fonts, radius, metadata.

    This is the canonical output of ``dtcg_to_shadcn``. Rendering helpers
    (``render_globals_css``, ``render_tailwind_config``) consume this model.
    """

    light: ShadcnColorVariables
    dark: ShadcnColorVariables
    font_sans: str
    font_mono: str | None
    radius_rem: float
    source_url: str | None = None
    shadcn_schema_version: int = 1
    resemblio_schema_version: int | None = None

    model_config = ConfigDict(frozen=True)
