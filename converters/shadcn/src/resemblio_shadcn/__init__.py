"""resemblio_shadcn - convert a Resemblio DTCG manifest into a shadcn/ui theme.

Public surface:
    - ``dtcg_to_shadcn(manifest)`` - pure-data converter, returns ``ShadcnTheme``.
    - ``render_globals_css(theme)`` - render the ``:root`` + ``.dark`` CSS block.
    - ``render_tailwind_config(theme)`` - render the ``tailwind.config.js`` extension.

Schema stability: this package emits ``shadcn_schema_version=1`` (HSL-triple
era, Tailwind v3 convention). A future ``schema_version=2`` will add the
OKLch / Tailwind v4 ``@theme inline`` format shadcn introduced in 2026.
"""
from __future__ import annotations

from resemblio_shadcn.converter import (
    dtcg_to_shadcn,
    render_globals_css,
    render_tailwind_config,
)
from resemblio_shadcn.types import (
    DTCGManifest,
    ShadcnTheme,
    ShadcnColorVariables,
)

__all__ = [
    "dtcg_to_shadcn",
    "render_globals_css",
    "render_tailwind_config",
    "DTCGManifest",
    "ShadcnTheme",
    "ShadcnColorVariables",
]

__version__ = "0.1.0"
SHADCN_SCHEMA_VERSION = 1
