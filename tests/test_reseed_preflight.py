"""Pre-apply corpus preflight tests for the Library v4 re-seed ceremony.

Purpose
-------
Phase 4 of the Library v4 re-seed handoff.  Proves that every brand in the
DRL corpus would yield a valid assertion verdict BEFORE ``seed_from_drl --apply``
fires against the prod DB.

Two test categories:

1. **Synthetic tests** (always run, no filesystem deps):
   Exercise ``preflight_corpus`` with hand-crafted ``(slug, dtcg_json)`` pairs
   covering the same canonical states the Phase 3 tests cover.  These run
   regardless of whether the DRL tree is on disk.

2. **Live corpus test** (self-skipping when DRL absent):
   ``test_all_40_corpus_brands_preflight_clean`` loads the real DRL corpus,
   runs every brand through ``brand_strip`` -> ``build_bundle`` ->
   ``preflight_corpus``, and asserts zero ``page_broken`` verdicts.  If any
   brand is broken the test fails with a per-brand breakdown so the blockage
   can be escalated to Opus before the re-seed fires.

   The test self-skips when neither of the three DRL root candidates is
   present, mirroring the pattern in ``test_button_corpus_coverage.py``.

Self-skip resolution order (mirrors production + local-dev conventions):
1. ``RESEMBLIO_DRL_ROOT`` env var (set in CI)
2. ``/opt/resemblio-api/drl`` (prod server path)
3. ``<workspace>/projects/Design Reference Library/`` (local dev)

Run command (from ``code/api/``):
    python -m pytest tests/test_reseed_preflight.py -v
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from app.library_assertion_report import (
    BRAND_VERDICT,
    LibraryAssertionReport,
    preflight_corpus,
)


# ---------------------------------------------------------------------------
# Path helpers (mirror test_button_corpus_coverage.py conventions)
# ---------------------------------------------------------------------------


def _api_root() -> Path:
    """Return the ``code/api/`` package root.

    Resolves as: tests/test_reseed_preflight.py -> tests/ -> code/api/
    """
    return Path(__file__).resolve().parents[1]


def _find_drl_root() -> Path | None:
    """Return the first accessible DRL corpus root, or None.

    Check order:
    1. ``RESEMBLIO_DRL_ROOT`` environment variable
    2. ``/opt/resemblio-api/drl`` (prod server)
    3. ``<workspace>/projects/Design Reference Library/`` (local dev)

    A root is considered accessible when ``corpus.json`` exists under it.
    """
    candidates: list[Path] = []
    env_root = os.environ.get("RESEMBLIO_DRL_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path("/opt/resemblio-api/drl"))
    # code/api -> code -> Resemblio -> projects -> workspace / Design Reference Library
    workspace_drl = _api_root().parents[2] / "Design Reference Library"
    candidates.append(workspace_drl)

    for root in candidates:
        if (root / "corpus.json").exists():
            return root
    return None


# ---------------------------------------------------------------------------
# Synthetic fixtures for the always-run tests
# ---------------------------------------------------------------------------

#: A dtcg_json that carries all 6 curated fields.
_FULL_DTCG: dict[str, Any] = {
    "schema_version": "v1",
    "slug": "stripe",
    "tier": "A",
    "category": "saas",
    "design_principles": ["clean", "structured"],
    "commercial_signal": "product-led-growth",
    "mood": ["modern"],
    "applicable_to": ["saas-marketing"],
    "tokens": {},
}

#: A dtcg_json that carries only tier + category (minimal faithful).
_MINIMAL_DTCG: dict[str, Any] = {
    "schema_version": "v1",
    "slug": "linear",
    "tier": "A",
    "category": "productivity",
    "tokens": {},
}

#: A dtcg_json with NO curated fields - the D11 honest-degradation case.
_ABSENT_DTCG: dict[str, Any] = {
    "schema_version": "v1",
    "slug": "aeon",
    "tokens": {},
}


# ---------------------------------------------------------------------------
# Synthetic tests - always run
# ---------------------------------------------------------------------------


class TestPreflightCorpusSynthetic:
    """Exercises preflight_corpus with synthetic dtcg_json bundles.

    No filesystem access: these tests run on every CI run and local run
    regardless of whether the DRL tree is present.
    """

    def test_all_pass_true_for_fully_curated_bundles(self) -> None:
        """All curated -> all_pass True, all panel_faithful."""
        bundles = [("stripe", _FULL_DTCG), ("linear", _MINIMAL_DTCG)]
        report = preflight_corpus(bundles, source="fixture")
        assert report["all_pass"] is True
        assert report["brand_count"] == 2
        for assertion in report["assertions"]:
            assert assertion["verdict"] == BRAND_VERDICT["panel_faithful"], (
                f"Expected panel_faithful for {assertion['brand_slug']!r}, "
                f"got {assertion['verdict']!r}"
            )

    def test_absent_panel_is_pass_not_broken(self) -> None:
        """D11 honest-degradation: absent curated fields -> panel_cleanly_absent, not page_broken."""
        bundles = [("aeon", _ABSENT_DTCG)]
        report = preflight_corpus(bundles, source="fixture")
        assert report["all_pass"] is True
        assert report["assertions"][0]["verdict"] == BRAND_VERDICT["panel_cleanly_absent"]
        assert report["assertions"][0]["verdict"] != BRAND_VERDICT["page_broken"]

    def test_mixed_corpus_all_pass(self) -> None:
        """Faithful + absent brands together -> all_pass True."""
        bundles = [
            ("stripe", _FULL_DTCG),
            ("aeon", _ABSENT_DTCG),
            ("linear", _MINIMAL_DTCG),
        ]
        report = preflight_corpus(bundles, source="fixture")
        assert report["all_pass"] is True
        assert report["brand_count"] == 3

    def test_schema_version_and_source_set(self) -> None:
        """Output carries schema_version and source for downstream consumers."""
        report = preflight_corpus([("stripe", _FULL_DTCG)], source="fixture")
        assert report["schema_version"] == "library_assertion_report_v1"
        assert report["source"] == "fixture"

    def test_empty_corpus_returns_valid_report(self) -> None:
        """Zero bundles -> brand_count 0, all_pass True (no broken pages)."""
        report = preflight_corpus([], source="fixture")
        assert report["brand_count"] == 0
        assert report["all_pass"] is True
        assert report["assertions"] == []

    def test_curated_fields_extracted_correctly(self) -> None:
        """Only the 6 curated fields flow into the assertion; token noise ignored."""
        noisy_dtcg = {
            **_FULL_DTCG,
            "not_a_curated_field": "should not appear",
            "tokens": {"color-primary": "#f00"},
        }
        report = preflight_corpus([("stripe", noisy_dtcg)], source="fixture")
        a = report["assertions"][0]
        assert "not_a_curated_field" not in a["present_curated_fields"]
        assert "not_a_curated_field" not in a["missing_curated_fields"]

    def test_null_curated_value_treated_as_absent(self) -> None:
        """A curated key present in dtcg_json but with value None counts as absent."""
        dtcg_with_null = {
            "schema_version": "v1",
            "slug": "gwern",
            "tier": "B",
            "category": "personal-site",
            "design_principles": None,  # explicitly None
            "tokens": {},
        }
        report = preflight_corpus([("gwern", dtcg_with_null)], source="fixture")
        a = report["assertions"][0]
        assert "design_principles" not in a["present_curated_fields"]

    def test_v3_chip_gating_is_intact_in_preflight(self) -> None:
        """preflight_corpus injects missing_groups=[] so chip-gating reports intact."""
        report = preflight_corpus([("stripe", _FULL_DTCG)], source="fixture")
        assert report["assertions"][0]["v3_chip_gating"] == "intact"

    def test_verdict_counts_sum_to_brand_count(self) -> None:
        """verdict_counts values must sum to brand_count."""
        bundles = [("stripe", _FULL_DTCG), ("aeon", _ABSENT_DTCG)]
        report = preflight_corpus(bundles, source="fixture")
        total = sum(report["verdict_counts"].values())
        assert total == report["brand_count"]


# ---------------------------------------------------------------------------
# Live corpus test - self-skipping when DRL absent
# ---------------------------------------------------------------------------


def test_all_40_corpus_brands_preflight_clean() -> None:
    """Pre-apply proof: every DRL brand produces a valid assertion verdict.

    Loads the real DRL corpus, builds a bundle for every brand via the same
    pipeline that ``seed_from_drl --apply`` would use, then runs
    ``preflight_corpus`` on the results.

    Acceptance gates:
    - ``all_pass: True`` (zero ``page_broken`` verdicts)
    - Every ``panel_faithful`` brand carries both ``tier`` and ``category``

    STOP + surface to Opus if this test fails.  Do NOT run
    ``seed_from_drl --apply`` while any brand is broken.
    """
    drl_root = _find_drl_root()
    if drl_root is None:
        pytest.skip(
            "No DRL corpus.json found at RESEMBLIO_DRL_ROOT, "
            "/opt/resemblio-api/drl, or the workspace DRL project. "
            "Preflight test requires the live DRL corpus."
        )

    # Import corpus tools only when DRL is present; keeps the module importable
    # even when the DRL subtree is absent (e.g. fresh API-only checkout).
    from scripts.seed_from_drl import (
        build_bundle,
        iter_assets,
        load_corpus,
        load_system_json,
        load_tokens_for_asset,
    )
    from transformer import brand_strip  # type: ignore[import]

    corpus = load_corpus(drl_root)
    bundles: list[tuple[str, dict[str, Any]]] = []
    skipped_slugs: list[str] = []

    for system, asset in iter_assets(corpus):
        try:
            stripped = brand_strip(system, asset)
        except ValueError:
            skipped_slugs.append(str(asset.get("source_id", "?unknown")))
            continue

        tokens = load_tokens_for_asset(drl_root, asset)
        if not tokens:
            # No tokens.css: the seeder would skip this row too.
            skipped_slugs.append(stripped.source_id)
            continue

        # Load optional curated metadata from system.json (same as apply_seed).
        system_json = load_system_json(drl_root, stripped.slug)
        design_principles: list[str] | None = None
        commercial_signal: str | None = None
        if system_json:
            dp = system_json.get("design_principles")
            if isinstance(dp, list):
                design_principles = dp
            cs = system_json.get("commercial_signal")
            if isinstance(cs, str):
                commercial_signal = cs

        seed_bundle = build_bundle(
            stripped,
            tokens,
            design_principles=design_principles,
            commercial_signal=commercial_signal,
        )
        bundles.append((stripped.slug, seed_bundle.dtcg_json))

    assert bundles, "No brands built from DRL corpus - corpus may be empty or malformed"

    report: LibraryAssertionReport = preflight_corpus(bundles, source="fixture")

    # ---------- Gate 1: no broken pages ----------
    broken = [
        a for a in report["assertions"]
        if a["verdict"] == BRAND_VERDICT["page_broken"]
    ]
    assert report["all_pass"] is True, (
        f"PREFLIGHT FAILED - {len(broken)} brand(s) are page_broken. "
        "Do NOT run seed_from_drl --apply until these are resolved.\n"
        + "\n".join(
            f"  [{a['brand_slug']}] {a['notes']}" for a in broken
        )
    )

    # ---------- Gate 2: faithful brands carry tier + category ----------
    faithful_missing_anchor: list[str] = []
    for a in report["assertions"]:
        if a["verdict"] != BRAND_VERDICT["panel_faithful"]:
            continue
        present = set(a["present_curated_fields"])
        if not {"tier", "category"}.issubset(present):
            faithful_missing_anchor.append(
                f"[{a['brand_slug']}] present={sorted(present)}"
            )
    assert not faithful_missing_anchor, (
        "panel_faithful brands must carry both tier and category. Missing:\n"
        + "\n".join(f"  {m}" for m in faithful_missing_anchor)
    )

    # Report summary to test output (visible with pytest -v -s)
    print(
        f"\nPreflight result: {report['brand_count']} brands assessed "
        f"({len(skipped_slugs)} skipped at build step). "
        f"Verdicts: {report['verdict_counts']}. "
        f"all_pass={report['all_pass']}"
    )
