"""Render-fidelity test sub-tree conftest.

Isolated from the API test suite's root conftest so this directory does
not drag in the FastAPI test client, Stripe doubles, or the in-memory
DB fixtures. The render tests only need filesystem references + the
visual_fidelity_check sub-package.

Schema: render_conftest_v1
"""
from __future__ import annotations

import os
import pathlib
from typing import Optional

# The render tests run a live HTTP fetch when a real network is available.
# They are pure-skip-safe when it isn't (see test logic). We do NOT set
# any global env defaults here; that is the calling test's job.

#: Fallback depth from this file to a best-effort workspace root when no
#: CLAUDE.md+projects ancestor is found. From tests/render/conftest.py the
#: parents are: [0]=render, [1]=tests, [2]=<repo root>. Index 2 is the
#: resemblio-api repo root on the standalone CI checkout; REFERENCE_ROOT
#: derived from it will not exist there, so the live gate self-skips.
_FALLBACK_ROOT_PARENT_INDEX = 2


def resolve_workspace_root(
    *,
    start: pathlib.Path,
    env_value: Optional[str],
) -> Optional[pathlib.Path]:
    """Resolve the workspace root from a start path + optional env override.

    Pure function (no os.environ / __file__ access) so it is unit-testable
    with an injected start path and env value.

    Resolution order:
      1. ``env_value`` when set and the named directory contains CLAUDE.md.
      2. Walk up from ``start`` to the first ancestor that contains BOTH a
         CLAUDE.md file and a projects/ directory (the workspace markers).

    Returns the resolved Path, or **None** when neither resolves. Returning
    None rather than raising is deliberate: this function runs at conftest
    import time, and a raise there is a pytest collection error for the whole
    tests/render tree (it turns the deploy red on the standalone resemblio-api
    checkout, which has no workspace markers above the repo root). The caller
    applies a best-effort fallback so import never crashes.
    """
    if env_value:
        candidate = pathlib.Path(env_value).resolve()
        if (candidate / "CLAUDE.md").is_file():
            return candidate
    start = start.resolve()
    for parent in (start, *start.parents):
        if (parent / "CLAUDE.md").is_file() and (parent / "projects").is_dir():
            return parent
    return None


def _module_workspace_root() -> pathlib.Path:
    """Bind WORKSPACE_ROOT for this process, never raising at import.

    Uses the pure resolver against the real file location + env, then falls
    back to a best-effort ancestor when resolution fails (the CI-checkout
    case). The fallback intentionally points at a directory whose
    REFERENCE_ROOT subtree will not exist on the standalone checkout, so the
    fidelity gate self-skips (load_tolerance / load_manifest call pytest.skip
    on missing files) instead of crashing collection.
    """
    here = pathlib.Path(__file__).resolve()
    resolved = resolve_workspace_root(
        start=here.parent,
        env_value=os.environ.get("WORKSPACE_ROOT"),
    )
    if resolved is not None:
        return resolved
    return here.parents[_FALLBACK_ROOT_PARENT_INDEX]


WORKSPACE_ROOT = _module_workspace_root()
REFERENCE_ROOT = (
    WORKSPACE_ROOT
    / "projects"
    / "Resemblio"
    / "_verification"
    / "library-inspirado-correction-20260604"
)
