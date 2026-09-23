"""Compile or verify the canonical Apple system manifest artifacts.

Dependencies: Python standard library plus the local ``app`` package.

Run from ``projects/Resemblio/code/api``:

    python -m scripts.compile_apple_evidence check
    python -m scripts.compile_apple_evidence write

``check`` is read-only and is the default. ``write`` persists the validated
public artifact and private ledger at their canonical repository paths.

Schema: resemblio_apple_compiler_run_v1
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from app.apple_system_manifest import (
    DEFAULT_PRIVATE_LEDGER_PATH,
    DEFAULT_PUBLIC_ARTIFACT_PATH,
    canonical_json_bytes,
    compile_apple_system_manifest,
    write_compiled_artifacts,
)


SCHEMA_VERSION = "resemblio_apple_compiler_run_v1"
LOGGER = logging.getLogger(__name__)


def _matches(path: Path, expected: bytes) -> bool:
    """Return whether an existing artifact is byte-identical to expected."""
    return path.is_file() and path.read_bytes() == expected


def run(action: str) -> dict[str, object]:
    """Compile once, optionally write outputs, and return a stable summary."""
    result = compile_apple_system_manifest()
    public_bytes = canonical_json_bytes(result.public_manifest)
    private_bytes = canonical_json_bytes(result.private_ledger)
    before_match = {
        "public": _matches(DEFAULT_PUBLIC_ARTIFACT_PATH, public_bytes),
        "private": _matches(DEFAULT_PRIVATE_LEDGER_PATH, private_bytes),
    }
    if action == "write":
        write_compiled_artifacts(result)
    after_match = {
        "public": _matches(DEFAULT_PUBLIC_ARTIFACT_PATH, public_bytes),
        "private": _matches(DEFAULT_PRIVATE_LEDGER_PATH, private_bytes),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "artifact_sha256": result.public_manifest["artifact_sha256"],
        "evidence_set_sha256": result.public_manifest["evidence_set_sha256"],
        "record_count": result.public_manifest["record_count"],
        "before_match": before_match,
        "after_match": after_match,
    }


def main(argv: list[str] | None = None) -> int:
    """Run the local compiler CLI and return a process status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "write"), nargs="?", default="check")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    summary = run(args.action)
    print(json.dumps(summary, sort_keys=True))
    if args.action == "check" and not all(summary["after_match"].values()):  # type: ignore[union-attr]
        LOGGER.error("checked-in Apple artifacts differ from deterministic compiler output")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
