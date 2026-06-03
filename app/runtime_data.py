"""Runtime-data root for files written by the running API.

Why this module exists
----------------------
Until 2026-06-03 the API wrote per-brand computed-style snapshots into the
git-tracked vendored tree at
``_vendored/drl/drl/_data/computed_styles/<slug>.json``. The running service
(systemd unit ``resemblio-api.service``) owns these writes; the deploy user
(``claude-cowork``) owns the git checkout. When CI ran
``git reset --hard origin/main`` as the deploy user, the runtime-owned files
in the tracked directory blocked the reset with ``Permission denied``,
breaking every deploy.

The structural fix splits code from data on disk.

- Code lives at ``/opt/resemblio-api/app/`` (git-managed, deploy-user-owned).
- Runtime data lives at ``/var/lib/resemblio/`` (service-user-owned, NOT git-tracked).

This module is the single source of truth for the runtime-data root. Every
write path runs through ``resolve_write_path``; every read path runs through
``resolve_read_path``, which falls back to the seed directory inside the git
tree when no runtime copy exists yet. The seed directory keeps shipping
baseline fixtures (the ``.gitkeep``, plus any committed reference snapshots)
without participating in the runtime-write story.

Env contract
------------
- ``RESEMBLIO_RUNTIME_DATA_ROOT`` (optional): absolute path for runtime data.
  Defaults to ``RUNTIME_DATA_ROOT_DEFAULT``. A relative value is rejected
  (raises ``RuntimeError`` at first ``runtime_root()`` call) to prevent the
  silent ``cwd``-dependent failure mode the bootstrap script hit on 2026-06.

- ``RESEMBLIO_SEED_DATA_ROOT`` (optional, primarily for tests): override the
  in-tree seed root. Defaults to ``_vendored/drl/drl/_data/`` resolved
  against this file's location.

Subdirectory layout
-------------------
Subdirectories mirror between runtime and seed roots:

::

    <runtime_root>/computed_styles/<slug>.json   <-- runtime writes here
    <seed_root>/computed_styles/<slug>.json      <-- baseline / fallback

Reads try runtime first, then seed. Writes go to runtime only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Final


# Default runtime root on prod. systemd's ``StateDirectory=resemblio``
# creates and chowns this path to the service user automatically on unit
# start, so an explicit migration is only needed for boxes that ran the
# pre-2026-06-03 code path. See ``scripts/migrate_runtime_data.sh``.
RUNTIME_DATA_ROOT_DEFAULT: Final[Path] = Path("/var/lib/resemblio")
"""Default for ``RESEMBLIO_RUNTIME_DATA_ROOT`` on prod."""

RUNTIME_DATA_ROOT_ENV: Final[str] = "RESEMBLIO_RUNTIME_DATA_ROOT"
"""Env var name. Centralized so a search-rename catches every reader."""

SEED_DATA_ROOT_ENV: Final[str] = "RESEMBLIO_SEED_DATA_ROOT"
"""Env var name for the seed root override (tests + dev)."""

# Resolved at import time from this file's location: app/runtime_data.py lives
# at ``<api_root>/app/runtime_data.py`` and the seed tree is at
# ``<api_root>/_vendored/drl/drl/_data/``.
_DEFAULT_SEED_ROOT: Final[Path] = (
    Path(__file__).resolve().parents[1] / "_vendored" / "drl" / "drl" / "_data"
)

# Subdirectory names. Add to this list as new runtime-data categories appear;
# the names are mirrored across runtime and seed roots.
COMPUTED_STYLES_SUBDIR: Final[str] = "computed_styles"


def runtime_root() -> Path:
    """Return the runtime-data root for this process.

    Reads ``RESEMBLIO_RUNTIME_DATA_ROOT`` each call so test monkeypatches
    against the env take effect without a module reload. The value must be
    an absolute path; a relative value raises ``RuntimeError`` rather than
    silently resolving against ``cwd`` (the 2026-06 bootstrap-relative bug
    pattern).
    """
    raw = os.environ.get(RUNTIME_DATA_ROOT_ENV)
    if raw is None or raw.strip() == "":
        return RUNTIME_DATA_ROOT_DEFAULT
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise RuntimeError(
            f"{RUNTIME_DATA_ROOT_ENV} must be an absolute path; got {raw!r}. "
            "Relative paths resolve against cwd at call time, which differs "
            "between systemd, manual ssh, and tests."
        )
    return candidate


def seed_root() -> Path:
    """Return the in-tree seed-data root.

    Defaults to ``<api_root>/_vendored/drl/drl/_data/``. Tests override via
    ``RESEMBLIO_SEED_DATA_ROOT`` so the read-fallback path can be exercised
    against synthetic fixtures.
    """
    raw = os.environ.get(SEED_DATA_ROOT_ENV)
    if raw is None or raw.strip() == "":
        return _DEFAULT_SEED_ROOT
    return Path(raw)


def resolve_read_path(subdir: str, name: str) -> Path | None:
    """Return the first existing path for ``<subdir>/<name>``, or ``None``.

    Lookup order: runtime root first (preferred when the running service has
    written a current copy), then seed root (baseline shipped in the git
    tree). Returns ``None`` when neither exists; callers treat that as
    "no data yet" rather than raising, mirroring the snapshot loader's
    fail-safe contract.
    """
    runtime_candidate = runtime_root() / subdir / name
    if runtime_candidate.exists():
        return runtime_candidate
    seed_candidate = seed_root() / subdir / name
    if seed_candidate.exists():
        return seed_candidate
    return None


def resolve_write_path(subdir: str, name: str, *, mkdir: bool = True) -> Path:
    """Return the runtime-root write path for ``<subdir>/<name>``.

    Writes never go to the seed root; that path is reserved for git-tracked
    baseline data and would re-introduce the deploy-blocking ownership
    conflict if the running service wrote there. When ``mkdir`` is True (the
    default) the parent directory is created with ``exist_ok=True`` so
    callers don't need a separate ``mkdir`` step.
    """
    target = runtime_root() / subdir / name
    if mkdir:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def runtime_subdir(subdir: str, *, mkdir: bool = True) -> Path:
    """Return the runtime root for ``<subdir>``, creating it if requested.

    Used by callers (capture script) that need a directory handle rather
    than a per-file path.
    """
    target = runtime_root() / subdir
    if mkdir:
        target.mkdir(parents=True, exist_ok=True)
    return target


def seed_subdir(subdir: str) -> Path:
    """Return the seed root for ``<subdir>``. Does not create.

    Provided so callers that legitimately need the seed location for a
    read-only purpose (e.g. logging which fallback path was used) don't
    reach into module internals.
    """
    return seed_root() / subdir
