"""Command-line entry point: ``python -m resemblio_shadcn <manifest.json>``.

Reads a Resemblio DTCG manifest from a file (or stdin), converts to a
shadcn theme, and writes the rendered ``globals.css`` + ``tailwind.config.js``
fragment to stdout, or to ``--out-dir`` if supplied.

Throwaway-friendly: no logging framework, no retries, no network. This is
an interactive utility; the library API (``dtcg_to_shadcn``) is the path
for programmatic use.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from resemblio_shadcn.converter import (
    dtcg_to_shadcn,
    render_globals_css,
    render_tailwind_config,
)


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser. Factored out for testability."""
    parser = argparse.ArgumentParser(
        prog="resemblio-shadcn",
        description="Convert a Resemblio DTCG manifest into a shadcn/ui theme.",
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        default="-",
        help="Path to a DTCG manifest JSON file, or '-' for stdin (default: stdin).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Directory to write globals.css and tailwind.config.js into. "
             "If omitted, both files are concatenated to stdout with separator headers.",
    )
    parser.add_argument(
        "--source-url",
        default=None,
        help="Optional source URL to stamp into the ShadcnTheme metadata.",
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
    theme = dtcg_to_shadcn(manifest, source_url=args.source_url)
    css = render_globals_css(theme)
    config = render_tailwind_config(theme)

    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "globals.css").write_text(css, encoding="utf-8")
        (args.out_dir / "tailwind.config.js").write_text(config, encoding="utf-8")
        sys.stdout.write(f"Wrote globals.css and tailwind.config.js to {args.out_dir}\n")
    else:
        sys.stdout.write("/* === globals.css === */\n")
        sys.stdout.write(css)
        sys.stdout.write("\n/* === tailwind.config.js === */\n")
        sys.stdout.write(config)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
