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
from typing import TypedDict

from app.apple_system_manifest import (
    DEFAULT_PRIVATE_LEDGER_PATH,
    DEFAULT_PUBLIC_ARTIFACT_PATH,
    canonical_json_bytes,
    compile_apple_system_manifest,
    write_compiled_artifacts,
)


SCHEMA_VERSION = "resemblio_apple_compiler_run_v1"
LOGGER = logging.getLogger(__name__)


class ProductMatches(TypedDict):
    """Byte-identity state for generated public and private products."""

    public: bool
    private: bool | None


class CompilerRunSummary(TypedDict):
    """Stable machine-readable result emitted by the compiler command."""

    schema_version: str
    action: str
    artifact_sha256: str
    evidence_set_sha256: str
    record_count: int
    private_ledger_path_present: bool
    before_match: ProductMatches
    after_match: ProductMatches


def _matches(path: Path, expected: bytes) -> bool:
    """Return whether an existing artifact is byte-identical to expected."""
    return path.is_file() and path.read_bytes() == expected


def _required_products_match(matches: ProductMatches) -> bool:
    """Return whether every repository-required or present product matches.

    The public artifact is checked in and therefore always required. The
    private ledger is intentionally ignored by Git, so an absent ledger is a
    valid clean-clone state while a present ledger must remain byte-identical.
    """
    return matches.get("public") is True and matches.get("private") in (True, None)


def run(action: str) -> CompilerRunSummary:
    """Compile once, optionally write outputs, and return a stable summary."""
    result = compile_apple_system_manifest()
    public_bytes = canonical_json_bytes(result.public_manifest)
    private_bytes = canonical_json_bytes(result.private_ledger)
    private_before_present = DEFAULT_PRIVATE_LEDGER_PATH.is_file()
    before_match: ProductMatches = {
        "public": _matches(DEFAULT_PUBLIC_ARTIFACT_PATH, public_bytes),
        "private": _matches(DEFAULT_PRIVATE_LEDGER_PATH, private_bytes)
        if private_before_present else None,
    }
    if action == "write":
        write_compiled_artifacts(result)
    private_after_present = DEFAULT_PRIVATE_LEDGER_PATH.is_file()
    after_match: ProductMatches = {
        "public": _matches(DEFAULT_PUBLIC_ARTIFACT_PATH, public_bytes),
        "private": _matches(DEFAULT_PRIVATE_LEDGER_PATH, private_bytes)
        if private_after_present else None,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "artifact_sha256": result.public_manifest["artifact_sha256"],
        "evidence_set_sha256": result.public_manifest["evidence_set_sha256"],
        "record_count": result.public_manifest["record_count"],
        "private_ledger_path_present": private_after_present,
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
    if args.action == "check" and not _required_products_match(summary["after_match"]):
        LOGGER.error("checked-in public artifact or existing private ledger differs from compiler output")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
