"""Tests for the library re-seed verification engine.

Purpose
-------
Phase A: Proves ``reconcile_reports`` correctly detects every class of
divergence between the offline preflight prediction and the live post-re-seed
assertion report.

All tests use synthetic ``LibraryAssertionReport`` fixtures: no network, no DB,
no filesystem.

Run command (from ``code/api/``):
    python -m pytest tests/test_library_reseed_verification.py -v
"""
from __future__ import annotations

import re

import pytest

# ---------------------------------------------------------------------------
# Helper: build minimal synthetic LibraryAssertionReport fixtures
# ---------------------------------------------------------------------------

from app.library_assertion_report import (
    BrandAssertion,
    LibraryAssertionReport,
    build_report,
)


def _brand_response(
    brand_slug: str,
    *,
    faithful: bool = True,
) -> dict:
    """Build a minimal synthetic brand API-response dict.

    Parameters
    ----------
    brand_slug:
        The slug to embed.
    faithful:
        When True, include ``tier`` + ``category`` so the verdict is
        ``panel_faithful``.  When False, omit curated_metadata so the
        verdict is ``panel_cleanly_absent``.
    """
    data: dict = {
        "schema_version": "library_data_v1",
        "brand_slug": brand_slug,
        "category_slug": "hero",
        "missing_groups": [],
    }
    if faithful:
        data["curated_metadata"] = {"tier": "A", "category": "saas"}
    return {"schema_version": 2, "data": data}


def _make_report(
    brands: list[tuple[str, bool]],
    *,
    source: str = "fixture",
    schema_version: str | None = None,
) -> LibraryAssertionReport:
    """Build a ``LibraryAssertionReport`` from a list of (slug, faithful) pairs.

    Parameters
    ----------
    brands:
        Each entry is ``(brand_slug, faithful)``.  ``faithful=True`` -> verdict
        ``panel_faithful``; ``False`` -> ``panel_cleanly_absent``.
    source:
        Passed through to ``build_report``.
    schema_version:
        When set, overrides the ``schema_version`` field of the produced report
        (used to simulate incompatible-report scenarios).
    """
    responses = [_brand_response(slug, faithful=f) for slug, f in brands]
    report = build_report(responses, source=source)
    if schema_version is not None:
        report = dict(report)  # type: ignore[assignment]
        report["schema_version"] = schema_version  # type: ignore[index]
    return report  # type: ignore[return-value]


def _make_report_with_dup_slug(*slugs: str) -> LibraryAssertionReport:
    """Build a report whose assertions list contains the same brand_slug more than once.

    Each positional argument produces one BrandAssertion (faithful panel).  Pass
    the same slug twice to create a duplicate-slug condition.  This exercises the
    silent-dedup bug that the Phase-A hardening closes.
    """
    responses = [_brand_response(slug, faithful=True) for slug in slugs]
    return build_report(responses, source="fixture")


# ---------------------------------------------------------------------------
# Phase A - Import the module under test (will fail RED until module exists)
# ---------------------------------------------------------------------------

from app.library_reseed_verification import (  # noqa: E402
    ReconciliationDivergence,
    ReconciliationResult,
    reconcile_reports,
)


# ---------------------------------------------------------------------------
# Phase A - reconcile_reports
# ---------------------------------------------------------------------------


