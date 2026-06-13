"""Tests for the curation metadata-overlay seam (``app.metadata_overrides``).

Phase 2B of the Library v5 plan. Some DRL-curated taxonomy is mis-tagged for a
small set of brands (Apple carries ``applicable_to: saas-marketing`` and
``category: consumer-dtc``, neither right for a premium consumer-product-
marketing site). The DRL tree is read-only from Resemblio, so the correction is
a Resemblio-owned, schema-versioned overlay applied to the brand-stripped entry
at seed time, per Frank's Option B decision (2026-06-12). DRL is never written.

These are pure-function tests with synthetic ``StrippedEntry`` fixtures: no
network, no DRL disk read, no DB. They pin:

  1. an allowlisted brand (apple) has the overlay applied (remove + add + set)
  2. a non-allowlisted brand passes through byte-for-byte unchanged
  3. the overlay is idempotent (applying twice == applying once)
  4. the shipped JSON loads, carries the schema version, and corrects apple
  5. the loader rejects an unknown schema version (forward-compat guard)

Run (from ``code/api``):
    python -m pytest tests/test_metadata_overrides.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from transformer import STRIPPED_SCHEMA_VERSION, StrippedEntry

from app.metadata_overrides import (
    METADATA_OVERRIDES_SCHEMA_VERSION,
    apply_metadata_overrides,
    load_metadata_overrides,
)


def _make_entry(
    slug: str,
    *,
    category: str,
    applicable_to: tuple[str, ...],
) -> StrippedEntry:
    """Build a synthetic StrippedEntry for one asset of a brand."""
    return StrippedEntry(
        source_id=f"{slug}/alphabet/{slug}",
        slug=slug,
        cls="",
        kind="alphabet",
        tldr="synthetic fixture",
        patterns=(),
        mood=("confident",),
        applicable_to=applicable_to,
        tags=("alphabet",),
        provenance_score="0.9",
        tier="A",
        category=category,
        schema_version=STRIPPED_SCHEMA_VERSION,
    )


class TestApplyMetadataOverrides:
    def test_apple_overlay_removes_miscategory_and_adds_correct(self) -> None:
        # Apple's first alphabet asset carries the clearly-wrong saas-marketing
        # tag; the overlay must strip it and the DTC product-marketing reframe.
        entry = _make_entry(
            "apple",
            category="consumer-dtc",
            applicable_to=("saas-marketing", "editorial-publication"),
        )
        corrected = apply_metadata_overrides(entry)

        assert "saas-marketing" not in corrected.applicable_to
        assert "editorial-publication" not in corrected.applicable_to
        assert "consumer-dtc" in corrected.applicable_to
        assert corrected.category == "marketing-modern"
        # Untouched fields are preserved.
        assert corrected.slug == "apple"
        assert corrected.mood == ("confident",)
        assert corrected.tier == "A"

    def test_overlay_applies_to_non_canonical_asset_slug(self) -> None:
        # A brand's layout/whole assets carry an asset slug that differs from
        # the brand slug (e.g. "apple-marketing-page-001"). The overlay keys on
        # the brand (source_id first segment), so it must still correct these -
        # otherwise only the alphabet specimen gets fixed and the corpus is
        # internally inconsistent.
        entry = StrippedEntry(
            source_id="apple/layout/apple-marketing-page-001",
            slug="apple-marketing-page-001",
            cls="",
            kind="layout",
            tldr="synthetic layout asset",
            patterns=(),
            mood=("confident",),
            applicable_to=("saas-marketing",),
            tags=("layout",),
            provenance_score="0.9",
            tier="A",
            category="consumer-dtc",
            schema_version=STRIPPED_SCHEMA_VERSION,
        )
        corrected = apply_metadata_overrides(entry)
        assert corrected.category == "marketing-modern"
        assert "saas-marketing" not in corrected.applicable_to
        assert "consumer-dtc" in corrected.applicable_to

    def test_explicit_brand_slug_overrides_source_id_derivation(self) -> None:
        entry = _make_entry(
            "apple",
            category="consumer-dtc",
            applicable_to=("saas-marketing",),
        )
        # Passing a non-allowlisted brand_slug suppresses the overlay even though
        # source_id would derive "apple".
        assert apply_metadata_overrides(entry, brand_slug="not-apple") == entry

    def test_non_allowlisted_brand_is_unchanged(self) -> None:
        entry = _make_entry(
            "stripe",
            category="marketing-modern",
            applicable_to=("saas-marketing", "dev-tools"),
        )
        corrected = apply_metadata_overrides(entry)
        # A brand outside the bounded allowlist must pass through identically -
        # the overlay never touches saas-marketing for stripe (it's correct there).
        assert corrected == entry

    def test_overlay_is_idempotent(self) -> None:
        entry = _make_entry(
            "apple",
            category="consumer-dtc",
            applicable_to=("saas-marketing", "editorial-publication"),
        )
        once = apply_metadata_overrides(entry)
        twice = apply_metadata_overrides(once)
        assert once == twice

    def test_add_does_not_duplicate_existing_token(self) -> None:
        # If a later corpus already carries the corrected token, the add must
        # not produce a duplicate.
        entry = _make_entry(
            "apple",
            category="consumer-dtc",
            applicable_to=("consumer-dtc",),
        )
        corrected = apply_metadata_overrides(entry)
        assert corrected.applicable_to.count("consumer-dtc") == 1


class TestLoadMetadataOverrides:
    def test_shipped_json_loads_and_corrects_apple(self) -> None:
        overrides = load_metadata_overrides()
        assert "apple" in overrides, "apple must be in the bounded allowlist"
        apple = overrides["apple"]
        assert apple.get("category") == "marketing-modern"
        assert "saas-marketing" in (apple.get("applicable_to_remove") or [])

    def test_schema_version_constant_matches_shipped_file(self) -> None:
        path = Path(__file__).resolve().parents[1] / "app" / "metadata_overrides.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["schema_version"] == METADATA_OVERRIDES_SCHEMA_VERSION

    def test_rejects_unknown_schema_version(self, tmp_path: Path) -> None:
        bad = tmp_path / "metadata_overrides.json"
        bad.write_text(
            json.dumps({"schema_version": 999, "overrides": {}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="schema_version"):
            load_metadata_overrides(bad)
