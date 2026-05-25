"""Command-line wrapper for the Resemblio Codex extractor.

Usage:
    python -m cli --url https://posthog.com

Prints DTCG JSON to stdout on success and exits non-zero on failure.
Throwaway: no. Quality floor applies.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from codex_extractor import CodexExtractor, dtcg_payload_with_schema


def main(argv: Sequence[str] | None = None) -> int:
    """Run one URL extraction and print the schema-versioned DTCG payload."""
    parser = argparse.ArgumentParser(description="Extract DTCG design tokens from a URL.")
    parser.add_argument("--url", required=True, help="http/https URL to extract.")
    args = parser.parse_args(argv)

    tokens, error = CodexExtractor().extract(args.url)
    if error is not None:
        print(error, file=sys.stderr)
        return 1

    assert tokens is not None
    print(json.dumps(dtcg_payload_with_schema(tokens), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
