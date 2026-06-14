"""Phase 9 corpus coverage guard - structural coverage floor and parametrized guard.

Phase 9 of the Library v5 TDD plan turns the vendored corpus from a fixture
that one test happens to read into an active CI regression guard. This file
implements two of the three guards:

1. Coverage floor (9.1 RED -> 9.2 GREEN): ``test_structural_ci_coverage_floor``
   asserts that the parametrized structural guard covers every brand with
   vendored specs. RED before the parametrized guard existed (coverage=0 < 8);
   GREEN after Phase 9.2 added the guard (coverage=8 == floor=8).

2. Parametrized structural guard (9.2 GREEN): ``test_spec_structural_guard``
   has one pytest case per vendored (brand, category) spec. Each case asserts
   that the spec's font assertion is schema-valid, resolvable, and evaluates
   correctly (positive HTML True / negative HTML False). Covers all 8 corpus
   brands across all 20 specs. Moves CI structural coverage from 1 brand
   (linear-only) to all 8 brands.

Design constraint (Phase 8 / D-5.1): every test resolves specs from CORPUS_ROOT
(the vendored in-repo copy) so the guard RUNS on a standalone CI checkout without
the workspace ``_verification/`` tree. If ``SPECS_DIR`` is absent the floor test
self-skips and the parametrize list is empty (zero cases generated). On a normal
checkout the corpus is vendored (Phase 8) and the guard RUNS.

Standalone re-proof: after Phase 9.2, ``git archive HEAD | tar -x -C <tmp>``
then ``pytest tests/render/`` from the extracted tree must show 20 parametrize
cases PASSED (not skipped).

See:
  _HANDOFF_2026-06-13_library-v5-phase9-corpus-coverage-guard.md
  D-5.1 (2026-06-13): structural gate is PRIMARY; SSIM is informational.
Schema: phase9_spec_coverage_v1
"""
from __future__ import annotations

import json
import pathlib
from typing import List, Tuple

import pytest

from .conftest import CORPUS_ROOT
from .test_visual_fidelity_gate import (
    evaluate_font_family_against_live_html,
    expected_token_from_assertion,
    font_family_assertion_from_spec,
)

SPECS_DIR: pathlib.Path = CORPUS_ROOT / "reference_captures" / "specs"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _brands_in_corpus(specs_dir: pathlib.Path) -> frozenset[str]:
    """Return the set of brand names for which specs exist in the corpus.

    Pure function (no os.environ / __file__ in the core logic) so it is
    unit-testable with an injected path. Returns an empty frozenset when
    specs_dir is absent rather than raising, matching the load_tolerance /
    load_manifest self-skip discipline used throughout this test tree.

    Brand names are derived by splitting each spec filename on the first
    underscore: ``aeon_alphabet.json`` -> brand ``aeon``.
    """
    if not specs_dir.is_dir():
        return frozenset()
    brands: set[str] = set()
    for spec_file in specs_dir.glob("*.json"):
        parts = spec_file.stem.split("_", 1)
        if parts:
            brands.add(parts[0])
    return frozenset(brands)


def _discover_specs(specs_dir: pathlib.Path) -> List[Tuple[str, str]]:
    """Return sorted (brand, category) pairs for all vendored spec files.

    Pure function (no os.environ / __file__ in the core logic). Returns an
    empty list when specs_dir is absent - the parametrized guard then has zero
    cases, matching the self-skip discipline of load_tolerance / load_manifest.

    Derives (brand, category) by splitting the stem on the first underscore:
      ``aeon_about-team.json`` -> (``aeon``, ``about-team``)
    """
    if not specs_dir.is_dir():
        return []
    pairs: List[Tuple[str, str]] = []
    for spec_file in sorted(specs_dir.glob("*.json")):
        parts = spec_file.stem.split("_", 1)
        if len(parts) == 2:
            pairs.append((parts[0], parts[1]))
    return pairs


# ---------------------------------------------------------------------------
# Module-level corpus scan (runs at import; no network)
# ---------------------------------------------------------------------------

# Derived from the corpus on disk so adding a brand's specs auto-raises the bar.
# Resolved at import time against the vendored in-repo CORPUS_ROOT (Phase 8).
STRUCTURAL_COVERAGE_FLOOR: int = len(_brands_in_corpus(SPECS_DIR))

