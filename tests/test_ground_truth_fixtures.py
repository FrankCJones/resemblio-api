"""Real-URL ground-truth fixture tests.

This test module is the production regression gate for the R3 Option A
extraction-fidelity work (decisions-log 2026-06-04). It discovers every
top-level fixture under ``tests/fixtures/ground_truth/`` and runs two
classes of test against each:

1. ``test_fixture_shape_valid`` — the fixture YAML itself parses and
   passes shape validation. Catches authoring drift at commit time.
2. ``test_ground_truth_snapshot`` — runs the fixture's
   ``expected_extraction_behavior`` against its
   ``extracted_payload_snapshot``. If no snapshot is present (e.g.
   live-extraction-only fixture) the test SKIPS with a clear marker.
3. ``test_ground_truth_live`` (opt-in via ``@pytest.mark.live_extraction``)
   — calls the real extractor against ``source_url`` and asserts on the
   live payload. Used for periodic regression sweeps + snapshot capture.
   Default CI skips this marker.

Source dispatch: Jim Builder dispatch 2026-06-04 (R3-downstream cycle #1).
Source decision: workspace decisions-log 2026-06-04 (R3 Option A RATIFIED).
Source PRD recommendation: PRD #2 at
``projects/Resemblio/02-prd/2026-05-31-extraction-fidelity-finding-susann.md``.

Throwaway: NO. Quality floor applies.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.ground_truth_harness import (
    FIXTURE_SCHEMA_VERSION,
    GroundTruthFixture,
    SkipFixture,
    discover_fixtures,
    load_fixture,
    resolve_payload_for_snapshot_mode,
    run_assertions,
)

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "ground_truth"
FIXTURE_PATHS = discover_fixtures(FIXTURES_ROOT)
FIXTURE_IDS = [p.stem for p in FIXTURE_PATHS]


# ---------------------------------------------------------------------------
# Inventory sanity
# ---------------------------------------------------------------------------


def test_fixtures_root_exists() -> None:
    """The ground-truth fixture root MUST exist after this dispatch lands."""
    assert FIXTURES_ROOT.exists(), f"fixtures root missing: {FIXTURES_ROOT}"


def test_fixture_inventory_has_minimum_entries() -> None:
    """Dispatch ships >= 5 fixtures as the v1 corpus.

    PRD #2 specifies 5-10 fixtures; this dispatch ships 5. Below this
    floor either fixtures were lost or the inventory regressed.
    """
    assert len(FIXTURE_PATHS) >= 5, (
        f"expected >= 5 ground-truth fixtures, found {len(FIXTURE_PATHS)}: {FIXTURE_IDS}"
    )


def test_fixture_schema_constant_holds() -> None:
    """Schema marker is the constant the harness expects.

    Sentinel test: if FIXTURE_SCHEMA_VERSION changes, this is the
    canary that catches the in-test misuse before fixtures fail.
    """
    assert FIXTURE_SCHEMA_VERSION == "resemblio_ground_truth_v2"


# ---------------------------------------------------------------------------
# Per-fixture shape validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=FIXTURE_IDS)
def test_fixture_shape_valid(path: Path) -> None:
    """Every fixture parses + passes harness shape validation."""
    fixture = load_fixture(path)
    # load_fixture would have raised on shape failure; assert the
    # post-load invariants the harness depends on downstream:
    assert fixture["schema_version"] == FIXTURE_SCHEMA_VERSION
    assert fixture["brand_slug"] == path.stem, (
        f"brand_slug {fixture['brand_slug']!r} must match filename stem {path.stem!r}"
    )


# ---------------------------------------------------------------------------
# Snapshot mode (default; CI-safe)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=FIXTURE_IDS)
def test_ground_truth_snapshot(path: Path) -> None:
    """Run expected_extraction_behavior against the fixture's snapshot.

    Skips with a clear marker if no snapshot is captured (fixture is
    live-extraction-only or pending its first live-extraction capture).
    """
    fixture: GroundTruthFixture = load_fixture(path)
    try:
        payload = resolve_payload_for_snapshot_mode(fixture)
    except SkipFixture as exc:
        pytest.skip(str(exc))
    result = run_assertions(fixture, payload)
    assert result.passed, (
        f"{result.fixture_slug}: {len(result.failures)} assertion failure(s):\n  "
        + "\n  ".join(f"[{f.kind}] {f.detail}" for f in result.failures)
    )


# ---------------------------------------------------------------------------
# Live mode (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.live_extraction
@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=FIXTURE_IDS)
def test_ground_truth_live(path: Path) -> None:
    """Call the real extractor and assert on the live payload.

    Opt-in via ``pytest -m live_extraction``. Requires Anthropic API
    access + Playwright installed; depends on outbound network. Used
    for periodic regression sweeps + when adding new fixtures (the
    captured payload becomes the next CI snapshot).

    Skipped automatically if RESEMBLIO_RUN_REAL_EXTRACTOR is unset, so
    a developer can run ``pytest -m live_extraction`` and have the
    test self-skip without crashing on missing creds.
    """
    if os.environ.get("RESEMBLIO_RUN_REAL_EXTRACTOR") != "1":
        pytest.skip(
            "live extraction disabled; set RESEMBLIO_RUN_REAL_EXTRACTOR=1 to run"
        )

    fixture: GroundTruthFixture = load_fixture(path)
    payload = _run_live_extraction(fixture["source_url"])
    result = run_assertions(fixture, payload)
    assert result.passed, (
        f"{result.fixture_slug}: {len(result.failures)} live-extraction failure(s):\n  "
        + "\n  ".join(f"[{f.kind}] {f.detail}" for f in result.failures)
        + f"\n  live payload: {payload!r}"
    )


def _run_live_extraction(source_url: str) -> dict[str, Any]:
    """Call the real extractor and shape its output for the harness.

    Isolated so the import of ``extractor.codex_extractor`` lives behind
    the opt-in flag; CI without the optional ``browser`` extra installed
    must not pay the import cost. The shape returned matches
    ``ExtractedPayloadSnapshot``.

    v2 (2026-06-04 cycle #1.5): returns the FLAT shape mirroring the
    real ``POST /v1/extractions`` response — ``{tokens: {...}, ...}`` —
    not the nested ``{color, font_family}`` split cycle #1 assumed.
    The assertion runner walks the flat dict and splits font_* keys off
    internally, so callers should keep the API shape intact here.
    """
    # Imported lazily so the snapshot-mode test path stays import-clean
    # when the browser extras are not installed.
    from extractor.codex_extractor import extract_tokens_from_url  # type: ignore[import-not-found]

    raw = extract_tokens_from_url(source_url) or {}
    # The codex extractor returns a flat TokenSet directly; wrap it in
    # the v2 envelope. If the extractor's contract changes to return the
    # full API response (with a top-level "tokens" key), prefer that.
    if isinstance(raw, dict) and "tokens" in raw and isinstance(raw["tokens"], dict):
        return {
            "tokens": raw["tokens"],
            "palette_completeness_warning": raw.get("palette_completeness_warning"),
        }
    return {
        "tokens": {k: v for k, v in raw.items() if isinstance(v, str)},
        "palette_completeness_warning": raw.get("palette_completeness_warning"),
    }
