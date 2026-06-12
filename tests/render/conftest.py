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

# The render tests run a live HTTP fetch when a real network is available.
# They are pure-skip-safe when it isn't (see test logic). We do NOT set
# any global env defaults here; that is the calling test's job.

# Allow tests to discover the workspace root via WORKSPACE_ROOT env var.
# Falls back to walking up from this file until a CLAUDE.md is found,
# which is the workspace-root marker.
def _resolve_workspace_root() -> pathlib.Path:
    """Return the workspace root directory.

    Resolution order:
      1. WORKSPACE_ROOT env var (absolute path).
      2. Walk up from this file until a directory contains CLAUDE.md.
      3. Fall back to four levels up from this file (api/tests/render
         -> api/tests -> api -> code -> Resemblio -> projects -> WS).

    Raises RuntimeError when no candidate works; the test then SKIPs.
    """
    env = os.environ.get("WORKSPACE_ROOT")
    if env:
        candidate = pathlib.Path(env).resolve()
        if (candidate / "CLAUDE.md").is_file():
            return candidate
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "CLAUDE.md").is_file() and (parent / "projects").is_dir():
            return parent
    raise RuntimeError(
        "could not resolve workspace root from "
        f"{here}; set WORKSPACE_ROOT to override",
    )


WORKSPACE_ROOT = _resolve_workspace_root()
REFERENCE_ROOT = (
    WORKSPACE_ROOT
    / "projects"
    / "Resemblio"
    / "_verification"
    / "library-inspirado-correction-20260604"
)