# (brand, category) pairs discovered from the vendored corpus.
# Drives the parametrized guard; also used to compute _GUARD_COVERED_BRANDS.
# Phase 9.1: was [] (placeholder). Phase 9.2: populated from corpus.
_SPEC_PARAMS: List[Tuple[str, str]] = _discover_specs(SPECS_DIR)

# Brands currently covered by the parametrized structural guard.
# Phase 9.1: frozenset() -> test_structural_ci_coverage_floor RED (0 >= 8 failed).
# Phase 9.2: all 8 corpus brands -> floor test GREEN (8 >= 8 passes).
_GUARD_COVERED_BRANDS: frozenset[str] = frozenset(b for b, _ in _SPEC_PARAMS)


# ---------------------------------------------------------------------------
# Unit tests for _brands_in_corpus (pure; no network)
# ---------------------------------------------------------------------------


def test_brands_in_corpus_empty_dir(tmp_path: pathlib.Path) -> None:
    """An empty directory yields an empty brand set (no exception)."""
    assert _brands_in_corpus(tmp_path) == frozenset()


def test_brands_in_corpus_absent_dir(tmp_path: pathlib.Path) -> None:
    """A non-existent path yields an empty brand set (no exception)."""
    absent = tmp_path / "does_not_exist"
    assert _brands_in_corpus(absent) == frozenset()


def test_brands_in_corpus_extracts_brand_prefix(tmp_path: pathlib.Path) -> None:
    """Brand names are derived from the first segment before the first underscore."""
    (tmp_path / "aeon_alphabet.json").write_text("{}", encoding="utf-8")
    (tmp_path / "aeon_buttons.json").write_text("{}", encoding="utf-8")
    (tmp_path / "stripe_alphabet.json").write_text("{}", encoding="utf-8")
    result = _brands_in_corpus(tmp_path)
    assert result == frozenset({"aeon", "stripe"})


