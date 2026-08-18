"""Vendor a read-only pinned snapshot of DRL asset content into
``_vendored/drl_corpus/``, mirroring the DRL on-disk layout so the seed
script and fidelity gate can run on a bare CI checkout without the workspace
DRL tree.

What is copied
--------------
- ``corpus.json``                           - the flat asset catalogue
- ``assets/<class>/<slug>/asset.html``      - one per corpus entry
- ``assets/<class>/<slug>/tokens.css``      - one per corpus entry (tokens_path)
- ``systems/<brand>/system.json``           - one per DRL system, when present

What is NEVER copied
--------------------
- PNGs, audio, or any file not referenced by corpus.json
- Anything under the DRL ``_scripts/`` tree (already vendored in _vendored/drl/)
- Any file whose extension is not .html, .css, or .json

The DRL is read-only throughout: this script never writes a single byte
into the DRL path. The ``verify_drl_untouched`` guard enforces this at
runtime as a belt-and-suspenders check.

Idempotency
-----------
Re-running this script against an unchanged DRL produces a byte-identical
``manifest.json`` because:
  1. The manifest contains only content hashes (sha256) - no timestamps.
  2. Files are listed in a stable, sorted order.
  3. Existing files whose content is unchanged are skipped (not re-written).
``VERSION`` is always re-written because it records the new run's timestamp.

Dependencies
------------
Python standard library only (argparse, datetime, hashlib, json, logging,
pathlib, shutil). No network, no third-party packages, no project imports.
``DEFAULT_DRL_ROOT`` is defined locally (mirroring
``scripts/seed_from_drl.py::DEFAULT_DRL_ROOT``) rather than imported, so the
script runs standalone via ``python scripts/sync_drl_corpus.py`` without the
``scripts`` package being importable on ``sys.path``. If the seed script's
default root ever changes, update the constant here to match.

Run command (from code/api/)
----------------------------
    python scripts/sync_drl_corpus.py

Optional: ``--dry-run``   show what would be copied without writing
Optional: ``--drl-root``  override the default workspace DRL path

Schema: sync_drl_corpus_v1
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import pathlib
import shutil
import sys
from typing import NamedTuple

# Mirrors scripts/seed_from_drl.py::DEFAULT_DRL_ROOT.
# parents: [0] scripts/, [1] api/, [2] code/, [3] Resemblio/, [4] projects/
# The DRL lives at projects/../Design Reference Library relative to the workspace.
DEFAULT_DRL_ROOT: pathlib.Path = (
    pathlib.Path(__file__).resolve().parents[4] / "Design Reference Library"
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_log = logging.getLogger("sync_drl_corpus")

SCRIPT_VERSION = "1.0.0"
SCHEMA_VERSION = "sync_drl_corpus_v1"

# Allowed file extensions for vendored content. A hard guard against
# accidental PNG or binary vendoring.
_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".html", ".css", ".json"})

# Destination root within this repo (code/api/_vendored/drl_corpus/).
_DEFAULT_VENDORED: pathlib.Path = (
    pathlib.Path(__file__).resolve().parent.parent / "_vendored" / "drl_corpus"
)


class SyncFile(NamedTuple):
    """A source -> destination pair to copy into the vendored snapshot.

    ``rel`` is the path relative to the vendored root and is used in the
    manifest.  It mirrors the DRL path structure so that
    ``--drl-root _vendored/drl_corpus`` works as a drop-in replacement.
    """

    src: pathlib.Path
    dst: pathlib.Path
    rel: str  # relative path within _vendored/drl_corpus/


# ---------------------------------------------------------------------------
# Pure-logic helpers (no filesystem side-effects; unit-tested in
# tests/test_sync_drl_corpus.py)
# ---------------------------------------------------------------------------


def build_corpus_plan(
    drl_root: pathlib.Path,
    vendored_root: pathlib.Path,
) -> list[SyncFile]:
    """Build the ordered list of (src, dst, rel) pairs to copy from DRL.

    Pure except for reading file existence/content from drl_root; writes
    nothing. Processes in a deterministic order: corpus.json first, then
    system.json entries sorted by brand slug, then per-asset files in
    corpus.json iteration order.

    Args:
        drl_root: Read-only DRL workspace root. Never written.
        vendored_root: Destination root (_vendored/drl_corpus/).

    Returns:
        Ordered list of SyncFile(src, dst, rel).

    Raises:
        FileNotFoundError: corpus.json absent at drl_root.
        ValueError: A planned file has a disallowed extension (safety guard).
    """
    plan: list[SyncFile] = []

    # 1. corpus.json - required anchor file.
    corpus_src = drl_root / "corpus.json"
    if not corpus_src.is_file():
        raise FileNotFoundError(
            f"DRL corpus.json not found at {corpus_src}. "
            "Verify --drl-root points to the Design Reference Library root."
        )
    plan.append(SyncFile(src=corpus_src, dst=vendored_root / "corpus.json", rel="corpus.json"))

    corpus: dict = json.loads(corpus_src.read_text(encoding="utf-8"))

    # 2. systems/<brand>/system.json - sorted by brand slug for stability.
    systems: list[dict] = corpus.get("systems", [])
    for system in sorted(systems, key=lambda s: s.get("slug", "")):
        brand: str = system.get("slug", "")
        if not brand:
            continue
        src = drl_root / "systems" / brand / "system.json"
        if not src.is_file():
            _log.debug("system.json absent for brand %r; skipping", brand)
            continue
        rel = f"systems/{brand}/system.json"
        plan.append(SyncFile(src=src, dst=vendored_root / rel, rel=rel))

    # 3. Per-asset files: asset.html + tokens.css, in corpus.json order.
    #    The corpus order is stable (it is a static JSON file); sorting by rel
    #    additionally ensures the manifest's files[] list is alphabetical,
    #    which makes git diffs readable and confirms idempotency at a glance.
    asset_items: list[SyncFile] = []
    for system in systems:
        for asset in system.get("assets", []):
            asset_path: str = asset.get("path", "")
            tokens_rel: str = asset.get("tokens_path", "")

            if asset_path:
                src = drl_root / asset_path / "asset.html"
                rel = f"{asset_path}/asset.html"
                if src.is_file():
                    asset_items.append(SyncFile(src=src, dst=vendored_root / rel, rel=rel))
                else:
                    _log.warning("asset.html missing at %s; skipping", src)

            if tokens_rel:
                src = drl_root / tokens_rel
                if src.is_file():
                    asset_items.append(SyncFile(src=src, dst=vendored_root / tokens_rel, rel=tokens_rel))
                else:
                    _log.warning("tokens.css missing at %s; skipping", src)

    # Sort asset items by rel path for a stable, alphabetical manifest.
    asset_items.sort(key=lambda sf: sf.rel)
    plan.extend(asset_items)

    # Extension guard: hard-fail if anything other than .html/.css/.json slips in.
    for item in plan:
        ext = pathlib.Path(item.rel).suffix.lower()
        if ext not in _ALLOWED_EXTENSIONS:
            raise ValueError(
                f"Unexpected file extension {ext!r} in sync plan: {item.rel}. "
                "Only .html, .css, and .json files may be vendored into drl_corpus/. "
                "Update _ALLOWED_EXTENSIONS only after explicit review."
            )

    return plan


def sha256_hex(path: pathlib.Path) -> str:
    """Return the hex SHA-256 digest of a file's byte contents.

    Used both by the sync script (to build the manifest) and by the test
    (to re-verify the manifest after the fact).
    """
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def read_lf_bytes(path: pathlib.Path) -> bytes:
    """Return file bytes with CRLF normalised to LF.

    The vendored corpus is committed with LF endings via ``.gitattributes``.
    Normalising before writes keeps Windows sync runs byte-identical to CI
    checkouts, which is required because ``manifest.json`` stores raw file
    hashes.
    """
    return path.read_bytes().replace(b"\r\n", b"\n")

def build_manifest(
    plan: list[SyncFile],
    corpus_meta: dict,
) -> dict:
    """Build the manifest dict from the SyncFile plan.

    Reads the destination files (which must already exist) to compute sha256.
    The manifest is timestamp-free so that re-running the sync with an
    unchanged DRL produces a byte-identical manifest.json.

    Args:
        plan: Ordered SyncFile list from build_corpus_plan.
        corpus_meta: Parsed corpus.json dict (for asset_count / schema_version).

    Returns:
        Dict suitable for JSON serialisation with schema_version, counts, and
        a ``files`` list of {path, sha256} entries sorted by path.
    """
    # Build entries in plan order.  The plan is deterministic:
    # corpus.json -> system.json entries (sorted by brand) -> asset files
    # (sorted by rel path).  Deterministic order = idempotent manifest.
    files = [
        {"path": item.rel, "sha256": sha256_hex(item.dst)}
        for item in plan
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "asset_count": corpus_meta.get("asset_count", 0),
        "file_count": len(files),
        "files": files,
    }


def verify_drl_untouched(
    drl_root: pathlib.Path,
    plan: list[SyncFile],
) -> None:
    """Assert that no destination path in the plan falls under drl_root.

    Belt-and-suspenders guard that makes the DRL-read-only invariant
    explicit and auditable. Raises RuntimeError on the first violation;
    in practice this should never trigger because dst is always under
    _vendored/drl_corpus/.

    Args:
        drl_root: The DRL root that must never be written.
        plan: The sync plan whose dst paths are checked.

    Raises:
        RuntimeError: If any dst path is inside drl_root.
    """
    drl_resolved = drl_root.resolve()
    for item in plan:
        dst_resolved = item.dst.resolve()
        try:
            dst_resolved.relative_to(drl_resolved)
        except ValueError:
            continue  # dst is not under drl_root - expected
        raise RuntimeError(
            f"SAFETY VIOLATION: destination path {dst_resolved} is inside the "
            f"DRL root {drl_resolved}. The sync script must never write into the "
            "DRL. This is a bug; the plan construction logic must be fixed."
        )


# ---------------------------------------------------------------------------
# Execution helpers (have I/O side-effects; tested via integration)
# ---------------------------------------------------------------------------


def execute_sync(
    plan: list[SyncFile],
    *,
    dry_run: bool = False,
) -> dict:
    """Copy each SyncFile from src to dst, skipping identical files.

    Idempotency: if dst already exists and is byte-identical to the LF-normalised
    source bytes, the file is counted as skipped (no write). This keeps git diff
    clean on re-runs and preserves the original mtime.

    Args:
        plan: SyncFile pairs from build_corpus_plan.
        dry_run: When True, log actions but write nothing.

    Returns:
        Summary dict with keys ``copied``, ``skipped``, ``total``.
    """
    copied = 0
    skipped = 0
    for item in plan:
        src_bytes = read_lf_bytes(item.src)
        if item.dst.exists() and item.dst.read_bytes() == src_bytes:
            _log.debug("skip (identical): %s", item.rel)
            skipped += 1
            continue
        if dry_run:
            _log.info("[dry-run] would copy: %s", item.rel)
            copied += 1
            continue
        item.dst.parent.mkdir(parents=True, exist_ok=True)
        item.dst.write_bytes(src_bytes)
        shutil.copystat(item.src, item.dst)
        _log.debug("copied: %s", item.rel)
        copied += 1
    return {"copied": copied, "skipped": skipped, "total": len(plan)}


def write_version(
    vendored_root: pathlib.Path,
    *,
    source: str,
    vendored_at: str,
    corpus_generated: str,
) -> None:
    """Write the VERSION provenance file (same convention as _vendored/drl/VERSION).

    VERSION carries the run timestamp and corpus generation date.  It is NOT
    part of manifest.json so the manifest remains timestamp-free and idempotent.

    Args:
        vendored_root: The _vendored/drl_corpus/ directory.
        source: Human-readable description of the DRL source.
        vendored_at: ISO-8601 UTC timestamp of this sync run.
        corpus_generated: The ``generated`` field from corpus.json.
    """
    content = (
        f"SOURCE: {source}\n"
        f"VENDORED_AT: {vendored_at}\n"
        f"CORPUS_GENERATED: {corpus_generated}\n"
    )
    version_path = vendored_root / "VERSION"
    version_path.write_bytes(content.encode("utf-8"))
    _log.info("wrote VERSION (vendored_at=%s)", vendored_at)


def write_manifest(vendored_root: pathlib.Path, manifest: dict) -> None:
    """Write manifest.json to vendored_root.

    Keys are sorted so serialisation is deterministic regardless of dict
    insertion order.

    Args:
        vendored_root: The _vendored/drl_corpus/ directory.
        manifest: Dict from build_manifest.
    """
    manifest_path = vendored_root / "manifest.json"
    content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    manifest_path.write_bytes(content.encode("utf-8"))
    _log.info("wrote manifest.json (%d files, %d assets)", manifest["file_count"], manifest["asset_count"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument(
        "--drl-root",
        type=pathlib.Path,
        default=str(DEFAULT_DRL_ROOT),
        help=f"Path to the DRL root (default: {DEFAULT_DRL_ROOT})",
    )
    args = parser.parse_args(argv)

    drl_root: pathlib.Path = pathlib.Path(args.drl_root).resolve()
    vendored_root: pathlib.Path = _DEFAULT_VENDORED

    if not drl_root.is_dir():
        _log.error("DRL root not found at %s. Pass --drl-root.", drl_root)
        return 1

    _log.info("DRL root:       %s", drl_root)
    _log.info("Vendored root:  %s", vendored_root)

    try:
        plan = build_corpus_plan(drl_root, vendored_root)
    except (FileNotFoundError, ValueError) as exc:
        _log.error("%s", exc)
        return 1

    # Safety: assert no dst path writes into the DRL.
    try:
        verify_drl_untouched(drl_root, plan)
    except RuntimeError as exc:
        _log.error("%s", exc)
        return 1

    _log.info("Plan: %d files", len(plan))

    summary = execute_sync(plan, dry_run=args.dry_run)
    _log.info(
        "%d copied, %d skipped, %d total%s",
        summary["copied"],
        summary["skipped"],
        summary["total"],
        " [dry-run]" if args.dry_run else "",
    )

    if args.dry_run:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "dry_run": True, **summary}))
        return 0

    # Write VERSION (has timestamp - not in manifest).
    vendored_at = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    corpus_meta: dict = json.loads((drl_root / "corpus.json").read_text(encoding="utf-8"))
    write_version(
        vendored_root,
        source=str(drl_root),
        vendored_at=vendored_at,
        corpus_generated=corpus_meta.get("generated", ""),
    )

    # Write manifest (timestamp-free, idempotent).
    manifest = build_manifest(plan, corpus_meta)
    write_manifest(vendored_root, manifest)

    _log.info("Done. Commit _vendored/drl_corpus/ to update CI.")
    print(json.dumps({"schema_version": SCHEMA_VERSION, **summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
