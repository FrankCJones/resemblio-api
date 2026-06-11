"""Re-seed verification engine for the Resemblio Library.

Purpose
-------
Two pure functions that prove the live post-re-seed assertion report matches
the offline preflight prediction, and encode the pre-apply ceremony gate
chain as a testable, auditable record.

``reconcile_reports``
    Diffs two ``LibraryAssertionReport`` instances (predicted vs actual) and
    returns a ``ReconciliationResult`` naming every class of divergence.
    Called in Phase D after the re-seed runs to prove the live library matches
    what preflight predicted offline.

``evaluate_ceremony_gates``
    Encodes the three pre-apply gates as a single tested decision.  Returns a
    ``CeremonyGoNoGo`` that names which gate(s) failed so a future agent
    cannot quietly skip one before a prod mutation.

Design notes
------------
- Both functions are pure: no DB, no network, no filesystem access.
- Outputs are schema-versioned TypedDicts so downstream consumers can detect
  incompatible shapes.
- ``LibraryAssertionReport`` is imported from ``app.library_assertion_report``;
  do NOT redefine it here.
- Named constants for gate labels prevent bare-string comparisons from silently
  drifting out of sync.

Dependencies
------------
- ``app.library_assertion_report.LibraryAssertionReport`` (data shape)
- stdlib only (datetime, typing)

Run the tests (from ``code/api/``):
    python -m pytest tests/test_library_reseed_verification.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TypedDict

from app.library_assertion_report import LibraryAssertionReport

# ---------------------------------------------------------------------------
# TypedDicts - reconciliation (Phase A)
# ---------------------------------------------------------------------------


class ReconciliationDivergence(TypedDict):
    """A single brand whose verdict changed between prediction and reality.

    Fields
    ------
    brand_slug:
        The brand that drifted.
    predicted_verdict:
        The verdict the offline preflight assigned to this brand.
    actual_verdict:
        The verdict the live post-re-seed report assigned to this brand.
    """

    brand_slug: str
    predicted_verdict: str
    actual_verdict: str


class ReconciliationResult(TypedDict):
    """Schema-versioned diff of two ``LibraryAssertionReport`` instances.

    Fields
    ------
    schema_version:
        Fixed string ``"library_reconciliation_v1"``.  Bump to v2 when the
        shape changes so downstream consumers can detect incompatible results.
    generated_at:
        UTC ISO-8601 timestamp of reconciliation.
    reconciled:
        ``True`` iff schema versions match AND no verdict drift AND no missing
        brands AND no unexpected brands.
    predicted_count:
        Brand count from the predicted (preflight) report.
    actual_count:
        Brand count from the actual (live) report.
    verdict_drift:
        Brands whose verdict changed between prediction and reality.
    missing_in_actual:
        Brand slugs present in predicted but absent from actual (the re-seed
        dropped them).
    unexpected_in_actual:
        Brand slugs present in actual but absent from predicted (unexpected new
        rows).
    notes:
        Free-text explanation when ``reconciled=False`` for the schema-mismatch
        case; empty string otherwise.
    """

    schema_version: str
    generated_at: str
    reconciled: bool
    predicted_count: int
    actual_count: int
    verdict_drift: list[ReconciliationDivergence]
    missing_in_actual: list[str]
    unexpected_in_actual: list[str]
    notes: str


# ---------------------------------------------------------------------------
# reconcile_reports - Phase A core function
# ---------------------------------------------------------------------------


def reconcile_reports(
    predicted: LibraryAssertionReport,
    actual: LibraryAssertionReport,
) -> ReconciliationResult:
    """Diff a predicted (offline preflight) report against an actual (live) report.

    Both reports must share ``schema_version``.  If they differ the function
    refuses to compare them and returns ``reconciled=False`` with a
    ``schema_mismatch`` note.

    Reconciliation is a boolean AND of four conditions:
    1. Schema versions match.
    2. No verdict drift on any brand in the intersection of both slug sets.
    3. No brands present in predicted but absent from actual.
    4. No brands present in actual but absent from predicted.

    Count mismatch (``predicted_count != actual_count``) is reported explicitly
    via the ``predicted_count`` / ``actual_count`` fields even when the set-diff
    already implies it - this is a deliberate redundant signal.

    Parameters
    ----------
    predicted:
        The ``LibraryAssertionReport`` produced by the offline preflight before
        ``seed_from_drl --apply`` ran.
    actual:
        The ``LibraryAssertionReport`` produced by running the live assertion
        CLI (``generate_library_assertion_report.py``) after the re-seed.

    Returns
    -------
    ReconciliationResult
        A schema-versioned record describing any divergences.
    """
    now = datetime.now(tz=timezone.utc).isoformat()

    # Schema-version guard: refuse to diff incompatible shapes.
    if predicted.get("schema_version") != actual.get("schema_version"):
        return ReconciliationResult(
            schema_version="library_reconciliation_v1",
            generated_at=now,
            reconciled=False,
            predicted_count=predicted.get("brand_count", 0),
            actual_count=actual.get("brand_count", 0),
            verdict_drift=[],
            missing_in_actual=[],
            unexpected_in_actual=[],
            notes=(
                f"schema_mismatch: predicted={predicted.get('schema_version')!r} "
                f"vs actual={actual.get('schema_version')!r} - cannot diff incompatible shapes"
            ),
        )

    # Build per-brand verdict maps keyed by brand_slug.
    predicted_verdicts: dict[str, str] = {
        a["brand_slug"]: a["verdict"] for a in predicted.get("assertions", [])
    }
    actual_verdicts: dict[str, str] = {
        a["brand_slug"]: a["verdict"] for a in actual.get("assertions", [])
    }

    predicted_slugs = set(predicted_verdicts)
    actual_slugs = set(actual_verdicts)

    # Set differences.
    missing_in_actual = sorted(predicted_slugs - actual_slugs)
    unexpected_in_actual = sorted(actual_slugs - predicted_slugs)

    # Verdict drift on the intersection.
    verdict_drift: list[ReconciliationDivergence] = []
    for slug in sorted(predicted_slugs & actual_slugs):
        p_verdict = predicted_verdicts[slug]
        a_verdict = actual_verdicts[slug]
        if p_verdict != a_verdict:
            verdict_drift.append(
                ReconciliationDivergence(
                    brand_slug=slug,
                    predicted_verdict=p_verdict,
                    actual_verdict=a_verdict,
                )
            )

    reconciled = (
        not verdict_drift
        and not missing_in_actual
        and not unexpected_in_actual
    )

    return ReconciliationResult(
        schema_version="library_reconciliation_v1",
        generated_at=now,
        reconciled=reconciled,
        predicted_count=predicted.get("brand_count", 0),
        actual_count=actual.get("brand_count", 0),
        verdict_drift=verdict_drift,
        missing_in_actual=missing_in_actual,
        unexpected_in_actual=unexpected_in_actual,
        notes="",
    )
