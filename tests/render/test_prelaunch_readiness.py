"""Tests for the prelaunch_readiness aggregator (Phase 14).

Tests `assess_public_readiness`, `load_gate_report`, and `render_readiness_markdown`
from `tests.render.prelaunch_readiness`.

All synthetic fixture factories produce v6-schema dicts so the tests are
independent of workspace files. The one exception is the v3 stale-report
regression test (Phase 14.2), which uses the actual on-disk audit fixture
and self-skips when it is absent (CI compatibility).
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict, List

import pytest

from tests.render.prelaunch_readiness import (
    ReadinessReason,
    ReadinessVerdict,
    assess_public_readiness,
    load_gate_report,
    render_readiness_markdown,
)


# ---------------------------------------------------------------------------
# Fixture factory
# ---------------------------------------------------------------------------

_WORKSPACE_ROOT = pathlib.Path(__file__).parents[4]
_V3_REPORT_PATH = (
    _WORKSPACE_ROOT
    / "_verification"
    / "library-inspirado-correction-20260604"
    / "fidelity_gate_runs"
    / "20260613T223602Z"
    / "gate_report.json"
)


def _make_tuple(
    brand: str = "apple",
    category: str = "alphabet",
    viewport: str = "1440x900",
    status: str = "PASS",
    drift_dimensions: List[str] | None = None,
    browser_eval_missing: List[str] | None = None,
    content_drift: List[str] | None = None,
) -> Dict[str, Any]:
    """Build a synthetic TupleOutcome dict for testing."""
    return {
        "tuple_id": f"{brand}__{category}__{viewport}",
        "brand": brand,
        "category": category,
        "viewport": viewport,
        "status": status,
        "gate": "structural" if status == "PASS" else "fail",
        "ssim": 0.72,
        "color_bucket_overlap": 4,
        "font_family_match": True,
        "drift_dimensions": drift_dimensions if drift_dimensions is not None else [],
        "error_message": None,
        "live_status_code": 200,
        "browser_eval_missing": browser_eval_missing if browser_eval_missing is not None else [],
        "content_drift": content_drift if content_drift is not None else [],
    }


def _clean_v6_report(**overrides: Any) -> Dict[str, Any]:
    """Build a synthetic clean v6 report dict for testing.

    Produces 6 tuples across 3 brand-category combos (all passing) for a
    brand_x_category_passes=3 aggregate that meets the default floor.
    """
    tuples = [
        _make_tuple(brand="apple", category="alphabet", viewport="1440x900"),
        _make_tuple(brand="apple", category="alphabet", viewport="375x812"),
        _make_tuple(brand="vercel", category="alphabet", viewport="1440x900"),
        _make_tuple(brand="vercel", category="alphabet", viewport="375x812"),
        _make_tuple(brand="quanta", category="alphabet", viewport="1440x900"),
        _make_tuple(brand="quanta", category="alphabet", viewport="375x812"),
    ]
    report: Dict[str, Any] = {
        "schema_version": "library_visual_fidelity_gate_report_v6",
        "generated_at_utc": "2026-06-14T00:00:00Z",
        "workspace_root": "/workspace",
        "reference_root": "/refs",
        "resemblio_base": "https://resemblio.com",
        "tolerance": {"brand_x_category_pass_minimum": 3},
        "total_tuples": 6,
        "pass_count": 6,
        "fail_count": 0,
        "skip_count": 0,
        "brand_x_category_passes": 3,
        "aggregate": "PASS",
        "compat_schema_version": "library_visual_fidelity_gate_report_v5",
        "tuples": tuples,
    }
    report.update(overrides)
    return report


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _get_reason(verdict: ReadinessVerdict, check: str) -> ReadinessReason:
    """Return the reason with the given check id; raise if absent."""
    for r in verdict.reasons:
        if r.check == check:
            return r
    raise AssertionError(
        f"Reason '{check}' not found. Present checks: {[r.check for r in verdict.reasons]}"
    )


# ---------------------------------------------------------------------------
# Phase 14.1 tests - core aggregator
# ---------------------------------------------------------------------------


class TestAssessPublicReadiness:
    """Tests for assess_public_readiness with synthetic v6 reports."""

    def test_clean_report_is_go(self) -> None:
        """A fully clean v6 report produces go=True with all hard checks ok."""
        report = _clean_v6_report()
        verdict = assess_public_readiness(report)

        assert verdict.go is True
        assert isinstance(verdict, ReadinessVerdict)
        assert verdict.schema_version == "prelaunch_readiness_v1"
        assert verdict.gate_report_schema == "library_visual_fidelity_gate_report_v6"
        for r in verdict.reasons:
            if r.check not in ("browser_eval_complete",):
                assert r.ok is True, f"Hard check '{r.check}' should be ok=True on clean report"

    def test_wordmark_leak_is_no_go(self) -> None:
        """A tuple with wordmark_leak in drift_dimensions -> go=False, trademark_clean=False."""
        tuples = [
            _make_tuple(brand="apple", category="alphabet", viewport="1440x900",
                        drift_dimensions=["wordmark_leak"]),
            _make_tuple(brand="apple", category="alphabet", viewport="375x812"),
        ]
        report = _clean_v6_report(tuples=tuples, brand_x_category_passes=0, aggregate="FAIL")
        verdict = assess_public_readiness(report)

        assert verdict.go is False
        trademark = _get_reason(verdict, "trademark_clean")
        assert trademark.ok is False
        assert "wordmark" in trademark.detail.lower() or "leak" in trademark.detail.lower()

    def test_avatar_photo_leak_is_no_go(self) -> None:
        """A tuple with avatar_photo_leak in drift_dimensions -> go=False, pii_clean=False."""
        tuples = [
            _make_tuple(brand="aeon", category="about-team", viewport="1440x900",
                        drift_dimensions=["avatar_photo_leak"]),
            _make_tuple(brand="aeon", category="about-team", viewport="375x812"),
        ]
        report = _clean_v6_report(tuples=tuples, brand_x_category_passes=0, aggregate="FAIL")
        verdict = assess_public_readiness(report)

        assert verdict.go is False
        pii = _get_reason(verdict, "pii_clean")
        assert pii.ok is False
        assert "avatar" in pii.detail.lower() or "photo" in pii.detail.lower()

    def test_below_bxc_floor_is_no_go(self) -> None:
        """brand_x_category_passes below floor -> go=False, coverage_floor_met=False."""
        report = _clean_v6_report(brand_x_category_passes=2)
        verdict = assess_public_readiness(report)

        assert verdict.go is False
        cov = _get_reason(verdict, "coverage_floor_met")
        assert cov.ok is False

    def test_aggregate_fail_is_no_go(self) -> None:
        """aggregate='FAIL' in the report -> go=False, aggregate_pass=False."""
        report = _clean_v6_report(aggregate="FAIL")
        verdict = assess_public_readiness(report)

        assert verdict.go is False
        agg = _get_reason(verdict, "aggregate_pass")
        assert agg.ok is False

    def test_browser_eval_missing_is_soft_warning(self) -> None:
        """browser_eval_missing on a tuple -> go=True (soft), detail names the count."""
        tuples = [
            _make_tuple(brand="apple", category="alphabet", viewport="1440x900",
                        browser_eval_missing=["avatar_photo_no_leak_assertion"]),
            _make_tuple(brand="apple", category="alphabet", viewport="375x812"),
            _make_tuple(brand="vercel", category="alphabet", viewport="1440x900"),
            _make_tuple(brand="vercel", category="alphabet", viewport="375x812"),
            _make_tuple(brand="quanta", category="alphabet", viewport="1440x900"),
            _make_tuple(brand="quanta", category="alphabet", viewport="375x812"),
        ]
        report = _clean_v6_report(tuples=tuples)
        verdict = assess_public_readiness(report)

        assert verdict.go is True
        browser = _get_reason(verdict, "browser_eval_complete")
        assert "1" in browser.detail or "missing" in browser.detail.lower()

    def test_multiple_failures_all_surfaces(self) -> None:
        """Multiple hard failures -> go=False, all failing reasons present."""
        tuples = [
            _make_tuple(drift_dimensions=["wordmark_leak", "avatar_photo_leak"]),
        ]
        report = _clean_v6_report(
            tuples=tuples,
            brand_x_category_passes=0,
            aggregate="FAIL",
        )
        verdict = assess_public_readiness(report)

        assert verdict.go is False
        assert _get_reason(verdict, "trademark_clean").ok is False
        assert _get_reason(verdict, "pii_clean").ok is False
        assert _get_reason(verdict, "coverage_floor_met").ok is False
        assert _get_reason(verdict, "aggregate_pass").ok is False

    def test_verdict_carries_schema_version(self) -> None:
        """ReadinessVerdict.schema_version is always 'prelaunch_readiness_v1'."""
        verdict = assess_public_readiness(_clean_v6_report())
        assert verdict.schema_version == "prelaunch_readiness_v1"

    def test_verdict_carries_gate_report_schema(self) -> None:
        """ReadinessVerdict.gate_report_schema mirrors the input report schema."""
        verdict = assess_public_readiness(_clean_v6_report())
        assert verdict.gate_report_schema == "library_visual_fidelity_gate_report_v6"

    def test_floor_from_tolerance_block(self) -> None:
        """BXC floor is read from tolerance block when present."""
        # tolerance says floor=2; bxc=2 passes
        report = _clean_v6_report(
            brand_x_category_passes=2,
            tolerance={"brand_x_category_pass_minimum": 2},
        )
        verdict = assess_public_readiness(report)
        cov = _get_reason(verdict, "coverage_floor_met")
        assert cov.ok is True

    def test_floor_fallback_to_default(self) -> None:
        """BXC floor falls back to DEFAULT_BXC_FLOOR when tolerance block omits it."""
        report = _clean_v6_report(
            brand_x_category_passes=2,
            tolerance={},  # no brand_x_category_pass_minimum key
        )
        verdict = assess_public_readiness(report)
        # default floor is 3; bxc=2 is below floor
        cov = _get_reason(verdict, "coverage_floor_met")
        assert cov.ok is False


# ---------------------------------------------------------------------------
# Phase 14.2 tests - stale-schema rejection + load_gate_report
# ---------------------------------------------------------------------------


class TestSchemaSupported:
    """Tests for the schema_supported hard check and load_gate_report IO helper."""

    def test_v3_report_is_no_go(self) -> None:
        """A v3 report (pre-enforcement) is a hard NO-GO on schema_supported."""
        report = _clean_v6_report(
            schema_version="library_visual_fidelity_gate_report_v3",
            aggregate="PASS",
            brand_x_category_passes=3,
        )
        verdict = assess_public_readiness(report)

        assert verdict.go is False
        schema = _get_reason(verdict, "schema_supported")
        assert schema.ok is False
        assert "v3" in schema.detail

    def test_v4_report_is_no_go(self) -> None:
        """A v4 report (predates avatar_photo_leak enforcement) is a hard NO-GO."""
        report = _clean_v6_report(
            schema_version="library_visual_fidelity_gate_report_v4",
            aggregate="PASS",
            brand_x_category_passes=3,
        )
        verdict = assess_public_readiness(report)

        assert verdict.go is False
        schema = _get_reason(verdict, "schema_supported")
        assert schema.ok is False

    def test_v5_report_passes_schema_check(self) -> None:
        """A v5 report is within the compat window - schema_supported ok=True."""
        report = _clean_v6_report(
            schema_version="library_visual_fidelity_gate_report_v5",
        )
        verdict = assess_public_readiness(report)
        schema = _get_reason(verdict, "schema_supported")
        assert schema.ok is True

    def test_v6_report_passes_schema_check(self) -> None:
        """A v6 report is the current schema - schema_supported ok=True."""
        report = _clean_v6_report()
        verdict = assess_public_readiness(report)
        schema = _get_reason(verdict, "schema_supported")
        assert schema.ok is True

    @pytest.mark.skipif(
        not _V3_REPORT_PATH.exists(),
        reason="v3 audit fixture not in workspace (_verification/ tree absent)",
    )
    def test_actual_v3_audit_fixture_is_no_go(self) -> None:
        """The actual v3 on-disk fixture from Gate 13 audit -> go=False, schema_supported=False."""
        report = load_gate_report(_V3_REPORT_PATH)
        assert report["schema_version"] == "library_visual_fidelity_gate_report_v3"

        verdict = assess_public_readiness(report)
        assert verdict.go is False

        schema = _get_reason(verdict, "schema_supported")
        assert schema.ok is False
        assert "v3" in schema.detail

    def test_load_gate_report_raises_on_missing_file(self) -> None:
        """load_gate_report raises FileNotFoundError for a nonexistent path."""
        missing = pathlib.Path("/nonexistent/path/gate_report.json")
        with pytest.raises(FileNotFoundError):
            load_gate_report(missing)

    def test_load_gate_report_raises_on_invalid_json(self, tmp_path: pathlib.Path) -> None:
        """load_gate_report raises ValueError for a file with invalid JSON."""
        bad = tmp_path / "bad_report.json"
        bad.write_text("not-valid-json{{{", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_gate_report(bad)

    def test_load_gate_report_returns_dict(self, tmp_path: pathlib.Path) -> None:
        """load_gate_report returns the parsed dict from a valid JSON file."""
        import json

        data = {"schema_version": "test", "aggregate": "PASS"}
        report_file = tmp_path / "gate_report.json"
        report_file.write_text(json.dumps(data), encoding="utf-8")

        result = load_gate_report(report_file)
        assert result == data


# ---------------------------------------------------------------------------
# Phase 14.3 tests - Markdown rendering
# ---------------------------------------------------------------------------


class TestRenderReadinessMarkdown:
    """Tests for render_readiness_markdown."""

    def test_go_verdict_headline(self) -> None:
        """A GO verdict produces a top-line 'GO' headline."""
        report = _clean_v6_report()
        verdict = assess_public_readiness(report)
        md = render_readiness_markdown(verdict)

        assert "GO" in md
        assert "NO-GO" not in md
        first_line = md.splitlines()[0]
        assert "GO" in first_line

    def test_no_go_verdict_headline(self) -> None:
        """A NO-GO verdict produces a top-line 'NO-GO' headline."""
        report = _clean_v6_report(aggregate="FAIL")
        verdict = assess_public_readiness(report)
        md = render_readiness_markdown(verdict)

        assert "NO-GO" in md
        assert md.splitlines()[0].count("NO-GO") >= 1

    def test_each_reason_appears(self) -> None:
        """Every reason check id appears in the markdown output."""
        report = _clean_v6_report()
        verdict = assess_public_readiness(report)
        md = render_readiness_markdown(verdict)

        for r in verdict.reasons:
            assert r.check in md, f"Reason check '{r.check}' missing from markdown"

    def test_each_reason_has_detail(self) -> None:
        """Each reason's detail string appears in the markdown output."""
        report = _clean_v6_report()
        verdict = assess_public_readiness(report)
        md = render_readiness_markdown(verdict)

        for r in verdict.reasons:
            assert r.detail in md, f"Detail for '{r.check}' missing from markdown"

    def test_failing_check_named_in_no_go(self) -> None:
        """A NO-GO verdict names the failing check(s) in the bottom summary."""
        report = _clean_v6_report(aggregate="FAIL")
        verdict = assess_public_readiness(report)
        md = render_readiness_markdown(verdict)

        lines = md.splitlines()
        last_paragraph = " ".join(lines[-3:])
        assert "aggregate_pass" in last_paragraph

    def test_returns_string(self) -> None:
        """render_readiness_markdown always returns a str."""
        verdict = assess_public_readiness(_clean_v6_report())
        assert isinstance(render_readiness_markdown(verdict), str)
