"""Phase 9.3 corpus consistency contract (9.3 RED -> 9.4 GREEN).

Pins the manifest <-> spec relationship so the corpus cannot silently rot.
Two guards:

1. ``test_every_manifest_tuple_has_a_spec`` - each (brand, category) tuple
   in the in-repo manifest.json has a matching spec file in specs/.
   GREEN at Phase 9.3 baseline (all 12 manifest tuples have specs); pinned
   so a future manifest edit that references a spec-less tuple is caught early.

2. ``test_every_spec_is_manifest_backed_or_declared_structural_only`` - each
   spec file either has a manifest tuple OR is listed in ``STRUCTURAL_ONLY_SPECS``.
   RED at Phase 9.3: 8 PNG-less specs (aeon x3, openai x2, stripe x3) are not
   yet declared in the allowlist, so they violate the contract.
   GREEN at Phase 9.4 after ``STRUCTURAL_ONLY_SPECS`` is populated.

Why the allowlist matters: before this contract, a genuinely missing spec
(e.g. a manifest tuple added by mistake) and an intentionally PNG-less spec
(e.g. aeon has structural assertions but no reference PNG) look identical.
The allowlist makes "this spec has no PNG on purpose" a declared fact, not an
accident indistinguishable from a missing capture.

Both tests resolve from CORPUS_ROOT (the vendored in-repo copy) so they RUN
on a standalone CI checkout. Self-skip when CORPUS_ROOT artifacts are absent.

See:
  _HANDOFF_2026-06-13_library-v5-phase9-corpus-coverage-guard.md
  reference_corpus/README.md (structural-only section added in Phase 9.4)
Schema: phase9_corpus_consistency_v1
"""
from __future__ import annotations

import json
import pathlib
from typing import FrozenSet, List, Optional, Tuple

import pytest

from .conftest import CORPUS_ROOT

SPECS_DIR: pathlib.Path = CORPUS_ROOT / "reference_captures" / "specs"
MANIFEST_PATH: pathlib.Path = CORPUS_ROOT / "reference_captures" / "manifest.json"


# ---------------------------------------------------------------------------
# Structural-only allowlist (Phase 9.3: empty -> RED; Phase 9.4: populated -> GREEN)
#
# Specs listed here have structural font assertions but no reference PNG was
# ever shot for them. They are intentionally PNG-less, meaning the live full-
# corpus sweep (FIDELITY_LIVE_SWEEP=1) will never produce a visual comparison
# for them. Adding a reference capture for a brand later means:
#   1. Remove the entry from this allowlist.
#   2. Add the brand tuple to fidelity_targets.yml so a capture can be shot.
#   3. Shoot the PNG and commit the manifest entry.
# The PNG itself never enters this public repo (trademark constraint; D-5.1).
# ---------------------------------------------------------------------------

#: (brand, category) tuples that intentionally have no manifest entry.
#: Phase 9.3 RED: was empty (8 orphan specs violated the consistency contract).
#: Phase 9.4 GREEN: all 8 PNG-less specs declared here with per-entry rationale.
STRUCTURAL_ONLY_SPECS: FrozenSet[Tuple[str, str]] = frozenset({
    # aeon: no reference capture was ever shot for any aeon category.
    # aeon.co deploys a Cloudflare challenge on automated requests, making
    # full-page screenshot capture unreliable. Structural assertions (font
    # family checks against Plus Jakarta Sans) are authored and pass CI.
    ("aeon", "about-team"),
    ("aeon", "alphabet"),
    ("aeon", "buttons"),
    # openai: only the alphabet category has a reference capture in the manifest.
    # openai.com is gated by Cloudflare Turnstile on the paths used for
    # about-team and buttons pages; captures return a challenge page rather
    # than real content. See also 02-prd/2026-06-07-openai-permanent-skip.md.
    # Structural font assertions (Inter) are authored and pass CI.
    ("openai", "about-team"),
    ("openai", "buttons"),
    # stripe: no reference capture was ever shot for any stripe category.
    # Stripe's marketing pages vary significantly by geography and A/B test
    # cohort; reliable full-page captures were deferred pending a stable
    # capture target. Structural assertions (Sohne -> Inter) are authored.
    ("stripe", "about-team"),
    ("stripe", "alphabet"),
    ("stripe", "buttons"),
})


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _load_manifest_tuples(manifest_path: pathlib.Path) -> Optional[List[Tuple[str, str]]]:
    """Return sorted (brand, category) tuples from the in-repo manifest.

    Returns None when the manifest is absent (self-skip signal for callers).
    Does NOT call pytest.skip itself so it stays a pure function testable in
    isolation. The load validates only that the file is valid JSON with a
    'records' list; schema-version check is left to the main gate test.
    """
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    seen: set[Tuple[str, str]] = set()
    for record in data.get("records", []):
        brand = record.get("brand")
        category = record.get("category")
        if brand and category:
            seen.add((str(brand), str(category)))
    return sorted(seen)


def _load_spec_tuples(specs_dir: pathlib.Path) -> Optional[List[Tuple[str, str]]]:
    """Return sorted (brand, category) tuples from the spec filenames.

    Returns None when specs_dir is absent (self-skip signal for callers).
    Derives (brand, category) by splitting each stem on the first underscore:
      ``aeon_about-team.json`` -> (``aeon``, ``about-team``)
    """
    if not specs_dir.is_dir():
        return None
    pairs: List[Tuple[str, str]] = []
    for spec_file in specs_dir.glob("*.json"):
        parts = spec_file.stem.split("_", 1)
        if len(parts) == 2:
            pairs.append((parts[0], parts[1]))
    return sorted(pairs)