def test_brands_in_corpus_ignores_non_json(tmp_path: pathlib.Path) -> None:
    """Non-JSON files (e.g. README.md, .gitignore) are ignored."""
    (tmp_path / "aeon_alphabet.json").write_text("{}", encoding="utf-8")
    (tmp_path / "README.md").write_text("text", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.png\n", encoding="utf-8")
    result = _brands_in_corpus(tmp_path)
    assert result == frozenset({"aeon"})


def test_discover_specs_sorted_and_paired(tmp_path: pathlib.Path) -> None:
    """_discover_specs returns sorted (brand, category) pairs from spec filenames."""
    (tmp_path / "stripe_alphabet.json").write_text("{}", encoding="utf-8")
    (tmp_path / "aeon_buttons.json").write_text("{}", encoding="utf-8")
    (tmp_path / "aeon_alphabet.json").write_text("{}", encoding="utf-8")
    result = _discover_specs(tmp_path)
    assert result == [("aeon", "alphabet"), ("aeon", "buttons"), ("stripe", "alphabet")]


def test_discover_specs_absent_dir(tmp_path: pathlib.Path) -> None:
    """An absent directory yields an empty list (no exception)."""
    assert _discover_specs(tmp_path / "missing") == []


def test_discover_specs_skips_files_without_underscore(tmp_path: pathlib.Path) -> None:
    """Files with no underscore in the stem are skipped (no pair derivable)."""
    (tmp_path / "readme.json").write_text("{}", encoding="utf-8")
    (tmp_path / "aeon_alphabet.json").write_text("{}", encoding="utf-8")
    result = _discover_specs(tmp_path)
    assert result == [("aeon", "alphabet")]


# ---------------------------------------------------------------------------
# Unit tests for expected_token_from_assertion (pure; no network)
# Phase 9.2: helper added to test_visual_fidelity_gate.py; tests here.
# ---------------------------------------------------------------------------


def test_expected_token_text_content_kind() -> None:
    """text_content kind: returns the expected_text value as-is (not lowercased)."""
    assertion: dict = {
        "id": "x-disclosure-aside-names-brand-font",
        "kind": "text_content",
        "expected_text": "PP Right Grotesk Wide",
    }
    assert expected_token_from_assertion(assertion) == "PP Right Grotesk Wide"


def test_expected_token_evaluate_double_quote() -> None:
    """evaluate kind: extracts the token from .includes("...") double-quoted form."""
    assertion: dict = {
        "id": "x-font-family-uses-free-alt",
        "evaluate": (
            "(() => { const fam = ''; "
            'return fam.toLowerCase().includes("inter"); })()'
        ),
    }
    assert expected_token_from_assertion(assertion) == "inter"


def test_expected_token_evaluate_single_quote() -> None:
    """evaluate kind: extracts the token from .includes('...') single-quoted form."""
    assertion: dict = {
        "id": "x-font-family-uses-free-alt",
        "evaluate": (
            "(() => { const fam = ''; "
            "return fam.toLowerCase().includes('plus jakarta sans'); })()"
        ),
    }
    assert expected_token_from_assertion(assertion) == "plus jakarta sans"


def test_expected_token_malformed_evaluate_returns_none() -> None:
    """evaluate kind with no .includes( marker returns None (not an exception)."""
    assertion: dict = {
        "id": "x-font-check",
        "evaluate": "(() => { return true; })()",
    }
    assert expected_token_from_assertion(assertion) is None


def test_expected_token_unknown_kind_returns_none() -> None:
    """An unrecognized kind (not text_content, not evaluate) returns None."""
    assertion: dict = {
        "id": "x-font-check",
        "kind": "css_value",
        "expected_value": "Inter",
    }
    assert expected_token_from_assertion(assertion) is None


def test_expected_token_text_content_missing_expected_text() -> None:
    """text_content with no expected_text key returns None (not KeyError)."""
    assertion: dict = {"id": "x-font-check", "kind": "text_content"}
    assert expected_token_from_assertion(assertion) is None


def test_evaluate_font_family_uses_expected_token_helper() -> None:
    """evaluate_font_family_against_live_html delegates to expected_token_from_assertion.

    Regression pin for the Phase 9.2 refactor: the evaluator must still produce
    correct True/False for both assertion kinds after the DRY refactor routes
    all token extraction through expected_token_from_assertion. The existing
    test_font_family_assertion_extracts_includes_token in test_visual_fidelity_gate.py
    stays green alongside this pin.
    """
    tc: dict = {
        "id": "x-disclosure-aside-names-brand-font",
        "kind": "text_content",
        "expected_text": "Inter",
    }
    assert evaluate_font_family_against_live_html(
        tc, "<aside>Rendered with Inter font.</aside>"
    )
    assert not evaluate_font_family_against_live_html(
        tc, "<aside>Rendered with Helvetica font.</aside>"
    )

    ev: dict = {
        "id": "x-font-family-uses-free-alt",
        "evaluate": (
            "(() => { const fam = getComputedStyle(el).fontFamily; "
            "return fam.toLowerCase().includes('plus jakarta sans'); })()"
        ),
    }
    assert evaluate_font_family_against_live_html(
        ev,
        "<style>body { font-family: 'Plus Jakarta Sans', sans-serif; }</style>",
    )
    assert not evaluate_font_family_against_live_html(
        ev,
        "<style>body { font-family: Times New Roman, serif; }</style>",
    )


# ---------------------------------------------------------------------------
# Coverage floor (Phase 9.1 RED -> Phase 9.2 GREEN)
# ---------------------------------------------------------------------------


def test_structural_ci_coverage_floor() -> None:
    """Structural CI guard must cover every brand that has vendored specs.

    Phase 9.1 RED: ``_GUARD_COVERED_BRANDS`` was empty; corpus had 8 brands.
    0 >= 8 failed.

    Phase 9.2 GREEN: ``_GUARD_COVERED_BRANDS`` is derived from ``_SPEC_PARAMS``
    which is discovered from the corpus at module load time. 8 >= 8 passes.

    The floor is derived from the corpus on disk (``_brands_in_corpus``) so
    that adding a brand's specs later automatically raises the bar without a
    manual edit to this test. This prevents the Phase 8 blind spot from
    recurring: 18 of 20 vendored specs were exercised by NO CI structural test.

    Self-skips when ``SPECS_DIR`` is absent (only possible if the corpus was
    not vendored - should not happen since Phase 8 delivered the in-repo mirror).

    See: _HANDOFF_2026-06-13_library-v5-phase9-corpus-coverage-guard.md
    """
    if not SPECS_DIR.exists():
        pytest.skip(
            f"Structural specs dir not found at {SPECS_DIR}; "
            "corpus may not be vendored - run scripts/sync_fidelity_corpus.py "
            "from the workspace root (Phase 8 deliverable)."
        )
    floor = STRUCTURAL_COVERAGE_FLOOR
    covered = _GUARD_COVERED_BRANDS
    missing = sorted(_brands_in_corpus(SPECS_DIR) - covered)
    assert len(covered) >= floor, (
        f"Structural CI guard covers {len(covered)} brand(s) "
        f"({sorted(covered) or 'none'}); "
        f"corpus has {floor} brand(s) with vendored specs. "
        "Add a parametrized structural guard over every vendored spec "
        "(Phase 9.2 per "
        "_HANDOFF_2026-06-13_library-v5-phase9-corpus-coverage-guard.md). "
        f"Brands missing coverage: {missing}"
    )


# ---------------------------------------------------------------------------
# Parametrized structural guard (Phase 9.2 GREEN)
# One case per vendored (brand, category) spec. Covers all 8 corpus brands.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "brand,category",
    _SPEC_PARAMS,
    ids=[f"{b}_{c}" for b, c in _SPEC_PARAMS],
)
def test_spec_structural_guard(brand: str, category: str) -> None:
    """Structural guard: each vendored spec's font assertion is valid and evaluates correctly.

    For every (brand, category) spec in the vendored corpus, asserts:
      1. The spec file is schema-valid: schema_version == "fidelity_spec_v2"
         and has a non-empty "assertions" list.
      2. font_family_assertion_from_spec returns a non-None assertion (the spec
         has at least one assertion whose id contains "font" or "family").
      3. expected_token_from_assertion returns a non-empty token string.
      4. evaluate_font_family_against_live_html(assertion, positive_html) is True,
         where positive_html embeds the token verbatim.
      5. evaluate_font_family_against_live_html(assertion, negative_html) is False,
         where negative_html deliberately omits the token.

    Pure-data: no network, no Playwright, no PNGs. Runs on a standalone CI
    checkout via CORPUS_ROOT (the vendored in-repo copy, Phase 8 deliverable).
    The parametrize ids ({brand}_{category}) name the exact spec in failures.

    Self-skips when SPECS_DIR is absent (corpus not vendored).
    """
    if not SPECS_DIR.exists():
        pytest.skip(
            f"SPECS_DIR absent at {SPECS_DIR}; structural guard cannot run. "
            "Vendor the corpus via scripts/sync_fidelity_corpus.py (Phase 8)."
        )

    spec_path = SPECS_DIR / f"{brand}_{category}.json"
    assert spec_path.exists(), (
        f"Spec file missing: {spec_path}. "
        "This (brand, category) pair was in _SPEC_PARAMS but the file is not on disk."
    )

    # 1. Schema validity
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    assert raw.get("schema_version") == "fidelity_spec_v2", (
        f"{brand}_{category}: expected schema_version='fidelity_spec_v2', "
        f"got {raw.get('schema_version')!r}"
    )
    assert raw.get("assertions"), (
        f"{brand}_{category}: 'assertions' list is absent or empty"
    )

    # 2. font_family_assertion_from_spec resolves
    assertion = font_family_assertion_from_spec(SPECS_DIR, brand, category)
    assert assertion is not None, (
        f"{brand}_{category}: font_family_assertion_from_spec returned None. "
        "The spec must have at least one assertion whose id contains 'font' or 'family'."
    )

    # 3. expected_token_from_assertion returns a non-empty string
    token = expected_token_from_assertion(assertion)
    assert token, (
        f"{brand}_{category}: expected_token_from_assertion returned {token!r}. "
        f"Assertion: {assertion}. "
        "The token must be non-empty for the evaluator to produce a meaningful check."
    )

    # 4. Positive HTML (contains the token verbatim) -> True
    positive_html = f"<html><body><span>{token}</span></body></html>"
    assert evaluate_font_family_against_live_html(assertion, positive_html) is True, (
        f"{brand}_{category}: assertion fails against positive HTML "
        f"containing token={token!r}. Assertion: {assertion}"
    )

    # 5. Negative HTML (omits the token) -> False
    negative_html = "<html><body>no font disclosure here</body></html>"
    assert evaluate_font_family_against_live_html(assertion, negative_html) is False, (
        f"{brand}_{category}: assertion unexpectedly passes against negative HTML "
        f"(which should not contain token={token!r}). "
        f"Assertion: {assertion}. Negative HTML: {negative_html!r}"
    )