class TestReconcileReportsPerfectMatch:
    """Perfect match: same brands, same verdicts -> reconciled=True."""

    def test_reconciled_true(self):
        brands = [("stripe", True), ("figma", True), ("aeon", False)]
        predicted = _make_report(brands)
        actual = _make_report(brands)
        result = reconcile_reports(predicted, actual)
        assert result["reconciled"] is True

    def test_verdict_drift_empty(self):
        brands = [("stripe", True), ("aeon", False)]
        predicted = _make_report(brands)
        actual = _make_report(brands)
        result = reconcile_reports(predicted, actual)
        assert result["verdict_drift"] == []

    def test_missing_in_actual_empty(self):
        brands = [("stripe", True)]
        predicted = _make_report(brands)
        actual = _make_report(brands)
        result = reconcile_reports(predicted, actual)
        assert result["missing_in_actual"] == []

    def test_unexpected_in_actual_empty(self):
        brands = [("stripe", True)]
        predicted = _make_report(brands)
        actual = _make_report(brands)
        result = reconcile_reports(predicted, actual)
        assert result["unexpected_in_actual"] == []

    def test_counts_reported_correctly(self):
        brands = [("stripe", True), ("figma", True)]
        predicted = _make_report(brands)
        actual = _make_report(brands)
        result = reconcile_reports(predicted, actual)
        assert result["predicted_count"] == 2
        assert result["actual_count"] == 2


class TestReconcileReportsVerdictDrift:
    """Verdict drift: a brand's verdict changed between prediction and reality."""

    def _build_pair(self) -> tuple[LibraryAssertionReport, LibraryAssertionReport]:
        predicted = _make_report([("stripe", True)])   # panel_faithful
        actual = _make_report([("stripe", False)])     # panel_cleanly_absent
        return predicted, actual

    def test_reconciled_false_on_drift(self):
        predicted, actual = self._build_pair()
        result = reconcile_reports(predicted, actual)
        assert result["reconciled"] is False

    def test_verdict_drift_contains_the_brand(self):
        predicted, actual = self._build_pair()
        result = reconcile_reports(predicted, actual)
        slugs = [d["brand_slug"] for d in result["verdict_drift"]]
        assert "stripe" in slugs

    def test_divergence_carries_predicted_and_actual_verdict(self):
        predicted, actual = self._build_pair()
        result = reconcile_reports(predicted, actual)
        divergence = next(d for d in result["verdict_drift"] if d["brand_slug"] == "stripe")
        assert divergence["predicted_verdict"] == "panel_faithful"
        assert divergence["actual_verdict"] == "panel_cleanly_absent"


class TestReconcileReportsMissingBrand:
    """A brand present in predicted is absent from actual (the re-seed dropped it)."""

    def test_reconciled_false(self):
        predicted = _make_report([("stripe", True), ("figma", True)])
        actual = _make_report([("stripe", True)])   # figma missing
        result = reconcile_reports(predicted, actual)
        assert result["reconciled"] is False

    def test_missing_brand_flagged(self):
        predicted = _make_report([("stripe", True), ("figma", True)])
        actual = _make_report([("stripe", True)])
        result = reconcile_reports(predicted, actual)
        assert "figma" in result["missing_in_actual"]

    def test_present_brand_not_in_missing(self):
        predicted = _make_report([("stripe", True), ("figma", True)])
        actual = _make_report([("stripe", True)])
        result = reconcile_reports(predicted, actual)
        assert "stripe" not in result["missing_in_actual"]


class TestReconcileReportsExtraBrand:
    """A brand in actual was not in predicted (unexpected new row)."""

    def test_reconciled_false(self):
        predicted = _make_report([("stripe", True)])
        actual = _make_report([("stripe", True), ("patagonia", True)])  # extra
        result = reconcile_reports(predicted, actual)
        assert result["reconciled"] is False

    def test_extra_brand_flagged(self):
        predicted = _make_report([("stripe", True)])
        actual = _make_report([("stripe", True), ("patagonia", True)])
        result = reconcile_reports(predicted, actual)
        assert "patagonia" in result["unexpected_in_actual"]

    def test_expected_brand_not_in_unexpected(self):
        predicted = _make_report([("stripe", True)])
        actual = _make_report([("stripe", True), ("patagonia", True)])
        result = reconcile_reports(predicted, actual)
        assert "stripe" not in result["unexpected_in_actual"]


