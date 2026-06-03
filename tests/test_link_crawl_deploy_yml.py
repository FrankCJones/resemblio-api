"""Asserts the link-crawl smoke step is wired into deploy.yml in the right
position (AFTER /v1/readyz, BEFORE the security-header check).

Paired with the standing-PR-gate contract locked 2026-06-03. If a future
careless edit removes the gate or moves it before /v1/readyz, this test
fails at pytest time before the deploy.yml lint gets a chance to.
"""
from __future__ import annotations

from pathlib import Path

import yaml

DEPLOY_YML = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy.yml"


def _build_steps() -> list[dict]:
    with DEPLOY_YML.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data["jobs"]["build-and-deploy"]["steps"]


def test_link_crawl_step_present() -> None:
    names = [s.get("name", "") for s in _build_steps()]
    assert any("Link-crawl smoke" in n for n in names), names


def test_link_crawl_step_runs_after_readyz() -> None:
    names = [s.get("name", "") for s in _build_steps()]
    readyz_idx = next(
        i for i, n in enumerate(names) if "Post-deploy smoke" in n
    )
    link_crawl_idx = next(
        i for i, n in enumerate(names) if "Link-crawl smoke" in n
    )
    assert link_crawl_idx > readyz_idx, (
        f"link-crawl smoke must run AFTER /v1/readyz "
        f"(found readyz={readyz_idx}, link_crawl={link_crawl_idx})"
    )


def test_link_crawl_step_invokes_script() -> None:
    step = next(
        s for s in _build_steps() if "Link-crawl smoke" in s.get("name", "")
    )
    assert "scripts/link_crawl_smoke.py" in step["run"]
    assert "--surfaces" in step["run"]


def test_yaml_parses_cleanly() -> None:
    with DEPLOY_YML.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert "jobs" in data
    assert "build-and-deploy" in data["jobs"]


# --- Vendored surfaces.yml gate ----------------------------------------
#
# The GitHub Actions runner checks out only this repo (rooted at what is
# `code/api/` in the workspace), so the canonical workspace path
# `../../surfaces.yml` does NOT exist on CI. The repo carries its own
# copy at the repo root (`code/api/surfaces.yml` from the workspace
# perspective). These tests assert:
#   1. The vendored file exists.
#   2. The deploy.yml step points at the in-repo path, not the
#      workspace-relative `../../surfaces.yml` that broke CI on
#      2026-06-03.
# A workspace-side parity check (byte-identical to
# `projects/Resemblio/surfaces.yml`) is not enforced here because CI
# does not have the workspace tree; parity is enforced by Builder
# discipline at edit time. See deploy.yml block comment.

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDORED_SURFACES = REPO_ROOT / "surfaces.yml"


def test_vendored_surfaces_yml_exists() -> None:
    assert VENDORED_SURFACES.is_file(), (
        f"Vendored surfaces.yml missing at {VENDORED_SURFACES}. "
        "CI relies on this file because the runner does not have "
        "access to the workspace-level canonical copy at "
        "projects/Resemblio/surfaces.yml."
    )


def test_vendored_surfaces_yml_parses_with_expected_shape() -> None:
    with VENDORED_SURFACES.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert data.get("schema_version") == 1
    assert data.get("project") == "resemblio"
    surfaces = data.get("surfaces") or []
    assert isinstance(surfaces, list) and surfaces, "surfaces list empty"
    names = {s.get("name") for s in surfaces}
    assert {"resemblio-web", "resemblio-api"}.issubset(names), names


def test_link_crawl_step_points_at_in_repo_surfaces() -> None:
    """The deploy step MUST pass `--surfaces ./surfaces.yml` (or the
    equivalent in-repo path), NOT `../../surfaces.yml`. The latter
    resolves outside the CI checkout root and fails the gate with
    `surfaces.yml not found` even though the deploy itself succeeded
    (root cause of the 2026-06-03 red-after-green incident).
    """
    step = next(
        s for s in _build_steps() if "Link-crawl smoke" in s.get("name", "")
    )
    run_block = step["run"]
    assert "../../surfaces.yml" not in run_block, (
        "deploy.yml link-crawl step references ../../surfaces.yml; "
        "that path does not exist on the CI runner (only the "
        "resemblio-api repo is checked out). Use ./surfaces.yml "
        "and keep the vendored copy in sync with the workspace "
        "canonical at projects/Resemblio/surfaces.yml."
    )
    assert "./surfaces.yml" in run_block, (
        "deploy.yml link-crawl step must pass --surfaces ./surfaces.yml "
        "to read the vendored in-repo registry."
    )
