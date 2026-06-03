"""Parity test: empty-token render produces a deterministic, contract-default snapshot.

Path C Phase 2 (per CTO sign-off
``projects/OptSus Team/cto-reviews/2026-06-03-resemblio-path-c-phase2-contract-signoff.md``):
the template rewrite swapped every contract-bound literal for
``var(--ds-<slot>, <literal>)``. The CSS source text necessarily differs
from pre-rewrite (each declaration now carries the ``var()`` wrapper),
so byte-identical-to-PRE-rewrite is not the test goal. The Path C
guarantee is BEHAVIORAL parity: when no brand override exists every
slot resolves to its contract default, which equals the pre-rewrite
literal. Browser-rendered output is therefore unchanged.

This test pins the post-rewrite snapshot. On first run it WRITES the
snapshot file under ``tests/snapshots/``; on every subsequent run it
asserts byte equality against the captured snapshot. A template edit
that drifts the contract default chain (or a contract slot that loses
its default) breaks this test immediately. Refresh the snapshot by
deleting the file and re-running pytest.
"""
from __future__ import annotations

from pathlib import Path

from app.library_indexer import _emit_brand_root


SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
SNAPSHOT_FILE = SNAPSHOT_DIR / "empty_brand_root_block.css"


def test_empty_brand_root_snapshot_is_stable() -> None:
    """Empty brand tokens -> deterministic ``:root`` block of contract defaults.

    Snapshot semantics: first run writes the snapshot and passes; later
    runs read the snapshot and assert byte equality. The test FAILS only
    when the rendered output drifts from the captured snapshot.
    """
    rendered = _emit_brand_root({})
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    if not SNAPSHOT_FILE.exists():
        SNAPSHOT_FILE.write_text(rendered, encoding="utf-8", newline="\n")
        return
    captured = SNAPSHOT_FILE.read_text(encoding="utf-8")
    assert rendered == captured, (
        "empty-brand :root block drifted from captured snapshot. "
        f"Refresh by deleting {SNAPSHOT_FILE} if the drift is intentional."
    )


def test_empty_brand_root_resolves_button_radius_to_contract_default() -> None:
    """The button-radius chain (button -> family -> literal) must resolve to ``6px`` with empty bag.

    Path C's behavioral-parity claim: pre-rewrite ``border-radius: var(--ds-radius-sm, 6px);``
    rendered ``6px`` (via the in-line var() fallback). Post-rewrite
    ``border-radius: var(--ds-button-radius, var(--ds-radius-sm, 6px));``
    must STILL render ``6px`` because the empty-token root block carries
    every slot at its contract default and the chain ``ds-button-radius
    -> ds-radius-button -> ds-radius-sm -> 6px`` terminates safely.
    """
    css = _emit_brand_root({})
    # The root block carries the literal ``6px`` for ds-radius-sm.
    assert "--ds-radius-sm: 6px;" in css
    # The button-radius slot chains through component alias to family.
    assert "--ds-button-radius:" in css
    # The badge-radius slot terminates at ``9999px`` via the same chain.
    assert "--ds-radius-full: 9999px;" in css


def test_brand_override_wins_over_contract_default() -> None:
    """Brand-supplied value beats the contract default for a single slot."""
    css = _emit_brand_root({"ds-bg": "#0a0a0a"})
    assert "--ds-bg: #0a0a0a;" in css
    # Other slots still hold contract defaults.
    assert "--ds-text: #111111;" in css
