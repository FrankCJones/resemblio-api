"""Unit tests for ``app.runtime_data``.

Covers the runtime-vs-seed split that moves API-written extraction output
out of the git-tracked working tree. Bug history: 2026-06-03 CI deploys
broke on ``git reset --hard origin/main`` because runtime-owned files lived
in a tracked directory the deploy user could not unlink.

Pure unit tests; no filesystem outside ``tmp_path``, no network.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture()
def rt_mod(monkeypatch: pytest.MonkeyPatch):
    """Re-import the module after clearing the env so each test starts clean.

    The module reads env at call time, not import time, so a fresh import
    is not strictly required, but doing it here means a future change that
    moves a read to import time fails loudly in tests rather than silently
    on prod.
    """
    monkeypatch.delenv("RESEMBLIO_RUNTIME_DATA_ROOT", raising=False)
    monkeypatch.delenv("RESEMBLIO_SEED_DATA_ROOT", raising=False)
    import app.runtime_data as runtime_data

    return importlib.reload(runtime_data)


# --- runtime_root ------------------------------------------------------------


def test_runtime_root_defaults_to_var_lib_resemblio_when_env_unset(
    rt_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RESEMBLIO_RUNTIME_DATA_ROOT", raising=False)
    assert rt_mod.runtime_root() == Path("/var/lib/resemblio")


def test_runtime_root_reads_env_when_set(
    rt_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", str(tmp_path))
    assert rt_mod.runtime_root() == tmp_path


def test_runtime_root_rejects_relative_paths(
    rt_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relative paths resolve against cwd at call time; that bit us in 2026-06."""
    monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", "relative/path")
    with pytest.raises(RuntimeError, match="absolute"):
        rt_mod.runtime_root()


def test_runtime_root_treats_empty_env_as_unset(
    rt_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", "")
    assert rt_mod.runtime_root() == Path("/var/lib/resemblio")


# --- seed_root ---------------------------------------------------------------


def test_seed_root_defaults_to_vendored_drl_data(rt_mod) -> None:
    """Default seed root sits inside the api tree at _vendored/drl/drl/_data."""
    expected_tail = Path("_vendored") / "drl" / "drl" / "_data"
    got = rt_mod.seed_root()
    assert got.parts[-len(expected_tail.parts):] == expected_tail.parts


def test_seed_root_overridable_for_tests(
    rt_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RESEMBLIO_SEED_DATA_ROOT", str(tmp_path))
    assert rt_mod.seed_root() == tmp_path


# --- resolve_read_path -------------------------------------------------------


def test_resolve_read_path_prefers_runtime_when_both_exist(
    rt_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    seed = tmp_path / "seed"
    (runtime / "computed_styles").mkdir(parents=True)
    (seed / "computed_styles").mkdir(parents=True)
    (runtime / "computed_styles" / "apple.json").write_text("RUNTIME", encoding="utf-8")
    (seed / "computed_styles" / "apple.json").write_text("SEED", encoding="utf-8")
    monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", str(runtime))
    monkeypatch.setenv("RESEMBLIO_SEED_DATA_ROOT", str(seed))

    found = rt_mod.resolve_read_path("computed_styles", "apple.json")

    assert found is not None
    assert found.read_text(encoding="utf-8") == "RUNTIME"


def test_resolve_read_path_falls_back_to_seed_when_runtime_missing(
    rt_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    seed = tmp_path / "seed"
    runtime.mkdir()
    (seed / "computed_styles").mkdir(parents=True)
    (seed / "computed_styles" / "apple.json").write_text("SEED", encoding="utf-8")
    monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", str(runtime))
    monkeypatch.setenv("RESEMBLIO_SEED_DATA_ROOT", str(seed))

    found = rt_mod.resolve_read_path("computed_styles", "apple.json")

    assert found is not None
    assert found.read_text(encoding="utf-8") == "SEED"


def test_resolve_read_path_returns_none_when_neither_exists(
    rt_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    seed = tmp_path / "seed"
    runtime.mkdir()
    seed.mkdir()
    monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", str(runtime))
    monkeypatch.setenv("RESEMBLIO_SEED_DATA_ROOT", str(seed))

    assert rt_mod.resolve_read_path("computed_styles", "apple.json") is None


# --- resolve_write_path ------------------------------------------------------


def test_resolve_write_path_returns_runtime_target(
    rt_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", str(tmp_path))
    got = rt_mod.resolve_write_path("computed_styles", "apple.json")
    assert got == tmp_path / "computed_styles" / "apple.json"


def test_resolve_write_path_creates_parent_dir(
    rt_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", str(tmp_path))
    rt_mod.resolve_write_path("computed_styles", "apple.json")
    assert (tmp_path / "computed_styles").is_dir()


def test_resolve_write_path_does_not_touch_seed_root(
    rt_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point of the split: writes never land in the git tree."""
    runtime = tmp_path / "runtime"
    seed = tmp_path / "seed"
    seed.mkdir()
    monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", str(runtime))
    monkeypatch.setenv("RESEMBLIO_SEED_DATA_ROOT", str(seed))

    target = rt_mod.resolve_write_path("computed_styles", "apple.json")

    assert seed not in target.parents
    assert runtime in target.parents
    assert not (seed / "computed_styles").exists()