class TestReconcileReportsCountMismatch:
    """Count mismatch is surfaced explicitly even when the set-diff implies it."""

    def test_counts_reflect_reality(self):
        predicted = _make_report([("stripe", True), ("figma", True)])
        actual = _make_report([("stripe", True)])     # 1 vs 2
        result = reconcile_reports(predicted, actual)
        assert result["predicted_count"] == 2
        assert result["actual_count"] == 1

    def test_reconciled_false_when_counts_differ(self):
        predicted = _make_report([("stripe", True), ("figma", True)])
        actual = _make_report([("stripe", True)])
        result = reconcile_reports(predicted, actual)
        assert result["reconciled"] is False


class TestReconcileReportsSchemaVersionGuard:
    """Incompatible schema versions -> refuses to diff; reconciled=False."""

    def test_reconciled_false_on_version_mismatch(self):
        predicted = _make_report([("stripe", True)], schema_version="library_assertion_report_v1")
        actual = _make_report([("stripe", True)], schema_version="library_assertion_report_v2")
        result = reconcile_reports(predicted, actual)
        assert result["reconciled"] is False

    def test_notes_contains_schema_mismatch_signal(self):
        predicted = _make_report([("stripe", True)], schema_version="library_assertion_report_v1")
        actual = _make_report([("stripe", True)], schema_version="library_assertion_report_v2")
        result = reconcile_reports(predicted, actual)
        assert "schema_mismatch" in result["notes"] or "schema" in result["notes"].lower()

    def test_matching_versions_do_not_trigger_guard(self):
        brands = [("stripe", True)]
        predicted = _make_report(brands)
        actual = _make_report(brands)
        result = reconcile_reports(predicted, actual)
        # schema_mismatch should not prevent a clean match
        assert result["reconciled"] is True


class TestReconcileReportsOutputShape:
    """ReconciliationResult carries schema_version and generated_at."""

    def test_schema_version_present(self):
        brands = [("stripe", True)]
        result = reconcile_reports(_make_report(brands), _make_report(brands))
        assert result["schema_version"] == "library_reconciliation_v1"

    def test_generated_at_is_utc_iso(self):
        brands = [("stripe", True)]
        result = reconcile_reports(_make_report(brands), _make_report(brands))
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", result["generated_at"])

    def test_typeddict_keys_complete(self):
        brands = [("stripe", True)]
        result = reconcile_reports(_make_report(brands), _make_report(brands))
        required = {
            "schema_version", "generated_at", "reconciled",
            "predicted_count", "actual_count",
            "verdict_drift", "missing_in_actual", "unexpected_in_actual",
            "notes", "duplicate_in_predicted", "duplicate_in_actual",
        }
        for key in required:
            assert key in result, f"ReconciliationResult missing key: {key!r}"


# ---------------------------------------------------------------------------
# Phase A hardening - absolute schema-version guard + duplicate-slug detection
# (RED until Phase A GREEN commit hardens reconcile_reports)
# ---------------------------------------------------------------------------


class TestReconcileReportsAbsoluteSchemaVersion:
    """Both reports agree on a version that is NOT the known v1 -> reconciled=False.

    The predecessor spec only guarded relative mismatch (predicted vs actual).
    This closes the gap: two future-v2 reports would pass the relative guard and
    be read with v1 assumptions.  The absolute guard refuses them both.
    """

    def test_unknown_matching_versions_rejected(self):
        brands = [("stripe", True)]
        predicted = _make_report(brands, schema_version="library_assertion_report_v2")
        actual = _make_report(brands, schema_version="library_assertion_report_v2")
        result = reconcile_reports(predicted, actual)
        assert result["reconciled"] is False

    def test_notes_contain_schema_unknown_signal(self):
        brands = [("stripe", True)]
        predicted = _make_report(brands, schema_version="library_assertion_report_v2")
        actual = _make_report(brands, schema_version="library_assertion_report_v2")
        result = reconcile_reports(predicted, actual)
        assert "schema_unknown" in result["notes"] or "unknown" in result["notes"].lower()

    def test_known_version_constant_importable(self):
        """_KNOWN_ASSERTION_SCHEMA_VERSION must be a named constant in the module."""
        from app.library_reseed_verification import _KNOWN_ASSERTION_SCHEMA_VERSION  # noqa: PLC0415
        assert _KNOWN_ASSERTION_SCHEMA_VERSION == "library_assertion_report_v1"

    def test_known_v1_still_passes_absolute_guard(self):
        """Reports carrying library_assertion_report_v1 must still reconcile cleanly."""
        brands = [("stripe", True)]
        predicted = _make_report(brands)  # default schema_version from build_report
        actual = _make_report(brands)
        result = reconcile_reports(predicted, actual)
        assert result["reconciled"] is True


