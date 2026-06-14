"""Phase 0 hardening: conftest workspace-root resolution must never crash import.

Regression guard for the CI-breakage this test pins: tests/render/conftest.py
resolves the workspace root at module-import time. The original implementation
RAISED RuntimeError when no CLAUDE.md+projects ancestor existed. On the
standalone resemblio-api CI checkout (repo rooted at what is code/api/ in the
workspace, with no workspace CLAUDE.md ancestor), that raise fires while pytest
imports the conftest, which is a COLLECTION ERROR for the whole tests/render
tree - not a skip - and turns the deploy red.

These tests exercise the pure resolver in isolation (injectable start path +
env value, no reliance on the real on-disk workspace) so the not-found path is
proven to degrade to None rather than raise. The module-level binding then
applies a best-effort fallback so importing conftest can never crash CI; the
live fidelity gate self-skips when the reference files under REFERENCE_ROOT are
absent (which they are on the standalone checkout).

Decision reference: probe-untracked-files-before-import-commit discipline -
tracking a file that crashes on a different deploy path is the exact failure
mode Phase 0 exists to close.
"""
from __future__ import annotations

import pathlib

from tests.render.conftest import resolve_workspace_root


def _make_workspace(root: pathlib.Path) -> pathlib.Path:
    """Create a CLAUDE.md + projects/ marker pair under root and return root."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "CLAUDE.md").write_text("# marker\n", encoding="utf-8")
    (root / "projects").mkdir()
    return root


def test_resolve_returns_marked_ancestor(tmp_path: pathlib.Path) -> None:
    """A start path nested under a CLAUDE.md+projects dir resolves to that dir."""
    ws = _make_workspace(tmp_path / "ws")
    start = ws / "projects" / "Resemblio" / "code" / "api" / "tests" / "render"
    start.mkdir(parents=True)
    resolved = resolve_workspace_root(start=start, env_value=None)
    assert resolved == ws


def test_resolve_returns_none_when_no_markers(tmp_path: pathlib.Path) -> None:
    """No CLAUDE.md+projects ancestor returns None - it must NOT raise.

    This is the CI-checkout case: the repo is rooted at code/api with no
    workspace marker above it. Returning None lets the module-level binding
    apply a fallback instead of crashing collection.
    """
    start = tmp_path / "repo" / "tests" / "render"
    start.mkdir(parents=True)
    resolved = resolve_workspace_root(start=start, env_value=None)
    assert resolved is None


def test_resolve_honors_env_override(tmp_path: pathlib.Path) -> None:
    """A WORKSPACE_ROOT env value pointing at a CLAUDE.md dir wins."""
    ws = tmp_path / "explicit"
    ws.mkdir()
    (ws / "CLAUDE.md").write_text("# marker\n", encoding="utf-8")
    start = tmp_path / "elsewhere" / "tests" / "render"
    start.mkdir(parents=True)
    resolved = resolve_workspace_root(start=start, env_value=str(ws))
    assert resolved == ws.resolve()


def test_resolve_ignores_env_without_marker(tmp_path: pathlib.Path) -> None:
    """An env value lacking CLAUDE.md falls through to the ancestor walk."""
    ws = _make_workspace(tmp_path / "ws")
    start = ws / "projects" / "code" / "api" / "tests" / "render"
    start.mkdir(parents=True)
    bogus = tmp_path / "no-marker-here"
    bogus.mkdir()
    resolved = resolve_workspace_root(start=start, env_value=str(bogus))
    assert resolved == ws


def test_module_level_binding_does_not_raise() -> None:
    """Importing conftest binds WORKSPACE_ROOT/REFERENCE_ROOT/CORPUS_ROOT without crashing.

    The import already happened (this module imports from conftest), so reaching
    this assertion at all proves the module-level resolution did not raise. The
    explicit attribute access pins the contract that all three names exist.
    Phase 8 adds CORPUS_ROOT.
    """
    from tests.render import conftest

    assert isinstance(conftest.WORKSPACE_ROOT, pathlib.Path)
    assert isinstance(conftest.REFERENCE_ROOT, pathlib.Path)
    assert isinstance(conftest.CORPUS_ROOT, pathlib.Path)


# ---------------------------------------------------------------------------
# Phase 8: resolve_corpus_root unit tests
# ---------------------------------------------------------------------------


from tests.render.conftest import resolve_corpus_root  # noqa: E402


def test_resolve_corpus_root_prefers_in_repo_when_tolerance_present(
    tmp_path: pathlib.Path,
) -> None:
    """In-repo corpus wins when tolerance_config.yml is present there.

    Phase 8 precedence rule 1: the vendored in-repo copy takes priority so
    the structural gate runs on any checkout (CI or dev).
    """
    in_repo = tmp_path / "reference_corpus"
    in_repo.mkdir()
    (in_repo / "tolerance_config.yml").write_text("schema_version: v1\n")

    ref_root = tmp_path / "workspace" / "_verification" / "corpus"
    ref_root.mkdir(parents=True)
    (ref_root / "tolerance_config.yml").write_text("schema_version: workspace\n")

    result = resolve_corpus_root(in_repo_dir=in_repo, reference_root=ref_root)
    assert result == in_repo


def test_resolve_corpus_root_falls_back_to_workspace_when_in_repo_absent(
    tmp_path: pathlib.Path,
) -> None:
    """Falls back to REFERENCE_ROOT when in-repo corpus lacks tolerance_config.yml.

    Phase 8 precedence rule 2: dev machines running a full gate sweep use the
    workspace _verification/ tree (which has PNGs + full manifest).
    """
    in_repo = tmp_path / "reference_corpus"
    in_repo.mkdir()  # Exists but lacks tolerance_config.yml.

    ref_root = tmp_path / "workspace" / "_verification"
    ref_root.mkdir(parents=True)
    (ref_root / "tolerance_config.yml").write_text("schema_version: workspace\n")

    result = resolve_corpus_root(in_repo_dir=in_repo, reference_root=ref_root)
    assert result == ref_root


def test_resolve_corpus_root_returns_in_repo_when_both_absent(
    tmp_path: pathlib.Path,
) -> None:
    """Returns in_repo_dir even when neither corpus exists.

    Phase 8 precedence rule 3: tests derive non-existent paths from the
    returned root and self-skip via load_tolerance / load_manifest guards.
    Returning in_repo (not raising) is the safe-import contract.
    """
    in_repo = tmp_path / "reference_corpus"  # Does not exist.
    result = resolve_corpus_root(in_repo_dir=in_repo, reference_root=None)
    assert result == in_repo


def test_resolve_corpus_root_returns_in_repo_when_workspace_also_missing(
    tmp_path: pathlib.Path,
) -> None:
    """Workspace reference_root=None (absent) yields in_repo fallback."""
    in_repo = tmp_path / "reference_corpus"  # Does not exist.
    result = resolve_corpus_root(in_repo_dir=in_repo, reference_root=None)
    assert result == in_repo


# ---------------------------------------------------------------------------
# Phase 8: resolve_manifest_path unit tests (live-sweep opt-in policy)
# ---------------------------------------------------------------------------


from tests.render.conftest import resolve_manifest_path  # noqa: E402


def test_resolve_manifest_path_default_uses_in_repo_corpus(
    tmp_path: pathlib.Path,
) -> None:
    """Default (opt-in False) resolves the manifest under the in-repo corpus.

    The in-repo corpus has the manifest JSON but no PNGs, so the live sweep
    finds zero records and SKIPS - the safe default for CI and routine dev runs.
    """
    corpus = tmp_path / "reference_corpus"
    reference = tmp_path / "workspace_verification"
    result = resolve_manifest_path(
        corpus_root=corpus,
        reference_root=reference,
        live_sweep_opt_in=False,
    )
    assert result == corpus / "reference_captures" / "manifest.json"


def test_resolve_manifest_path_opt_in_uses_workspace_reference(
    tmp_path: pathlib.Path,
) -> None:
    """Opt-in True resolves the manifest under the workspace reference root.

    The workspace tree carries the brand-site PNGs co-located with the
    manifest, so the live sweep can actually run. This is the explicit
    gate-run-box / scheduled-job mode (FIDELITY_LIVE_SWEEP=1).
    """
    corpus = tmp_path / "reference_corpus"
    reference = tmp_path / "workspace_verification"
    result = resolve_manifest_path(
        corpus_root=corpus,
        reference_root=reference,
        live_sweep_opt_in=True,
    )
    assert result == reference / "reference_captures" / "manifest.json"


def test_resolve_manifest_path_is_pure_no_disk_dependency(
    tmp_path: pathlib.Path,
) -> None:
    """resolve_manifest_path returns a path without touching the filesystem.

    Neither root exists on disk; the function still returns the correct
    derived path. Pins the pure-function contract (mirrors resolve_corpus_root
    and resolve_workspace_root) so a future refactor cannot smuggle in an
    is_file() check that would change behavior based on disk state.
    """
    corpus = tmp_path / "nope_corpus"
    reference = tmp_path / "nope_reference"
    assert not corpus.exists() and not reference.exists()
    default = resolve_manifest_path(
        corpus_root=corpus, reference_root=reference, live_sweep_opt_in=False,
    )
    opted = resolve_manifest_path(
        corpus_root=corpus, reference_root=reference, live_sweep_opt_in=True,
    )
    assert default == corpus / "reference_captures" / "manifest.json"
    assert opted == reference / "reference_captures" / "manifest.json"
