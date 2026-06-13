"""Durable repo-integrity guards: tracked code must not depend on untracked data files.

Purpose
-------
A tracked module that imports or loads a data file (YAML, JSON, etc.) at
runtime will break on a fresh ``git clone`` if that data file is untracked.
This test catches that class of bug by asserting that every known
"tracked-consumer -> required-data-file" pair has the data file tracked.

Run this test in your offline suite as part of ``pytest`` - it requires only
``git`` on PATH and exits fast (all checks are pure shell invocations).

Dependencies: git (on PATH), pytest
Run: python -m pytest tests/test_repo_integrity.py

Scope
-----
The parametrized list covers the concrete pairs known at the time this guard
was written (2026-06-13, Library v5 Phase 6 pre-flip hygiene per
_HANDOFF_2026-06-13_library-v5-phase6-preflip-hygiene.md). When you add a
tracked module that loads a data file, add the pair to CONSUMER_DATA_PAIRS
so future authors trip this guard if they forget to track the file.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Repo root
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Pairs to guard: (consumer_module, required_data_file)
# Each data file must be tracked in git for the repo to be runnable from a
# fresh clone. Add a row here whenever a new tracked module gains an
# untracked data-file dependency.
# ---------------------------------------------------------------------------

CONSUMER_DATA_PAIRS: list[tuple[str, str]] = [
    # site_classifier.py loads site_classifier_signals.yml at import time.
    # Without the YAML the module raises FileNotFoundError on any import.
    (
        "app/site_classifier.py",
        "app/site_classifier_signals.yml",
    ),
]


def _is_tracked(path: str) -> bool:
    """Return True if *path* (repo-relative) is tracked by git."""
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", path],
        cwd=_REPO_ROOT,
        capture_output=True,
    )
    return result.returncode == 0


@pytest.mark.parametrize("consumer,data_file", CONSUMER_DATA_PAIRS)
def test_tracked_code_has_no_untracked_data_deps(consumer: str, data_file: str) -> None:
    """A tracked module must not depend on an untracked data file.

    A fresh clone of origin/main must be runnable. If *consumer* is tracked
    but *data_file* is untracked, a fresh clone would fail at import time.

    To add a new pair, append it to CONSUMER_DATA_PAIRS in this file.
    """
    assert _is_tracked(consumer), (
        f"Integrity check misconfigured: consumer {consumer!r} is itself untracked. "
        "The consumer must be a tracked file for this guard to make sense."
    )
    assert _is_tracked(data_file), (
        f"Broken-on-clone dependency detected: {consumer!r} depends on "
        f"{data_file!r} but that file is not tracked by git. "
        "Add the data file to git before merging."
    )