class TestReconcileReportsDuplicateSlug:
    """Duplicate brand_slug in either report -> reconciled=False + slug named.

    A DB defect or seeder bug could produce two library_pages rows for one brand,
    causing two BrandAssertion entries with the same slug.  The dict-comprehension
    verdict map silently dedupes them, so the count fields would disagree but
    ``reconciled`` would stay True.  The hardened engine detects this before
    building the maps.
    """

    def test_duplicate_in_actual_reconciled_false(self):
        predicted = _make_report([("stripe", True)])
        actual = _make_report_with_dup_slug("stripe", "stripe")
        result = reconcile_reports(predicted, actual)
        assert result["reconciled"] is False

    def test_duplicate_in_actual_slug_named(self):
        predicted = _make_report([("stripe", True)])
        actual = _make_report_with_dup_slug("stripe", "stripe")
        result = reconcile_reports(predicted, actual)
        assert "stripe" in result["duplicate_in_actual"]

    def test_duplicate_in_predicted_reconciled_false(self):
        actual = _make_report([("stripe", True)])
        predicted = _make_report_with_dup_slug("stripe", "stripe")
        result = reconcile_reports(predicted, actual)
        assert result["reconciled"] is False

    def test_duplicate_in_predicted_slug_named(self):
        actual = _make_report([("stripe", True)])
        predicted = _make_report_with_dup_slug("stripe", "stripe")
        result = reconcile_reports(predicted, actual)
        assert "stripe" in result["duplicate_in_predicted"]

    def test_clean_reports_have_empty_duplicate_lists(self):
        brands = [("stripe", True), ("figma", True)]
        predicted = _make_report(brands)
        actual = _make_report(brands)
        result = reconcile_reports(predicted, actual)
        assert result["duplicate_in_actual"] == []
        assert result["duplicate_in_predicted"] == []

    def test_multiple_dupes_all_named(self):
        """Two different slugs that each appear twice -> both in the list."""
        actual = _make_report_with_dup_slug("stripe", "stripe", "figma", "figma")
        predicted = _make_report([("stripe", True)])
        result = reconcile_reports(predicted, actual)
        assert "stripe" in result["duplicate_in_actual"]
        assert "figma" in result["duplicate_in_actual"]


# ---------------------------------------------------------------------------
# Phase B - Import gate functions (RED until Phase B symbols added to module)
# ---------------------------------------------------------------------------

from app.library_reseed_verification import (  # noqa: E402
    CeremonyGateInputs,
    CeremonyGoNoGo,
    evaluate_ceremony_gates,
)


# ---------------------------------------------------------------------------
# Phase B - evaluate_ceremony_gates
# ---------------------------------------------------------------------------


class TestEvaluateCeremonyGatesAllPass:
    """All three inputs True -> go=True, empty failed_gates."""

    def test_go_true(self):
        inputs = CeremonyGateInputs(
            backup_verified=True,
            dryrun_stable=True,
            preflight_all_pass=True,
        )
        result = evaluate_ceremony_gates(inputs)
        assert result["go"] is True

    def test_failed_gates_empty(self):
        inputs = CeremonyGateInputs(
            backup_verified=True,
            dryrun_stable=True,
            preflight_all_pass=True,
        )
        result = evaluate_ceremony_gates(inputs)
        assert result["failed_gates"] == []