# ---------------------------------------------------------------------------
# Unit tests for helpers (pure; no network)
# ---------------------------------------------------------------------------


def test_load_manifest_tuples_absent_returns_none(tmp_path: pathlib.Path) -> None:
    """A missing manifest file yields None (self-skip signal)."""
    assert _load_manifest_tuples(tmp_path / "manifest.json") is None


def test_load_manifest_tuples_extracts_brand_category(tmp_path: pathlib.Path) -> None:
    """Records are deduplicated by (brand, category) across viewports."""
    manifest = {
        "schema_version": "reference_capture_manifest_v1",
        "records": [
            {"brand": "aeon", "category": "alphabet", "viewport": "1440x900"},
            {"brand": "aeon", "category": "alphabet", "viewport": "375x812"},
            {"brand": "stripe", "category": "buttons", "viewport": "1440x900"},
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _load_manifest_tuples(path)
    assert result == [("aeon", "alphabet"), ("stripe", "buttons")]


def test_load_spec_tuples_absent_returns_none(tmp_path: pathlib.Path) -> None:
    """A missing specs directory yields None (self-skip signal)."""
    assert _load_spec_tuples(tmp_path / "missing") is None


def test_load_spec_tuples_sorted_pairs(tmp_path: pathlib.Path) -> None:
    """Spec filenames are parsed into sorted (brand, category) pairs."""
    (tmp_path / "stripe_alphabet.json").write_text("{}", encoding="utf-8")
    (tmp_path / "aeon_buttons.json").write_text("{}", encoding="utf-8")
    result = _load_spec_tuples(tmp_path)
    assert result == [("aeon", "buttons"), ("stripe", "alphabet")]


# ---------------------------------------------------------------------------
# Consistency contracts
# ---------------------------------------------------------------------------


def test_every_manifest_tuple_has_a_spec() -> None:
    """Every (brand, category) tuple in the manifest has a matching spec file.

    GREEN at Phase 9.3 baseline (all 12 manifest tuples have specs); pinned so
    a future manifest addition that references a non-existent spec is caught
    before the gate run box ever runs a sweep against a missing assertion.

    Resolves from CORPUS_ROOT (in-repo vendor copy) so it runs on CI.
    Self-skips when either the manifest or specs dir is absent.

    See: _HANDOFF_2026-06-13_library-v5-phase9-corpus-coverage-guard.md
    """
    manifest_tuples = _load_manifest_tuples(MANIFEST_PATH)
    if manifest_tuples is None:
        pytest.skip(f"Manifest not found at {MANIFEST_PATH}; skipping consistency check.")

    spec_tuples_list = _load_spec_tuples(SPECS_DIR)
    if spec_tuples_list is None:
        pytest.skip(f"Specs dir not found at {SPECS_DIR}; skipping consistency check.")

    spec_tuples = frozenset(spec_tuples_list)
    missing = [(b, c) for b, c in manifest_tuples if (b, c) not in spec_tuples]
    assert not missing, (
        "Manifest tuples with no matching spec file:\n"
        + "\n".join(f"  {b}_{c}.json (expected at {SPECS_DIR / f'{b}_{c}.json'})"
                    for b, c in missing)
        + "\n\nCreate the spec JSON file or remove the tuple from fidelity_targets.yml "
        "and re-run scripts/sync_fidelity_corpus.py."
    )


def test_every_spec_is_manifest_backed_or_declared_structural_only() -> None:
    """Every spec is either manifest-backed or declared in STRUCTURAL_ONLY_SPECS.

    Phase 9.3 RED: 8 PNG-less specs (aeon x3, openai x2, stripe x3) are not
    declared in STRUCTURAL_ONLY_SPECS and have no manifest entry -> fails.

    Phase 9.4 GREEN: STRUCTURAL_ONLY_SPECS is populated with all 8 PNG-less
    tuples; every spec is now either manifest-backed or explicitly declared.

    Before this contract, a genuinely missing spec and an intentionally PNG-less
    spec look identical: both appear as orphans. The allowlist makes the
    distinction explicit and auditable.

    Resolves from CORPUS_ROOT (in-repo vendor copy) so it runs on CI.
    Self-skips when either the manifest or specs dir is absent.

    See: _HANDOFF_2026-06-13_library-v5-phase9-corpus-coverage-guard.md
    """
    manifest_tuples = _load_manifest_tuples(MANIFEST_PATH)
    if manifest_tuples is None:
        pytest.skip(f"Manifest not found at {MANIFEST_PATH}; skipping consistency check.")

    spec_tuples_list = _load_spec_tuples(SPECS_DIR)
    if spec_tuples_list is None:
        pytest.skip(f"Specs dir not found at {SPECS_DIR}; skipping consistency check.")

    manifest_set = frozenset(manifest_tuples)
    undeclared = [
        (b, c) for b, c in spec_tuples_list
        if (b, c) not in manifest_set and (b, c) not in STRUCTURAL_ONLY_SPECS
    ]
    assert not undeclared, (
        "Spec files that have no manifest entry AND are not in STRUCTURAL_ONLY_SPECS:\n"
        + "\n".join(f"  {b}_{c}" for b, c in sorted(undeclared))
        + "\n\nEach spec must be either:\n"
        "  (a) manifest-backed: a reference PNG was shot and the tuple is in manifest.json\n"
        "  (b) declared in STRUCTURAL_ONLY_SPECS in this file with a rationale comment\n\n"
        "To fix: add these tuples to STRUCTURAL_ONLY_SPECS in "
        "tests/render/test_corpus_consistency.py with a one-line comment explaining "
        "why no PNG reference capture exists for each.\n"
        "See: _HANDOFF_2026-06-13_library-v5-phase9-corpus-coverage-guard.md (Phase 9.4)"
    )
