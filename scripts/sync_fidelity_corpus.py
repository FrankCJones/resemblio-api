"""Sync the structural fidelity text corpus from the workspace authoring tree
into the in-repo CI mirror at tests/render/reference_corpus/.

Purpose
-------
The workspace ``_verification/library-inspirado-correction-20260604/`` directory
is the authoring source for the structural fidelity corpus. When a gate run
updates the specs (or when new brand specs are added), this script re-syncs
the in-repo mirror so CI picks up the changes. Run it from the workspace root
after a gate-run update, then commit the result.

What is copied
--------------
- ``tolerance_config.yml``
- ``fidelity_targets.yml``
- ``reference_captures/manifest.json``
- ``reference_captures/specs/*.json``

What is NEVER copied
--------------------
- ``*.png`` - brand-site screenshots. Public-repo trademark constraint; SSIM is
  informational-only (D-5.1 locked 2026-06-13). The sync helper hard-fails if a
  caller accidentally passes a PNG source.

Dependencies
------------
Python standard library only (shutil, pathlib, json). No network.

Run command (from workspace root)
----------------------------------
    python projects/Resemblio/code/api/scripts/sync_fidelity_corpus.py

Optional: --dry-run to show what would be copied without writing.

Schema: sync_fidelity_corpus_v1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import pathlib
import shutil
import sys
from typing import List, NamedTuple

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_log = logging.getLogger("sync_fidelity_corpus")

# Script version for audit logs.
SCRIPT_VERSION = "1.0.0"


class SyncItem(NamedTuple):
    """A single file to be synced from source to destination."""

    src: pathlib.Path
    dst: pathlib.Path


# ---------------------------------------------------------------------------
# Pure copy logic (no filesystem side-effects; unit-tested)
# ---------------------------------------------------------------------------


def build_sync_plan(
    workspace_corpus_root: pathlib.Path,
    in_repo_corpus_root: pathlib.Path,
) -> List[SyncItem]:
    """Build the list of (src, dst) pairs to sync.

    Pure function: reads directory contents but writes nothing. Hard-fails with
    ``ValueError`` if any PNG files are discovered in the source tree (defense
    against accidental PNG vendoring into the public repo).

    Args:
        workspace_corpus_root: Workspace authoring root (REFERENCE_ROOT).
            Must contain ``tolerance_config.yml`` and ``reference_captures/``.
        in_repo_corpus_root: In-repo mirror root (tests/render/reference_corpus/).

    Returns:
        List of SyncItem(src, dst) in a stable order. Empty when source tree is
        absent (the caller may treat that as an error).

    Raises:
        ValueError: If any ``*.png`` file is found in the source scan.
    """
    plan: List[SyncItem] = []

    # Top-level text files.
    for name in ("tolerance_config.yml", "fidelity_targets.yml"):
        src = workspace_corpus_root / name
        if src.is_file():
            plan.append(SyncItem(src=src, dst=in_repo_corpus_root / name))

    # reference_captures/manifest.json
    manifest_src = workspace_corpus_root / "reference_captures" / "manifest.json"
    if manifest_src.is_file():
        plan.append(SyncItem(
            src=manifest_src,
            dst=in_repo_corpus_root / "reference_captures" / "manifest.json",
        ))

    # reference_captures/specs/*.json
    specs_src_dir = workspace_corpus_root / "reference_captures" / "specs"
    if specs_src_dir.is_dir():
        for src in sorted(specs_src_dir.glob("*.json")):
            plan.append(SyncItem(
                src=src,
                dst=in_repo_corpus_root / "reference_captures" / "specs" / src.name,
            ))

    # PNG guard: scan the source tree for any PNG. The workspace _verification/
    # dir contains reference screenshots; they must not enter the sync plan.
    png_sources = [item.src for item in plan if item.src.suffix.lower() == ".png"]
    if png_sources:
        raise ValueError(
            f"PNG files found in sync plan: {png_sources}. "
            "Brand-site screenshots must NEVER be synced to the in-repo corpus. "
            "Update build_sync_plan() to exclude them explicitly."
        )

    return plan


def files_match(src: pathlib.Path, dst: pathlib.Path) -> bool:
    """Return True when src and dst exist and have identical content.

    Uses MD5 for speed (not security). Pure comparison; no writes.
    """
    if not dst.exists():
        return False
    src_digest = hashlib.md5(src.read_bytes(), usedforsecurity=False).hexdigest()
    dst_digest = hashlib.md5(dst.read_bytes(), usedforsecurity=False).hexdigest()
    return src_digest == dst_digest


def execute_sync(
    plan: List[SyncItem],
    *,
    dry_run: bool = False,
) -> dict:
    """Execute a sync plan, returning a summary dict.

    Args:
        plan: List of (src, dst) pairs from ``build_sync_plan``.
        dry_run: When True, log what would be done but write nothing.

    Returns:
        Dict with keys ``copied`` (int), ``skipped`` (int), ``total`` (int).
    """
    copied = 0
    skipped = 0
    for item in plan:
        if files_match(item.src, item.dst):
            _log.debug("skip (identical): %s", item.dst.name)
            skipped += 1
            continue
        if dry_run:
            _log.info("would copy: %s -> %s", item.src, item.dst)
            copied += 1
            continue
        item.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.src, item.dst)
        _log.info("copied: %s -> %s", item.src.name, item.dst)
        copied += 1
    return {"copied": copied, "skipped": skipped, "total": len(plan)}


# ---------------------------------------------------------------------------
# Workspace and repo root discovery
# ---------------------------------------------------------------------------


def find_workspace_root(start: pathlib.Path) -> pathlib.Path:
    """Walk up from start to find the workspace root (CLAUDE.md + projects/).

    Raises ``RuntimeError`` when no workspace root is found above start. Callers
    catch this and print a user-friendly message.
    """
    for parent in (start, *start.parents):
        if (parent / "CLAUDE.md").is_file() and (parent / "projects").is_dir():
            return parent
    raise RuntimeError(
        f"Workspace root not found above {start}. "
        "Run this script from the workspace root or a subdirectory of it."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: List[str] | None = None) -> int:
    """Entry point. Returns 0 on success, 1 on error."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be copied without writing anything.",
    )
    args = parser.parse_args(argv)

    try:
        workspace_root = find_workspace_root(pathlib.Path.cwd())
    except RuntimeError as exc:
        _log.error("%s", exc)
        return 1

    workspace_corpus = (
        workspace_root
        / "projects"
        / "Resemblio"
        / "_verification"
        / "library-inspirado-correction-20260604"
    )
    in_repo_corpus = (
        workspace_root
        / "projects"
        / "Resemblio"
        / "code"
        / "api"
        / "tests"
        / "render"
        / "reference_corpus"
    )

    if not workspace_corpus.is_dir():
        _log.error("Workspace corpus not found at %s", workspace_corpus)
        return 1

    _log.info("source: %s", workspace_corpus)
    _log.info("destination: %s", in_repo_corpus)

    try:
        plan = build_sync_plan(workspace_corpus, in_repo_corpus)
    except ValueError as exc:
        _log.error("sync plan rejected: %s", exc)
        return 1

    if not plan:
        _log.warning("Sync plan is empty - source tree may be missing expected files.")
        return 1

    summary = execute_sync(plan, dry_run=args.dry_run)
    _log.info(
        "%s%d copied, %d skipped, %d total",
        "[dry-run] " if args.dry_run else "",
        summary["copied"],
        summary["skipped"],
        summary["total"],
    )

    if not args.dry_run and summary["copied"] > 0:
        _log.info("Corpus synced. Commit tests/render/reference_corpus/ to update CI.")

    # Emit a machine-readable summary to stdout for scripting.
    print(json.dumps({"schema_version": "sync_fidelity_corpus_v1", **summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
