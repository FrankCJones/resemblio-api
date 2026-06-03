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
