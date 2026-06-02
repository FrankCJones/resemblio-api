"""R3 extraction-fidelity tests.

These tests exercise the ground-truth fixture set under
``tests/fixtures/extraction/``. Each fixture pairs a ``source.html`` (what
the extractor would see) with a ``ground_truth.json`` (the human-authored
DTCG TokenSet a correct extractor would produce, plus rubric expectations).

Two layers of validation run on every fixture:

1. ``test_fixture_inventory_well_formed`` — the fixture file itself is
   shape-correct (schema_version, _provenance block, tokens dict). Catches
   typos and authoring drift at commit time.
2. ``test_fidelity_<fixture_id>`` — the GROUND-TRUTH TokenSet, when scored
   through ``compute_quality_score`` + ``apply_heuristic_penalties``,
   matches its declared ``rubric_expectations`` (should_flag, expected
   penalty names). This is the CALIBRATION check on the rubric itself.

The "feed source.html through the real extractor and assert tolerance"
test depends on Anthropic API access + Playwright and lives outside this
CI-safe layer (opt-in via ``RESEMBLIO_RUN_REAL_EXTRACTOR=1``; deferred to
the R3.1 surgery mission per
``projects/Resemblio/02-prd/2026-06-02-r3-phase-a-probe.md``).

Source mission: ``projects/OptSus Team/missions/resemblio-r3-extraction-fidelity-v1.md``.
Source finding: ``projects/Resemblio/02-prd/2026-05-31-extraction-fidelity-finding-susann.md``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

import pytest

from app.quality_heuristics import apply_heuristic_penalties
from app.quality_scoring import compute_quality_score
from app.scoring_weights import DEFAULT_THRESHOLD_V1_1_X

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "extraction"
FIXTURE_SCHEMA_VERSION = "extraction_fidelity_fixture_v1"


class FixtureProvenance(TypedDict):
    """Required `_provenance` block on every fixture."""

    author: str
    date_iso: str
    source_concept: str
    failure_mode_caught: str


class RubricExpectations(TypedDict, total=False):
    """Per-fixture rubric expectations.

    Required keys: ``should_flag_low_quality`` (bool), ``expected_penalties``
    (list[str]). Optional keys: ``min_penalties_fired`` (int),
    ``max_penalized_score`` (float), ``notes`` (str).
    """

    should_flag_low_quality: bool
    expected_penalties: list[str]
    min_penalties_fired: int
    max_penalized_score: float
    notes: str


class FixtureFile(TypedDict):
    """The on-disk shape of `ground_truth.json` for every fixture."""

    schema_version: str
    _provenance: FixtureProvenance
    tokens: dict[str, str]
    rubric_expectations: RubricExpectations


def _discover_fixture_ids() -> list[str]:
    """Return sorted directory names for every fixture under FIXTURES_ROOT.

    Each fixture is a subdirectory with `source.html` + `ground_truth.json`.
    """
    if not FIXTURES_ROOT.exists():
        return []
    out: list[str] = []
    for child in sorted(FIXTURES_ROOT.iterdir()):
        if not child.is_dir():
            continue
        if (child / "source.html").exists() and (child / "ground_truth.json").exists():
            out.append(child.name)
    return out


def _load_fixture(fixture_id: str) -> FixtureFile:
    """Load the ground-truth JSON for one fixture id."""
    path = FIXTURES_ROOT / fixture_id / "ground_truth.json"
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


FIXTURE_IDS = _discover_fixture_ids()


def test_fixture_root_exists_and_has_entries() -> None:
    """Sanity: the fixtures directory exists and carries at least 5 entries."""
    assert FIXTURES_ROOT.exists(), f"fixtures root missing: {FIXTURES_ROOT}"
    assert len(FIXTURE_IDS) >= 5, (
        f"expected at least 5 fixtures, found {len(FIXTURE_IDS)}: {FIXTURE_IDS}"
    )


@pytest.mark.parametrize("fixture_id", FIXTURE_IDS)
def test_fixture_inventory_well_formed(fixture_id: str) -> None:
    """Every fixture's `ground_truth.json` carries the required shape.

    Catches authoring drift: missing schema_version, missing _provenance
    fields, missing tokens dict, missing rubric_expectations.
    """
    data: dict[str, Any] = _load_fixture(fixture_id)  # type: ignore[assignment]
    assert data.get("schema_version") == FIXTURE_SCHEMA_VERSION, (
        f"{fixture_id}: bad schema_version: {data.get('schema_version')!r}"
    )
    prov = data.get("_provenance") or {}
    for required in ("author", "date_iso", "source_concept", "failure_mode_caught"):
        assert prov.get(required), f"{fixture_id}: _provenance.{required} missing"
    tokens = data.get("tokens") or {}
    assert isinstance(tokens, dict) and tokens, f"{fixture_id}: tokens dict missing or empty"
    expectations = data.get("rubric_expectations") or {}
    assert "should_flag_low_quality" in expectations, (
        f"{fixture_id}: rubric_expectations.should_flag_low_quality missing"
    )
    assert "expected_penalties" in expectations, (
        f"{fixture_id}: rubric_expectations.expected_penalties missing"
    )


@pytest.mark.parametrize("fixture_id", FIXTURE_IDS)
def test_fidelity_rubric_matches_expectations(fixture_id: str) -> None:
    """Scoring the fixture's ground-truth tokens matches its rubric expectations.

    This is the CALIBRATION assertion: when a correct extractor produces
    these tokens (or when the LLM-defaulted floor produces them, in the
    case of fixture 010), the rubric's verdict matches the human-authored
    expectation. Failures here are EITHER a rubric miscalibration OR a
    fixture authoring error — both are bugs we want to catch.
    """
    data = _load_fixture(fixture_id)
    tokens = data["tokens"]
    expectations: dict[str, Any] = data["rubric_expectations"]  # type: ignore[assignment]

    base = compute_quality_score(tokens)
    out = apply_heuristic_penalties(tokens, base)

    expected_penalties = set(expectations.get("expected_penalties", []))
    actual_penalties = set(out.penalties_applied)
    assert actual_penalties == expected_penalties, (
        f"{fixture_id}: penalty mismatch\n"
        f"  expected: {sorted(expected_penalties)}\n"
        f"  actual:   {sorted(actual_penalties)}\n"
        f"  diagnostic: {out.diagnostic}"
    )

    should_flag = bool(expectations["should_flag_low_quality"])
    actually_flagged = out.penalized_score < DEFAULT_THRESHOLD_V1_1_X
    assert actually_flagged == should_flag, (
        f"{fixture_id}: flag mismatch\n"
        f"  expected should_flag_low_quality={should_flag}\n"
        f"  actual penalized_score={out.penalized_score} threshold={DEFAULT_THRESHOLD_V1_1_X}\n"
        f"  base={base.composite_score} diagnostic={out.diagnostic}"
    )

    if "min_penalties_fired" in expectations:
        assert len(out.penalties_applied) >= int(expectations["min_penalties_fired"]), (
            f"{fixture_id}: expected >= {expectations['min_penalties_fired']} penalties, "
            f"got {len(out.penalties_applied)} ({out.penalties_applied})"
        )

    if "max_penalized_score" in expectations:
        assert out.penalized_score <= float(expectations["max_penalized_score"]), (
            f"{fixture_id}: penalized_score {out.penalized_score} exceeds "
            f"max {expectations['max_penalized_score']}"
        )


def test_low_quality_baseline_is_flagged() -> None:
    """Fixture 010 (default HTML baseline) MUST score below 0.5 with >=2 penalties.

    This is the rubric's calibration anchor: the worst-case
    LLM-defaulted-everything output. If this fixture stops failing the
    rubric, the rubric has been weakened past the point it can catch the
    Susann-class pathology.
    """
    data = _load_fixture("010_default_html_baseline")
    tokens = data["tokens"]
    base = compute_quality_score(tokens)
    out = apply_heuristic_penalties(tokens, base)
    assert len(out.penalties_applied) >= 2, (
        f"default-baseline fixture must fire >=2 penalties; "
        f"got {out.penalties_applied} (diagnostic={out.diagnostic})"
    )
    assert out.penalized_score < 0.5, (
        f"default-baseline fixture must score < 0.5 after penalties; "
        f"base={base.composite_score} penalized={out.penalized_score}"
    )
    assert out.penalized_score < DEFAULT_THRESHOLD_V1_1_X, (
        f"default-baseline must also fall below the refund threshold "
        f"{DEFAULT_THRESHOLD_V1_1_X}; got {out.penalized_score}"
    )


def test_susann_headlights_ground_truth_passes_rubric() -> None:
    """Fixture 001 (Susann ground truth) MUST score ABOVE the threshold.

    The Susann concept's CORRECT extraction (ink/bone/sun + Anton+Inter)
    is a distinctive design system. The rubric must NOT penalize this
    output. This pairs with the regression test in
    ``test_quality_heuristics.py`` which validates that the WRONG
    (extracted) Susann tokens DO trip the rubric.
    """
    data = _load_fixture("001_susann_headlights")
    tokens = data["tokens"]
    base = compute_quality_score(tokens)
    out = apply_heuristic_penalties(tokens, base)
    assert out.penalties_applied == (), (
        f"Susann ground truth must trigger no penalties; "
        f"got {out.penalties_applied} (diagnostic={out.diagnostic})"
    )
    assert out.penalized_score >= DEFAULT_THRESHOLD_V1_1_X, (
        f"Susann ground truth must score >= {DEFAULT_THRESHOLD_V1_1_X}; "
        f"got {out.penalized_score} (base={base.composite_score})"
    )
