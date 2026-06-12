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
    """Importing conftest binds WORKSPACE_ROOT/REFERENCE_ROOT without crashing.

    The import already happened (this module imports from conftest), so reaching
    this assertion at all proves the module-level resolution did not raise. The
    explicit attribute access pins the contract that both names exist.
    """
    from tests.render import conftest

    assert isinstance(conftest.WORKSPACE_ROOT, pathlib.Path)
    assert isinstance(conftest.REFERENCE_ROOT, pathlib.Path)
