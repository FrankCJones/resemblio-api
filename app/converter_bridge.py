"""Bridge into the vendored Resemblio converter packages.

Wires the in-tree converter packages (``code/api/converters/shadcn`` and
``code/api/converters/figma``) onto ``sys.path`` so the API process can import
their pure-data conversion helpers without requiring a separate ``pip install``
step in CI or on the box. The pattern mirrors ``app.extractor_bridge``'s
treatment of the vendored DRL extractor tree: discover the source root at
import time, prepend it once, then re-export the public surface.

Design intent:
    - The converters are pure-data, no I/O, no network. They are safe to call
      synchronously from a request handler.
    - The bridge owns the ``sys.path`` mutation; route modules import from
      ``app.converter_bridge`` only.
    - Re-export the converter schema-version integers so the API response
      contract can echo them back to the caller for audit.
"""
from __future__ import annotations

import sys
from pathlib import Path

# ``code/api/`` -> parent of the ``app/`` package. The vendored converter
# source trees live alongside ``app/`` at ``converters/<name>/src``.
API_ROOT = Path(__file__).resolve().parent.parent
SHADCN_SRC = API_ROOT / "converters" / "shadcn" / "src"
FIGMA_SRC = API_ROOT / "converters" / "figma" / "src"


def _prepend_sys_path(path: Path) -> None:
    """Move a path to the front of ``sys.path`` without duplicating entries."""
    path_text = str(path)
    if path_text in sys.path:
        sys.path.remove(path_text)
    sys.path.insert(0, path_text)


def _verify_path(path: Path, label: str) -> None:
    """Raise at import time if the vendored converter tree is missing."""
    if not path.exists():
        raise RuntimeError(
            f"vendored converter source for {label} not found at {path}; "
            "the API container or wheel build is missing the converters tree"
        )


_verify_path(SHADCN_SRC, "shadcn")
_verify_path(FIGMA_SRC, "figma")
_prepend_sys_path(SHADCN_SRC)
_prepend_sys_path(FIGMA_SRC)

# Imports below intentionally come AFTER the path mutation. Pyright/mypy will
# complain about the module location; this is the documented bridge pattern.
from resemblio_figma import (  # noqa: E402
    FIGMA_SCHEMA_VERSION,
    FigmaVariablesPayload,
    dtcg_to_figma_variables,
)
from resemblio_shadcn import (  # noqa: E402
    SHADCN_SCHEMA_VERSION,
    ShadcnTheme,
    dtcg_to_shadcn,
)
from resemblio_shadcn.converter import (  # noqa: E402
    render_globals_css,
    render_tailwind_config,
)

__all__ = [
    "FIGMA_SCHEMA_VERSION",
    "FigmaVariablesPayload",
    "SHADCN_SCHEMA_VERSION",
    "ShadcnTheme",
    "dtcg_to_figma_variables",
    "dtcg_to_shadcn",
    "render_globals_css",
    "render_tailwind_config",
]
