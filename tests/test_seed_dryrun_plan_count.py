"""One-off plan-count probe for the DRL bulk-seed dry-run.

# one-off: emits the real corpus plan count to stdout so the operator can
# eyeball it before queueing --apply. Not part of the standard test gate.

Run with:
    pytest tests/test_seed_dryrun_plan_count.py -s
"""
from __future__ import annotations

from pathlib import Path

import pytest

# ``scripts.seed_from_drl`` resolves via pytest's ``pythonpath = ["."]`` in
# pyproject.toml; ``transformer`` is vendored at ``code/api/transformer/``.

from scripts.seed_from_drl import iter_assets, load_corpus, plan_only


def test_emit_real_corpus_dryrun_plan() -> None:
    """Emit the dry-run plan for the real DRL corpus; never fails."""
    drl_root = Path(__file__).resolve().parents[4] / "Design Reference Library"
    if not (drl_root / "corpus.json").exists():
        pytest.skip("Real DRL corpus not present")
    corpus = load_corpus(drl_root)
    pairs = list(iter_assets(corpus))
    print(f"\nDRL corpus: {len(pairs)} (system, asset) pairs available")
    plan = plan_only(iter(pairs), drl_root, None)
    print(f"PLAN: {len(plan)} rows would be inserted (assets with tokens.css on disk)")
    skipped = len(pairs) - len(plan)
    print(f"SKIPPED: {skipped} assets without tokens.css")
    print("\nFirst 5 planned rows:")
    for row in plan[:5]:
        print(
            f"  source_id={row['source_id']:<60} "
            f"op={row['operation']:<6} tokens={row['tokens_count']:<3} "
            f"zip={row['zip_bytes']}B"
        )
    total_zip = sum(row["zip_bytes"] for row in plan)
    print(f"\nTotal zip bytes: {total_zip:,}")
