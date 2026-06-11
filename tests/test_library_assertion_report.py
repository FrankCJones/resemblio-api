"""Tests for the library brand assertion-report engine.

Purpose
-------
Proves the Phase 3 assertion engine (``app/library_assertion_report.py``) is
correct across every canonical brand state BEFORE it is run against live prod
in Phase 7.  All tests use synthetic fixtures: no network, no DB.

Canonical fixture states:

1. full-panel     - all 6 curated keys -> verdict ``panel_faithful``
2. scalar-light   - 4 of 6 keys (no commercial_signal / design_principles) -> ``panel_faithful``
3. absent-panel   - zero curated keys (D11 honest-degradation) -> ``panel_cleanly_absent``
4. broken-page    - missing required structural slot -> ``page_broken``
5. v3-chip-gating - ``missing_groups`` present and honest -> v3 invariant flagged ``intact``

Run command (from ``code/api/``):
    python -m pytest tests/test_library_assertion_report.py -v
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Fixtures: representative API-response dicts (no network, no DB)
# ---------------------------------------------------------------------------

def _make_response(
    brand_slug: str = "stripe",
    curated: dict | None = None,
    missing_groups: list[str] | None = None,
    omit_brand_slug: bool = False,
) -> dict:
    """Build a minimal synthetic brand-API-response dict for testing.

    Mirrors the shape of ``GET /v1/library/brands/{slug}`` as consumed by
    ``build_brand_assertion``.  Only the fields the assertion engine reads are
    populated; the rest are left sparse to keep fixtures readable.
    """
    payload: dict = {
        "schema_version": 2,
        "data": {
            "schema_version": "library_data_v1",
            "category_slug": "hero",
        },
    }
    if not omit_brand_slug:
        payload["data"]["brand_slug"] = brand_slug
    if curated is not None:
        payload["data"]["curated_metadata"] = curated
    if missing_groups is not None:
        payload["data"]["missing_groups"] = missing_groups
    return payload


FULL_PANEL_RESPONSE = _make_response(
    brand_slug="stripe",
    curated={
        "tier": "A",
        "category": "saas",
        "design_principles": ["clean", "structured"],
        "commercial_signal": "product-led-growth",
        "mood": ["modern", "utilitarian"],
        "applicable_to": ["saas-marketing", "api-tooling"],
    },
    missing_groups=[],
)

SCALAR_LIGHT_RESPONSE = _make_response(
    brand_slug="gwern",
    curated={
        "tier": "B",
        "category": "personal-site",
        "mood": ["intellectual", "restrained"],
        "applicable_to": ["editorial-publication"],
        # commercial_signal and design_principles absent (no system.json)
    },
    missing_groups=["buttons"],
)

ABSENT_PANEL_RESPONSE = _make_response(
    brand_slug="aeon",
    curated=None,       # no curated_metadata key at all (D11 pre-seeded brand)
    missing_groups=["buttons", "cards"],
)

BROKEN_PAGE_RESPONSE = _make_response(
    brand_slug="unknown",
    omit_brand_slug=True,   # missing required structural slot
)

V3_CHIP_RESPONSE = _make_response(
    brand_slug="figma",
    curated={
        "tier": "A",
        "category": "design-tool",
        "mood": ["professional"],
        "applicable_to": ["product-design"],
    },
    missing_groups=["buttons", "cards", "badges"],  # v3 chip-gating: honest
)


# ---------------------------------------------------------------------------
# Import the module under test (will fail RED until the module exists)
# ---------------------------------------------------------------------------

from app.library_assertion_report import (  # noqa: E402
    BRAND_VERDICT,
    BrandAssertion,
    LibraryAssertionReport,
    build_brand_assertion,
    build_report,
    render_markdown,
)


# ---------------------------------------------------------------------------
# BRAND_VERDICT constants
# ---------------------------------------------------------------------------

class TestBrandVerdictConstants:
    """The verdict literals must be a named constant, not bare strings."""

    def test_panel_faithful_defined(self):
        assert BRAND_VERDICT["panel_faithful"] == "panel_faithful"

    def test_panel_cleanly_absent_defined(self):
        assert BRAND_VERDICT["panel_cleanly_absent"] == "panel_cleanly_absent"

    def test_page_broken_defined(self):
        assert BRAND_VERDICT["page_broken"] == "page_broken"

    def test_no_extra_verdicts(self):
        """Exactly three verdicts.  Adding a fourth requires a seam review."""
        assert set(BRAND_VERDICT.keys()) == {
            "panel_faithful",
            "panel_cleanly_absent",
            "page_broken",
        }


# ---------------------------------------------------------------------------
# build_brand_assertion - five canonical states
# ---------------------------------------------------------------------------

class TestBuildBrandAssertionFullPanel:
    """State 1: all 6 curated keys -> panel_faithful."""

    def test_verdict_is_panel_faithful(self):
        result = build_brand_assertion(FULL_PANEL_RESPONSE)
        assert result["verdict"] == BRAND_VERDICT["panel_faithful"]

    def test_brand_slug_extracted(self):
        result = build_brand_assertion(FULL_PANEL_RESPONSE)
        assert result["brand_slug"] == "stripe"

    def test_all_six_fields_present(self):
        result = build_brand_assertion(FULL_PANEL_RESPONSE)
        assert set(result["present_curated_fields"]) == {
            "tier", "category", "design_principles",
            "commercial_signal", "mood", "applicable_to",
        }

    def test_missing_fields_empty(self):
        result = build_brand_assertion(FULL_PANEL_RESPONSE)
        assert result["missing_curated_fields"] == []

    def test_typeddict_keys_present(self):
        result = build_brand_assertion(FULL_PANEL_RESPONSE)
        for key in ("brand_slug", "verdict", "present_curated_fields",
                    "missing_curated_fields", "v3_chip_gating", "notes"):
            assert key in result, f"BrandAssertion missing key: {key!r}"


class TestBuildBrandAssertionScalarLight:
    """State 2: 4 of 6 keys present -> still panel_faithful (partial is faithful)."""

    def test_verdict_is_panel_faithful(self):
        result = build_brand_assertion(SCALAR_LIGHT_RESPONSE)
        assert result["verdict"] == BRAND_VERDICT["panel_faithful"]

    def test_present_and_missing_split_correctly(self):
        result = build_brand_assertion(SCALAR_LIGHT_RESPONSE)
        assert "tier" in result["present_curated_fields"]
        assert "category" in result["present_curated_fields"]
        assert "mood" in result["present_curated_fields"]
        assert "applicable_to" in result["present_curated_fields"]
        assert "commercial_signal" in result["missing_curated_fields"]
        assert "design_principles" in result["missing_curated_fields"]

    def test_at_least_tier_and_category_required_for_faithful(self):
        """A brand with ONLY tier+category still counts as faithful."""
        response = _make_response(
            brand_slug="minimal",
            curated={"tier": "C", "category": "blog"},
        )
        result = build_brand_assertion(response)
        assert result["verdict"] == BRAND_VERDICT["panel_faithful"]


class TestBuildBrandAssertionAbsentPanel:
    """State 3: zero curated keys -> panel_cleanly_absent (a PASS; honest degradation)."""

    def test_verdict_is_panel_cleanly_absent(self):
        result = build_brand_assertion(ABSENT_PANEL_RESPONSE)
        assert result["verdict"] == BRAND_VERDICT["panel_cleanly_absent"]

    def test_present_fields_empty(self):
        result = build_brand_assertion(ABSENT_PANEL_RESPONSE)
        assert result["present_curated_fields"] == []

    def test_cleanly_absent_does_not_count_as_broken(self):
        """D11: absent panel is expected; it must never be classified page_broken."""
        result = build_brand_assertion(ABSENT_PANEL_RESPONSE)
        assert result["verdict"] != BRAND_VERDICT["page_broken"]


class TestBuildBrandAssertionBrokenPage:
    """State 4: missing required structural slot -> page_broken (the only failing verdict)."""

    def test_verdict_is_page_broken(self):
        result = build_brand_assertion(BROKEN_PAGE_RESPONSE)
        assert result["verdict"] == BRAND_VERDICT["page_broken"]

    def test_brand_slug_fallback_on_broken(self):
        """Broken pages must still carry a brand_slug (fallback string) so the
        report remains readable."""
        result = build_brand_assertion(BROKEN_PAGE_RESPONSE)
        assert isinstance(result["brand_slug"], str)
        assert len(result["brand_slug"]) > 0

    def test_notes_explain_the_break(self):
        result = build_brand_assertion(BROKEN_PAGE_RESPONSE)
        assert result["notes"], "notes must explain why the page is broken"


class TestBuildBrandAssertionV3ChipGating:
    """State 5: missing_groups present and honest -> v3 chip-gating flagged intact."""

    def test_v3_chip_gating_intact(self):
        result = build_brand_assertion(V3_CHIP_RESPONSE)
        assert result["v3_chip_gating"] == "intact"

    def test_empty_missing_groups_also_intact(self):
        """An empty missing_groups list means all groups captured; still intact."""
        result = build_brand_assertion(FULL_PANEL_RESPONSE)
        assert result["v3_chip_gating"] == "intact"

    def test_absent_missing_groups_flagged(self):
        """When missing_groups key is entirely absent the v3 invariant cannot be
        verified; should be flagged as 'unknown' (not 'intact')."""
        response = _make_response(brand_slug="legacy", curated={"tier": "B"})
        # missing_groups key not set -> unknown
        result = build_brand_assertion(response)
        assert result["v3_chip_gating"] == "unknown"


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------

class TestBuildReport:
    """build_report aggregates a list of responses into a LibraryAssertionReport."""

    def _make_report(self, source: str = "fixture") -> LibraryAssertionReport:
        responses = [
            FULL_PANEL_RESPONSE,
            SCALAR_LIGHT_RESPONSE,
            ABSENT_PANEL_RESPONSE,
            V3_CHIP_RESPONSE,
        ]
        return build_report(responses, source=source)

    def test_schema_version_present(self):
        report = self._make_report()
        assert report["schema_version"] == "library_assertion_report_v1"

    def test_generated_at_is_utc_iso(self):
        import re
        report = self._make_report()
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", report["generated_at"])

    def test_source_field_set(self):
        assert self._make_report(source="fixture")["source"] == "fixture"
        assert self._make_report(source="prod")["source"] == "prod"

    def test_brand_count_matches_input(self):
        report = self._make_report()
        assert report["brand_count"] == 4

    def test_assertions_length_matches(self):
        report = self._make_report()
        assert len(report["assertions"]) == 4

    def test_all_pass_true_when_no_broken_pages(self):
        """No page_broken verdict -> all_pass: True."""
        report = self._make_report()
        assert report["all_pass"] is True

    def test_all_pass_false_when_broken_page_present(self):
        report = build_report(
            [FULL_PANEL_RESPONSE, BROKEN_PAGE_RESPONSE],
            source="fixture",
        )
        assert report["all_pass"] is False

    def test_verdict_counts_populated(self):
        report = self._make_report()
        counts = report["verdict_counts"]
        assert isinstance(counts, dict)
        # 2 panel_faithful + 1 panel_cleanly_absent + 0 page_broken
        assert counts.get("panel_faithful", 0) == 3
        assert counts.get("panel_cleanly_absent", 0) == 1
        assert counts.get("page_broken", 0) == 0

    def test_empty_input_produces_valid_report(self):
        report = build_report([], source="fixture")
        assert report["brand_count"] == 0
        assert report["all_pass"] is True
        assert report["assertions"] == []


# ---------------------------------------------------------------------------
# CURATED_METADATA_FIELDS single-source contract
# ---------------------------------------------------------------------------

class TestCuratedMetadataFieldsSingleSource:
    """The assertion engine must import CURATED_METADATA_FIELDS from routes.library,
    not maintain a separate hardcoded list.  This test fails if the engine
    defines its own copy of the field set."""

    def test_engine_uses_routes_library_field_set(self):
        """Import both and confirm they are the exact same object (or equal set).
        If the engine hardcodes a copy, add/remove a field in routes.library and
        this test will catch the drift."""
        from app.routes.library import CURATED_METADATA_FIELDS as SEAM_FIELDS
        from app.library_assertion_report import _CURATED_FIELDS_FROM_SEAM

        assert _CURATED_FIELDS_FROM_SEAM == SEAM_FIELDS, (
            "assertion engine's field set differs from routes.library.CURATED_METADATA_FIELDS; "
            "do NOT hardcode a second copy - import the constant"
        )


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------

class TestRenderMarkdown:
    """render_markdown produces a valid Markdown contact sheet."""

    def _full_report(self) -> LibraryAssertionReport:
        return build_report(
            [FULL_PANEL_RESPONSE, SCALAR_LIGHT_RESPONSE,
             ABSENT_PANEL_RESPONSE, BROKEN_PAGE_RESPONSE],
            source="fixture",
        )

    def test_returns_string(self):
        assert isinstance(render_markdown(self._full_report()), str)

    def test_contains_all_brand_slugs(self):
        md = render_markdown(self._full_report())
        for slug in ("stripe", "gwern", "aeon"):
            assert slug in md, f"brand slug {slug!r} missing from markdown"

    def test_contains_verdict_tokens(self):
        md = render_markdown(self._full_report())
        assert "panel_faithful" in md
        assert "panel_cleanly_absent" in md
        assert "page_broken" in md

    def test_contains_schema_version(self):
        md = render_markdown(self._full_report())
        assert "library_assertion_report_v1" in md
