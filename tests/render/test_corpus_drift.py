"""Drift guard: in-repo corpus must not silently diverge from workspace source.

Single source of truth discipline (Phase 8.4):

The workspace ``_verification/library-inspirado-correction-20260604/`` is the
authoring source for the structural fidelity specs. The in-repo
``tests/render/reference_corpus/`` is the CI mirror. Two copies that can
drift silently are worse than one - the first time CI passes but the gate-run
box fails you know you have a split-brain. This guard makes drift loud on a
dev machine (where both copies are present) without breaking CI (where the
workspace tree is absent).

Behavior:
  - Workspace tree ABSENT (CI checkout): all drift tests self-skip. Same
    discipline as load_tolerance / load_manifest in test_visual_fidelity_gate.
  - Workspace tree PRESENT (dev): each vendored spec is compared byte-for-byte
    against its workspace original. A mismatch fails with the file name and a
    note to run scripts/sync_fidelity_corpus.py.

PNG guard:
  - test_no_png_in_corpus_permanent runs EVERYWHERE (no workspace dependency).
    It enforces the public-repo trademark constraint regardless of whether the
    workspace tree is present.

Schema: corpus_drift_guard_v1
"""
from __future__ import annotations

import hashlib
import pathlib

import pytest

# In-repo corpus root (always present after Phase 8.2).
_IN_REPO_CORPUS = pathlib.Path(__file__).resolve().parent / "reference_corpus"

# Workspace authoring root (absent on CI, present on dev machines).
_WORKSPACE_CORPUS_MARKER = "_verification/library-inspirado-correction-20260604"


def _find_workspace_corpus_root() -> pathlib.Path | None:
    """Resolve the workspace _verification/ corpus root, or None when absent.

    Walks up from this file to find the CLAUDE.md+projects/ workspace marker,
    then appends the known relative path to the verification corpus root.
    Returns None when no workspace marker is found (the CI-checkout case).
    """
    here = pathlib.Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "CLAUDE.md").is_file() and (parent / "projects").is_dir():
            candidate = (
                parent
                / "projects"
                / "Resemblio"
                / "_verification"
                / "library-inspirado-correction-20260604"
            )
            return candidate if candidate.is_dir() else None
    return None


_WORKSPACE_CORPUS = _find_workspace_corpus_root()


def _md5(path: pathlib.Path) -> str:
    return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()


def test_no_png_in_corpus_permanent() -> None:
    """No brand-site PNG may exist under the in-repo corpus at any time.

    Runs on every checkout (CI + dev). The .gitignore in reference_corpus/
    blocks accidental adds; this test catches anything that slipped through
    (e.g. a force-add or a future well-meaning PNG vendor).

    Public-repo trademark defense: D-5.1 (locked 2026-06-13) - SSIM is
    informational-only; the structural gate does not need PNGs. Brand-site
    screenshots (apple.com, stripe.com, vercel.com, ...) must never land
    in a public repository.
    """
    if not _IN_REPO_CORPUS.exists():
        pytest.skip("in-repo corpus not yet vendored (pre-Phase 8.2)")
    pngs = list(_IN_REPO_CORPUS.rglob("*.png"))
    assert not pngs, (
        f"PNG(s) committed to the in-repo corpus: {pngs}. "
        "Remove them immediately. Brand-site screenshots must not appear in a "
        "public repo. See reference_corpus/README.md for rationale."
    )


def test_vendored_specs_match_workspace_when_present() -> None:
    """Each vendored spec must be byte-identical to its workspace original.

    Self-skips on CI (workspace corpus absent). Loud on dev so the operator
    knows to run scripts/sync_fidelity_corpus.py before committing.
    """
    if _WORKSPACE_CORPUS is None:
        pytest.skip(
            f"workspace corpus not found ({_WORKSPACE_CORPUS_MARKER}); "
            "skipping drift check (expected on CI checkout)"
        )

    workspace_specs = _WORKSPACE_CORPUS / "reference_captures" / "specs"
    in_repo_specs = _IN_REPO_CORPUS / "reference_captures" / "specs"

    if not workspace_specs.is_dir():
        pytest.skip(f"workspace specs dir absent at {workspace_specs}")
    if not in_repo_specs.is_dir():
        pytest.fail(
            f"in-repo specs dir absent at {in_repo_specs}. "
            "Has the corpus been vendored? Run scripts/sync_fidelity_corpus.py."
        )

    workspace_jsons = sorted(workspace_specs.glob("*.json"))
    assert workspace_jsons, f"No JSON specs found in workspace at {workspace_specs}"

    drifted: list[str] = []
    for ws_file in workspace_jsons:
        ir_file = in_repo_specs / ws_file.name
        if not ir_file.exists():
            drifted.append(f"MISSING in repo: {ws_file.name}")
            continue
        if _md5(ws_file) != _md5(ir_file):
            drifted.append(f"CONTENT DRIFT: {ws_file.name}")

    # Also check for in-repo files that have no workspace counterpart (leaked extras).
    ir_jsons = {f.name for f in in_repo_specs.glob("*.json")}
    ws_jsons = {f.name for f in workspace_jsons}
    extras = ir_jsons - ws_jsons
    for extra in sorted(extras):
        drifted.append(f"EXTRA in repo (no workspace original): {extra}")

    assert not drifted, (
        "In-repo corpus has drifted from workspace source:\n"
        + "\n".join(f"  {d}" for d in drifted)
        + "\n\nFix: run `python scripts/sync_fidelity_corpus.py` from the "
        "workspace root and commit the result."
    )


def test_vendored_tolerance_matches_workspace_when_present() -> None:
    """tolerance_config.yml in-repo copy must match workspace original.

    Self-skips on CI. Catches the case where the tolerance knobs are updated
    in the workspace (e.g. Frank ratifies a tighter floor) but the in-repo
    copy is not synced before pushing.
    """
    if _WORKSPACE_CORPUS is None:
        pytest.skip(
            "workspace corpus not found; skipping drift check (expected on CI)"
        )

    ws_tol = _WORKSPACE_CORPUS / "tolerance_config.yml"
    ir_tol = _IN_REPO_CORPUS / "tolerance_config.yml"

    if not ws_tol.is_file():
        pytest.skip(f"workspace tolerance_config.yml not found at {ws_tol}")
    if not ir_tol.is_file():
        pytest.fail(
            f"in-repo tolerance_config.yml absent at {ir_tol}. "
            "Run scripts/sync_fidelity_corpus.py."
        )

    assert _md5(ws_tol) == _md5(ir_tol), (
        "tolerance_config.yml differs between workspace and in-repo copy. "
        "Run scripts/sync_fidelity_corpus.py and commit."
    )
