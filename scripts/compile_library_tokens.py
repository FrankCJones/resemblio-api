"""Compile one DRL library token source into identical checked-in projections.

Run from the API repository root with positional arguments:

    python -m scripts.compile_library_tokens SOURCE OUTPUT [OUTPUT ...]

The source is read only. Each output receives the same deterministic bytes.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from app.apple_system_manifest import load_public_manifest
from app.dtcg_compiler import CompilePolicy, canonical_bytes, compile_projection

APPLE_PREDECESSOR_SHA256 = "2a083501b1247b0c9f5ce623e56f7ba8e4cf95269d28af801f679456d03e2244"
APPLE_PROJECTION_VERSION = "2026.10.3"
APPLE_FONT_STACKS = {
    "display": ("system-ui", "sans-serif"),
    "body": ("system-ui", "sans-serif"),
    "mono": ("ui-monospace", "Consolas", "monospace"),
}


def compile_apple(source: Path) -> bytes:
    """Compile the Apple source using the approved public-safe font policy."""
    css_text = source.read_bytes().decode("utf-8")
    policy = CompilePolicy(
        projection_version=APPLE_PROJECTION_VERSION,
        source_manifest_sha256=load_public_manifest()["artifact_sha256"],
        predecessor_artifact_sha256=APPLE_PREDECESSOR_SHA256,
        font_stacks=APPLE_FONT_STACKS,
    )
    return canonical_bytes(compile_projection(css_text, policy))


def main() -> int:
    """Compile the source and replace every requested output deterministically."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("outputs", nargs="+", type=Path)
    args = parser.parse_args()
    payload = compile_apple(args.source)
    for output in args.outputs:
        output.write_bytes(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
