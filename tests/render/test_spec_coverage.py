"""Phase 9 corpus coverage guard - structural coverage floor (Phase 9.1 RED).

Phase 9 of the Library v5 TDD plan turns the vendored corpus from a fixture
that one test happens to read into an active CI regression guard.

This file implements the coverage floor and parametrized structural guard:

Phase 9.1 RED: ``test_structural_ci_coverage_floor`` is added here. It asserts
  that ``_GUARD_COVERED_BRANDS`` covers every brand with vendored specs.
  RED because ``_GUARD_COVERED_BRANDS`` is empty and the floor is 8.

Phase 9.2 GREEN (separate commit): ``expected_token_from_assertion`` is added
  to ``test_visual_fidelity_gate``, ``_SPEC_PARAMS`` is driven from the corpus,
  ``_GUARD_COVERED_BRANDS`` is derived from ``_SPEC_PARAMS``, and the parametrized
  ``test_spec_structural_guard`` test is added. Coverage becomes 8 >= floor=8.

Design constraint (Phase 8 / D-5.1): every test resolves specs from CORPUS_ROOT
(the vendored in-repo copy) so the guard RUNS on a standalone CI checkout without
the workspace ``_verification/`` tree. If ``SPECS_DIR`` is absent the floor test
self-skips.

See:
  _HANDOFF_2026-06-13_library-v5-phase9-corpus-coverage-guard.md
  D-5.1 (2026-06-13): structural gate is PRIMARY; SSIM is informational.
Schema: phase9_spec_coverage_v1
"""
from __future__ import annotations

import pathlib
from typing import List, Tuple

import pytest

from .conftest import CORPUS_ROOT

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
    empty list when specs_dir is absent - the parametrized guard then has
    zero cases and the coverage floor test self-skips, matching the self-skip
    discipline of load_tolerance / load_manifest.

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
STRUCTURAL_COVERAGE_FLOOR: int = len(_brands_in_corpus(SPECS_DIR))

# Phase 9.1: empty placeholder. Phase 9.2 replaces with _discover_specs(SPECS_DIR).
_SPEC_PARAMS: List[Tuple[str, str]] = []

# Phase 9.1: empty -> test_structural_ci_coverage_floor is RED (0 >= 8 fails).
# Phase 9.2: derived from _SPEC_PARAMS after the corpus is discovered.
_GUARD_COVERED_BRANDS: frozenset[str] = frozenset()


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
# Coverage floor (Phase 9.1 RED -> Phase 9.2 GREEN)
# ---------------------------------------------------------------------------


def test_structural_ci_coverage_floor() -> None:
    """Structural CI guard must cover every brand that has vendored specs.

    Phase 9.1 RED: ``_GUARD_COVERED_BRANDS`` is empty; corpus has 8 brands.
    0 >= 8 fails.

    Phase 9.2 GREEN: ``_GUARD_COVERED_BRANDS`` is derived from ``_SPEC_PARAMS``
    which is discovered from the corpus at module load time.
    8 >= 8 passes.

    The floor is derived from the corpus on disk (``_brands_in_corpus``) so
    that adding a brand's specs later automatically raises the bar without a
    manual edit. This prevents the Phase 8 blind spot from recurring: 18 of
    20 vendored specs were exercised by NO CI test before Phase 9.

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
