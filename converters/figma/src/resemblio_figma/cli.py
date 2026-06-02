"""Command-line entry point: ``python -m resemblio_figma <manifest.json>``.

Reads a Resemblio DTCG manifest from a file (or stdin), converts to a Figma
Variables payload, and writes it as JSON to stdout (or to ``--out`` if
supplied). The output is the Figma REST Variables import shape; pipe it to
the Figma API or save it as ``figma-variables.json``.

Throwaway-friendly: no logging framework, no retries, no network. This is
an interactive utility; the library API (``dtcg_to_figma_variables``) is
the path for programmatic use.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from resemblio_figma.converter import dtcg_to_figma_variables


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser. Factored out for testability."""
    parser = argparse.ArgumentParser(
        prog="resemblio-figma",
        description="Convert a Resemblio DTCG manifest into a Figma Variables import payload.",
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        default="-",
        help="Path to a DTCG manifest JSON file, or '-' for stdin (default: stdin).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="File to write the Figma Variables JSON payload to. "
             "If omitted, writes to stdout.",
    )
    parser.add_argument(
        "--source-url",
        default=None,
        help="Optional source URL to stamp into the payload metadata.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation for output (default: 2).",
    )
    return parser


def _read_manifest(path_arg: str) -> dict:
    """Load a manifest from path or stdin. Returns parsed JSON dict."""
    if path_arg == "-":
        return json.load(sys.stdin)
    text = Path(path_arg).read_text(encoding="utf-8")
    return json.loads(text)


def main(argv: list[str] | None = None) -> int:
    """CLI main; returns process exit code."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    manifest = _read_manifest(args.manifest)
    payload = dtcg_to_figma_variables(manifest, source_url=args.source_url)
    rendered = json.dumps(payload.model_dump(mode="json"), indent=args.indent)

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
        sys.stdout.write(f"Wrote Figma Variables payload to {args.out}\n")
    else:
        sys.stdout.write(rendered + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