class TestEvaluateCeremonyGatesSingleFailure:
    """Each single False gate -> go=False, exactly that gate named."""

    def test_backup_not_verified(self):
        inputs = CeremonyGateInputs(
            backup_verified=False,
            dryrun_stable=True,
            preflight_all_pass=True,
        )
        result = evaluate_ceremony_gates(inputs)
        assert result["go"] is False
        assert len(result["failed_gates"]) == 1
        assert any("backup" in g.lower() for g in result["failed_gates"])

    def test_dryrun_not_stable(self):
        inputs = CeremonyGateInputs(
            backup_verified=True,
            dryrun_stable=False,
            preflight_all_pass=True,
        )
        result = evaluate_ceremony_gates(inputs)
        assert result["go"] is False
        assert len(result["failed_gates"]) == 1
        assert any("dry" in g.lower() or "dryrun" in g.lower() or "dry-run" in g.lower()
                   for g in result["failed_gates"])

    def test_preflight_not_passed(self):
        inputs = CeremonyGateInputs(
            backup_verified=True,
            dryrun_stable=True,
            preflight_all_pass=False,
        )
        result = evaluate_ceremony_gates(inputs)
        assert result["go"] is False
        assert len(result["failed_gates"]) == 1
        assert any("preflight" in g.lower() for g in result["failed_gates"])


class TestEvaluateCeremonyGatesAllFail:
    """All three inputs False -> go=False, all three gates listed."""

    def test_go_false(self):
        inputs = CeremonyGateInputs(
            backup_verified=False,
            dryrun_stable=False,
            preflight_all_pass=False,
        )
        result = evaluate_ceremony_gates(inputs)
        assert result["go"] is False

    def test_all_three_gates_listed(self):
        inputs = CeremonyGateInputs(
            backup_verified=False,
            dryrun_stable=False,
            preflight_all_pass=False,
        )
        result = evaluate_ceremony_gates(inputs)
        assert len(result["failed_gates"]) == 3


class TestEvaluateCeremonyGatesOutputShape:
    """CeremonyGoNoGo carries schema_version and generated_at."""

    def _all_pass_inputs(self) -> CeremonyGateInputs:
        return CeremonyGateInputs(
            backup_verified=True,
            dryrun_stable=True,
            preflight_all_pass=True,
        )

    def test_schema_version_present(self):
        result = evaluate_ceremony_gates(self._all_pass_inputs())
        assert result["schema_version"] == "ceremony_gate_v1"

    def test_generated_at_is_utc_iso(self):
        result = evaluate_ceremony_gates(self._all_pass_inputs())
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", result["generated_at"])

    def test_typeddict_keys_complete(self):
        result = evaluate_ceremony_gates(self._all_pass_inputs())
        required = {"schema_version", "generated_at", "go", "failed_gates", "notes"}
        for key in required:
            assert key in result, f"CeremonyGoNoGo missing key: {key!r}"


# ---------------------------------------------------------------------------
# Phase B - render_reconciliation_markdown (RED until function added to engine)
# Local imports used so existing tests remain runnable during the RED window.
# ---------------------------------------------------------------------------


