"""Render-fidelity test sub-tree conftest.

Isolated from the API test suite's root conftest so this directory does
not drag in the FastAPI test client, Stripe doubles, or the in-memory
DB fixtures. The render tests only need filesystem references + the
visual_fidelity_check sub-package.

Schema: render_conftest_v2

Phase 8 addition (2026-06-13): CORPUS_ROOT resolves the structural fidelity
text corpus (specs/*.json, tolerance_config.yml, manifest.json). It prefers
the in-repo ``tests/render/reference_corpus/`` copy so the structural gate's
text-only tier runs on a standalone CI checkout without the workspace
``_verification/`` tree. On a dev machine that has the workspace tree, it
falls back to REFERENCE_ROOT so full-gate runs (with live renders + SSIM)
keep working. SSIM + PNGs stay workspace-only per D-5.1 (informational-only)
and the public-repo trademark constraint.

Corpus root precedence (Phase 8):
  1. ``tests/render/reference_corpus/`` when ``tolerance_config.yml`` is
     present there (in-repo vendor copy; exists after Phase 8.2 commit).
  2. REFERENCE_ROOT (workspace _verification/ tree) when present and
     contains ``tolerance_config.yml`` (local full-gate runs).
  3. ``tests/render/reference_corpus/`` even when absent - tests derive
     non-existent paths and self-skip via the standard load_tolerance /
     load_manifest pattern.
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

#: Name of the in-repo corpus directory (sibling to this conftest).
_IN_REPO_CORPUS_NAME = "reference_corpus"


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


def resolve_corpus_root(
    *,
    in_repo_dir: pathlib.Path,
    reference_root: Optional[pathlib.Path],
) -> pathlib.Path:
    """Resolve the structural fidelity corpus root.

    Pure function (no os.environ / __file__ access) so it is unit-testable
    with injected paths.

    Precedence:
      1. ``in_repo_dir`` when it contains ``tolerance_config.yml`` (the
         vendored in-repo copy; present after Phase 8.2).
      2. ``reference_root`` when it contains ``tolerance_config.yml``
         (workspace ``_verification/`` tree; present on dev machines running
         full-gate sweeps).
      3. ``in_repo_dir`` even when absent - the caller derives non-existent
         paths from it; ``load_tolerance`` / ``load_manifest`` in the gate
         test call ``pytest.skip`` on missing files so tests self-skip rather
         than hard-fail.

    The in-repo corpus mirrors the workspace REFERENCE_ROOT layout:
      <corpus_root>/tolerance_config.yml
      <corpus_root>/fidelity_targets.yml
      <corpus_root>/reference_captures/manifest.json
      <corpus_root>/reference_captures/specs/*.json

    PNGs are intentionally absent from the in-repo corpus (public-repo
    trademark constraint; SSIM is informational-only per D-5.1). The live
    full-corpus sweep (``test_library_render_within_tolerance_of_brand_reference``)
    requires PNGs and therefore self-skips on CI, which is the intended
    behavior for that tier.
    """
    if (in_repo_dir / "tolerance_config.yml").is_file():
        return in_repo_dir
    if reference_root is not None and (reference_root / "tolerance_config.yml").is_file():
        return reference_root
    return in_repo_dir  # Absent; tests derive non-existent paths and self-skip.


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


def _module_corpus_root(
    here: pathlib.Path,
    workspace_root: pathlib.Path,
) -> pathlib.Path:
    """Bind CORPUS_ROOT for this process, never raising at import.

    Separated from ``_module_workspace_root`` so WORKSPACE_ROOT is
    available to pass as ``reference_root`` when present. Accepts both
    arguments injected (not read from globals) so that the resolution
    chain is testable without monkeypatching module state.
    """
    in_repo = here.parent / _IN_REPO_CORPUS_NAME
    reference_root = (
        workspace_root
        / "projects"
        / "Resemblio"
        / "_verification"
        / "library-inspirado-correction-20260604"
    )
    # Only pass reference_root when it actually exists on disk; avoids the
    # resolver preferring a non-existent workspace path over the in-repo dir.
    resolved_ref = reference_root if reference_root.is_dir() else None
    return resolve_corpus_root(in_repo_dir=in_repo, reference_root=resolved_ref)


WORKSPACE_ROOT = _module_workspace_root()
REFERENCE_ROOT = (
    WORKSPACE_ROOT
    / "projects"
    / "Resemblio"
    / "_verification"
    / "library-inspirado-correction-20260604"
)
CORPUS_ROOT = _module_corpus_root(
    here=pathlib.Path(__file__).resolve(),
    workspace_root=WORKSPACE_ROOT,
)
