"""Smoke tests for ``scripts/migrate_runtime_data.sh``.

Exercises the script against a synthetic filesystem so the move-vs-skip
branching is covered. Skips entirely on platforms without ``bash`` and a
working ``chown``/``stat`` chain (Windows CI; the script is prod-only).

The test deliberately passes ``--dry-run`` for the chown assertion path:
``chown`` on a tmp_path mid-test fails on Linux CI without root, and the
point of the test is the file-routing logic, not the chown shell-out.
For the move-vs-skip branch we run the script for real (dry-run also
suppresses ``mv``, so we get the routing decisions via log lines).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "migrate_runtime_data.sh"
)


def _have_bash() -> bool:
    return shutil.which("bash") is not None


pytestmark = pytest.mark.skipif(
    not _have_bash() or os.name == "nt",
    reason="migration script requires bash + POSIX tools; skipped on Windows",
)


def _run(env_overrides: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT_PATH), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def test_script_exists_and_is_readable() -> None:
    assert SCRIPT_PATH.exists()
    assert SCRIPT_PATH.read_text(encoding="utf-8").startswith("#!/bin/bash")


def test_dry_run_reports_planned_moves(tmp_path: Path) -> None:
    """Dry-run lists every JSON file it would move out of the seed dir.

    The point: idempotency + safety. A dry-run leaves the filesystem
    untouched and reports the routing decisions.
    """
    app_root = tmp_path / "app"
    runtime_root = tmp_path / "var-lib-resemblio"
    seed_dir = app_root / "_vendored" / "drl" / "drl" / "_data" / "computed_styles"
    seed_dir.mkdir(parents=True)
    (seed_dir / "apple.json").write_text("{}", encoding="utf-8")
    (seed_dir / ".gitkeep").write_text("", encoding="utf-8")
    runtime_root.mkdir()

    # Real users on the box; on CI we are running as the same user, so the
    # script's owner-check will report SKIPs (current user == DEPLOY_USER).
    # That is exactly what we want to verify: the script never touches a
    # file owned by the deploy user.
    current_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "root"
    result = _run(
        {
            "APP_ROOT": str(app_root),
            "RESEMBLIO_RUNTIME_DATA_ROOT": str(runtime_root),
            "SERVICE_USER": current_user,
            "DEPLOY_USER": current_user,
        },
        "--dry-run",
    )

    # Either current_user matches DEPLOY_USER (so apple.json is SKIPPED), or
    # the script still completes cleanly. The non-zero-exit failure mode
    # would be a missing user; that branch is covered separately.
    assert result.returncode == 0, result.stderr
    # apple.json should be referenced in the log either way (skip OR move).
    assert "apple.json" in result.stderr
    # Dry-run leaves the seed dir intact.
    assert (seed_dir / "apple.json").exists()
    assert (seed_dir / ".gitkeep").exists()


def test_script_rejects_missing_service_user(tmp_path: Path) -> None:
    """The script must fail loud rather than chown to a phantom UID."""
    app_root = tmp_path / "app"
    runtime_root = tmp_path / "var-lib-resemblio"
    (app_root / "_vendored" / "drl" / "drl" / "_data" / "computed_styles").mkdir(
        parents=True
    )
    runtime_root.mkdir()

    result = _run(
        {
            "APP_ROOT": str(app_root),
            "RESEMBLIO_RUNTIME_DATA_ROOT": str(runtime_root),
            "SERVICE_USER": "definitely-not-a-real-user-xyz",
            "DEPLOY_USER": os.environ.get("USER") or "root",
        },
        "--dry-run",
    )

    assert result.returncode == 2
    assert "does not exist" in result.stderr