class TestRenderReconciliationMarkdown:
    """render_reconciliation_markdown produces a human-readable Markdown summary.

    The function lives in ``app.library_reseed_verification`` next to the engine.
    Tests use local imports so the rest of this file can still be collected when
    the function does not yet exist.
    """

    def test_function_importable(self):
        from app.library_reseed_verification import render_reconciliation_markdown  # noqa: PLC0415
        assert callable(render_reconciliation_markdown)

    def test_reconciled_verdict_present(self):
        from app.library_reseed_verification import render_reconciliation_markdown  # noqa: PLC0415
        brands = [("stripe", True)]
        result = reconcile_reports(_make_report(brands), _make_report(brands))
        md = render_reconciliation_markdown(result)
        assert "reconciled" in md.lower()
        assert "True" in md

    def test_schema_version_present(self):
        from app.library_reseed_verification import render_reconciliation_markdown  # noqa: PLC0415
        brands = [("stripe", True)]
        result = reconcile_reports(_make_report(brands), _make_report(brands))
        md = render_reconciliation_markdown(result)
        assert "library_reconciliation_v1" in md

    def test_brand_slug_present_on_verdict_drift(self):
        from app.library_reseed_verification import render_reconciliation_markdown  # noqa: PLC0415
        predicted = _make_report([("stripe", True)])
        actual = _make_report([("stripe", False)])
        result = reconcile_reports(predicted, actual)
        md = render_reconciliation_markdown(result)
        assert "stripe" in md

    def test_missing_brand_present_in_output(self):
        from app.library_reseed_verification import render_reconciliation_markdown  # noqa: PLC0415
        predicted = _make_report([("stripe", True), ("figma", True)])
        actual = _make_report([("stripe", True)])
        result = reconcile_reports(predicted, actual)
        md = render_reconciliation_markdown(result)
        assert "figma" in md

    def test_clean_result_contains_no_error_sections(self):
        from app.library_reseed_verification import render_reconciliation_markdown  # noqa: PLC0415
        brands = [("stripe", True)]
        result = reconcile_reports(_make_report(brands), _make_report(brands))
        md = render_reconciliation_markdown(result)
        assert "Verdict drift" not in md
        assert "Missing" not in md


# ---------------------------------------------------------------------------
# Phase A (this handoff) - Malformed-report minimum-shape guard
# RED until Phase A GREEN commit adds _validate_assertion_report_shape and
# _REQUIRED_ASSERTION_KEYS to the engine and wires the guard into reconcile_reports.
# ---------------------------------------------------------------------------


class TestReconcileReportsMalformedShapeGuard:
    """Wrong-shape inputs (valid JSON, not a LibraryAssertionReport) -> named failure, no crash.

    Guards the ceremony from the realistic 'operator pointed --actual at the wrong file'
    error.  After Phase A, reconcile_reports returns reconciled=False with a
    malformed_report note instead of raising KeyError or TypeError.

    Seam choice: engine-level guard (preferred per handoff Phase A design note).
    The CLI maps a notes value starting with 'malformed_report:' to exit 2.
    """

    def test_required_keys_constant_importable(self):
        """_REQUIRED_ASSERTION_KEYS must be a named frozenset constant (no drift)."""
        from app.library_reseed_verification import _REQUIRED_ASSERTION_KEYS  # noqa: PLC0415
        assert isinstance(_REQUIRED_ASSERTION_KEYS, frozenset)
        assert "brand_slug" in _REQUIRED_ASSERTION_KEYS
        assert "verdict" in _REQUIRED_ASSERTION_KEYS

    def test_missing_assertions_key_reconciled_false(self):
        """Valid JSON with no assertions key -> reconciled=False, not silent wrong result."""
        no_assertions = {
            "schema_version": "library_assertion_report_v1",
            "brand_count": 0,
        }
        well_formed = _make_report([("stripe", True)])
        result = reconcile_reports(no_assertions, well_formed)
        assert result["reconciled"] is False

    def test_missing_assertions_key_note_names_problem(self):
        """The note starts with malformed_report: and names the problem."""
        no_assertions = {
            "schema_version": "library_assertion_report_v1",
            "brand_count": 0,
        }
        well_formed = _make_report([("stripe", True)])
        result = reconcile_reports(no_assertions, well_formed)
        assert "malformed_report" in result["notes"]

    def test_missing_assertions_key_names_predicted_side(self):
        """When predicted is malformed, the note names 'predicted'."""
        bad_predicted = {
            "schema_version": "library_assertion_report_v1",
            "brand_count": 0,
        }
        well_formed = _make_report([("stripe", True)])
        result = reconcile_reports(bad_predicted, well_formed)
        assert "predicted" in result["notes"]

    def test_assertions_not_a_list_reconciled_false(self):
        """assertions is a dict instead of a list -> named failure, no crash."""
        bad_actual = {
            "schema_version": "library_assertion_report_v1",
            "brand_count": 1,
            "assertions": {"brand_slug": "stripe", "verdict": "panel_faithful"},
        }
        well_formed = _make_report([("stripe", True)])
        result = reconcile_reports(well_formed, bad_actual)
        assert result["reconciled"] is False

    def test_assertions_not_a_list_note_names_problem(self):
        """assertions is a string -> note contains malformed_report."""
        bad_actual = {
            "schema_version": "library_assertion_report_v1",
            "brand_count": 0,
            "assertions": "not-a-list",
        }
        well_formed = _make_report([("stripe", True)])
        result = reconcile_reports(well_formed, bad_actual)
        assert "malformed_report" in result["notes"]

    def test_assertions_not_a_list_names_actual_side(self):
        """When actual has non-list assertions, the note names 'actual'."""
        bad_actual = {
            "schema_version": "library_assertion_report_v1",
            "brand_count": 0,
            "assertions": "not-a-list",
        }
        well_formed = _make_report([("stripe", True)])
        result = reconcile_reports(well_formed, bad_actual)
        assert "actual" in result["notes"]

    def test_assertion_entry_missing_brand_slug(self):
        """assertions present, one entry missing brand_slug -> named failure, no crash."""
        bad_actual = {
            "schema_version": "library_assertion_report_v1",
            "brand_count": 1,
            "assertions": [{"verdict": "panel_faithful"}],
        }
        well_formed = _make_report([("stripe", True)])
        result = reconcile_reports(well_formed, bad_actual)
        assert result["reconciled"] is False
        assert "malformed_report" in result["notes"]

    def test_assertion_entry_missing_brand_slug_names_actual_side(self):
        """Note names 'actual' when actual assertions entry is missing brand_slug."""
        bad_actual = {
            "schema_version": "library_assertion_report_v1",
            "brand_count": 1,
            "assertions": [{"verdict": "panel_faithful"}],
        }
        well_formed = _make_report([("stripe", True)])
        result = reconcile_reports(well_formed, bad_actual)
        assert "actual" in result["notes"]

    def test_assertion_entry_missing_verdict(self):
        """assertions present, one entry missing verdict -> named failure, no crash."""
        bad_actual = {
            "schema_version": "library_assertion_report_v1",
            "brand_count": 1,
            "assertions": [{"brand_slug": "stripe"}],
        }
        well_formed = _make_report([("stripe", True)])
        result = reconcile_reports(well_formed, bad_actual)
        assert result["reconciled"] is False
        assert "malformed_report" in result["notes"]

    def test_malformed_predicted_with_entry_missing_verdict(self):
        """Malformed predicted (entry missing verdict) names 'predicted' in note."""
        bad_predicted = {
            "schema_version": "library_assertion_report_v1",
            "brand_count": 1,
            "assertions": [{"brand_slug": "stripe"}],
        }
        well_formed = _make_report([("stripe", True)])
        result = reconcile_reports(bad_predicted, well_formed)
        assert "malformed_report" in result["notes"]
        assert "predicted" in result["notes"]

    def test_well_formed_reports_still_reconcile(self):
        """Regression guard: well-formed reports must still pass the new shape guard."""
        brands = [("stripe", True), ("figma", True)]
        predicted = _make_report(brands)
        actual = _make_report(brands)
        result = reconcile_reports(predicted, actual)
        assert result["reconciled"] is True

    def test_well_formed_reports_no_malformed_note(self):
        """Clean result must not have a malformed_report note."""
        brands = [("stripe", True)]
        result = reconcile_reports(_make_report(brands), _make_report(brands))
        assert "malformed_report" not in result["notes"]
