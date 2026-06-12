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

from collections import Counter
from datetime import datetime, timezone
from typing import TypedDict

from app.library_assertion_report import (
    LIBRARY_ASSERTION_SCHEMA_VERSION as _KNOWN_ASSERTION_SCHEMA_VERSION,
    LibraryAssertionReport,
)

# ---------------------------------------------------------------------------
# Gate label constants (Phase B)
# ---------------------------------------------------------------------------

#: Human-readable label for the backup-verified gate.
_GATE_BACKUP_VERIFIED: str = "backup_verified"

#: Human-readable label for the dry-run-stable gate.
_GATE_DRYRUN_STABLE: str = "dryrun_stable"

#: Human-readable label for the preflight-all-pass gate.
_GATE_PREFLIGHT_ALL_PASS: str = "preflight_all_pass"

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
        ``True`` iff schema versions match AND the version is the known v1 AND
        no verdict drift AND no missing brands AND no unexpected brands AND no
        duplicate brand_slugs in either report.
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
    duplicate_in_predicted:
        Brand slugs that appear more than once in the predicted report's
        assertions list (sorted, deduplicated).  Non-empty -> ``reconciled=False``.
    duplicate_in_actual:
        Brand slugs that appear more than once in the actual report's assertions
        list (sorted, deduplicated).  Non-empty -> ``reconciled=False``.
    notes:
        Free-text explanation when ``reconciled=False`` for the schema-mismatch
        or schema-unknown cases; empty string otherwise.
    """

    schema_version: str
    generated_at: str
    reconciled: bool
    predicted_count: int
    actual_count: int
    verdict_drift: list[ReconciliationDivergence]
    missing_in_actual: list[str]
    unexpected_in_actual: list[str]
    duplicate_in_predicted: list[str]
    duplicate_in_actual: list[str]
    notes: str


# ---------------------------------------------------------------------------
# reconcile_reports - Phase A core function
# ---------------------------------------------------------------------------


def reconcile_reports(
    predicted: LibraryAssertionReport,
    actual: LibraryAssertionReport,
) -> ReconciliationResult:
    """Diff a predicted (offline preflight) report against an actual (live) report.

    Two schema-version guards run before any diffing:
    - Relative guard: the two reports must share the same ``schema_version``.
      If they differ the function refuses to compare them and returns
      ``reconciled=False`` with a ``schema_mismatch`` note.
    - Absolute guard: the shared version must equal the known
      ``library_assertion_report_v1`` this reconciler is written against.  Two
      future-v2 reports would pass the relative guard yet be mis-read with v1
      assumptions; the absolute guard returns ``reconciled=False`` with a
      ``schema_unknown`` note instead.

    Reconciliation is a boolean AND of seven conditions:
    1. Schema versions of the two reports match each other (relative guard).
    2. The matching version equals the known ``library_assertion_report_v1``
       (absolute guard - prevents silent mis-read of future report shapes).
    3. No duplicate ``brand_slug`` values in the predicted assertions list.
    4. No duplicate ``brand_slug`` values in the actual assertions list.
    5. No verdict drift on any brand in the intersection of both slug sets.
    6. No brands present in predicted but absent from actual.
    7. No brands present in actual but absent from predicted.

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

    # Relative schema-version guard: refuse to diff incompatible shapes.
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
            duplicate_in_predicted=[],
            duplicate_in_actual=[],
            notes=(
                f"schema_mismatch: predicted={predicted.get('schema_version')!r} "
                f"vs actual={actual.get('schema_version')!r} - cannot diff incompatible shapes"
            ),
        )

    # Absolute schema-version guard: refuse to apply v1 logic to an unknown shape.
    # Two future-v2 reports would pass the relative guard above and then be read
    # with v1 assumptions; this guard catches that case.
    matched_version = predicted.get("schema_version")
    if matched_version != _KNOWN_ASSERTION_SCHEMA_VERSION:
        return ReconciliationResult(
            schema_version="library_reconciliation_v1",
            generated_at=now,
            reconciled=False,
            predicted_count=predicted.get("brand_count", 0),
            actual_count=actual.get("brand_count", 0),
            verdict_drift=[],
            missing_in_actual=[],
            unexpected_in_actual=[],
            duplicate_in_predicted=[],
            duplicate_in_actual=[],
            notes=(
                f"schema_unknown: both reports carry version {matched_version!r} "
                f"which this reconciler does not know how to read "
                f"(expected {_KNOWN_ASSERTION_SCHEMA_VERSION!r})"
            ),
        )

    # Detect duplicates BEFORE building the verdict maps.  A DB defect could
    # produce two library_pages rows for one brand, giving two BrandAssertion
    # entries with the same slug.  The dict comprehension below would silently
    # collapse them; detect first so the caller sees the real defect.
    predicted_assertions = predicted.get("assertions", [])
    actual_assertions = actual.get("assertions", [])

    predicted_slug_counts: Counter[str] = Counter(
        a["brand_slug"] for a in predicted_assertions
    )
    actual_slug_counts: Counter[str] = Counter(
        a["brand_slug"] for a in actual_assertions
    )

    duplicate_in_predicted = sorted(
        slug for slug, count in predicted_slug_counts.items() if count > 1
    )
    duplicate_in_actual = sorted(
        slug for slug, count in actual_slug_counts.items() if count > 1
    )

    # Build per-brand verdict maps keyed by brand_slug.
    # Note: dict comprehension silently dedupes; duplicates are already detected above.
    predicted_verdicts: dict[str, str] = {
        a["brand_slug"]: a["verdict"] for a in predicted_assertions
    }
    actual_verdicts: dict[str, str] = {
        a["brand_slug"]: a["verdict"] for a in actual_assertions
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
        not duplicate_in_predicted
        and not duplicate_in_actual
        and not verdict_drift
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
        duplicate_in_predicted=duplicate_in_predicted,
        duplicate_in_actual=duplicate_in_actual,
        notes="",
    )


# ---------------------------------------------------------------------------
# TypedDicts - ceremony gate (Phase B)
# ---------------------------------------------------------------------------


class CeremonyGateInputs(TypedDict):
    """Inputs to the pre-apply ceremony gate chain.

    Fields
    ------
    backup_verified:
        ``True`` when the pre-op ``pg_dump`` was verified clean via
        ``pg_restore --list``.
    dryrun_stable:
        ``True`` when two sequential dry-runs of ``seed_from_drl`` produced
        identical summary diffs (idempotency confirmed).
    preflight_all_pass:
        ``True`` when ``preflight_corpus`` returned a ``LibraryAssertionReport``
        with ``all_pass: True``.
    """

    backup_verified: bool
    dryrun_stable: bool
    preflight_all_pass: bool


class CeremonyGoNoGo(TypedDict):
    """Schema-versioned go/no-go record for the re-seed apply step.

    Fields
    ------
    schema_version:
        Fixed string ``"ceremony_gate_v1"``.
    generated_at:
        UTC ISO-8601 timestamp.
    go:
        ``True`` iff all three gate inputs are ``True``.
    failed_gates:
        List of human-readable gate labels that were ``False``.  Empty when
        ``go=True``.
    notes:
        Free-text context; empty string when ``go=True``.
    """

    schema_version: str
    generated_at: str
    go: bool
    failed_gates: list[str]
    notes: str


# ---------------------------------------------------------------------------
# evaluate_ceremony_gates - Phase B core function
# ---------------------------------------------------------------------------


def evaluate_ceremony_gates(inputs: CeremonyGateInputs) -> CeremonyGoNoGo:
    """Evaluate the three pre-apply ceremony gates and return a go/no-go record.

    This is a pure, tested decision function.  Its value is not the boolean AND
    logic (trivial) but the named-gate output: a future agent cannot silently
    skip a gate because the failure record explicitly names which gate failed.

    Gates (all must be True for ``go=True``):
    1. ``backup_verified`` - pre-op pg_dump verified clean.
    2. ``dryrun_stable``   - two sequential dry-runs produced identical diffs.
    3. ``preflight_all_pass`` - offline preflight corpus ``all_pass: True``.

    Parameters
    ----------
    inputs:
        Boolean values for each of the three gates.

    Returns
    -------
    CeremonyGoNoGo
        Schema-versioned go/no-go record.
    """
    now = datetime.now(tz=timezone.utc).isoformat()

    gate_map: list[tuple[str, bool]] = [
        (_GATE_BACKUP_VERIFIED, inputs["backup_verified"]),
        (_GATE_DRYRUN_STABLE, inputs["dryrun_stable"]),
        (_GATE_PREFLIGHT_ALL_PASS, inputs["preflight_all_pass"]),
    ]

    failed_gates = [label for label, passed in gate_map if not passed]
    go = len(failed_gates) == 0

    notes = (
        ""
        if go
        else f"Failed gates: {', '.join(failed_gates)}"
    )

    return CeremonyGoNoGo(
        schema_version="ceremony_gate_v1",
        generated_at=now,
        go=go,
        failed_gates=failed_gates,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# render_reconciliation_markdown - human-readable summary of ReconciliationResult
# ---------------------------------------------------------------------------


def render_reconciliation_markdown(result: ReconciliationResult) -> str:
    """Render a ``ReconciliationResult`` as a Markdown contact-sheet summary.

    Mirrors ``app.library_assertion_report.render_markdown`` in purpose: produces
    a human-readable string suitable for pasting into STATUS.md or a PR description.
    Only sections that carry data are emitted (verdict drift, missing, unexpected,
    and duplicate lists are omitted when empty so a clean result stays terse).

    Parameters
    ----------
    result:
        A completed ``ReconciliationResult`` as returned by ``reconcile_reports``.

    Returns
    -------
    str
        Markdown-formatted reconciliation summary.
    """
    lines: list[str] = [
        "# Library Reconciliation Report",
        "",
        f"**schema_version:** {result['schema_version']}",
        f"**generated_at:** {result['generated_at']}",
        f"**reconciled:** {result['reconciled']}",
        f"**predicted_count:** {result['predicted_count']}",
        f"**actual_count:** {result['actual_count']}",
    ]

    if result.get("notes"):
        lines += ["", f"**note:** {result['notes']}"]

    if result["verdict_drift"]:
        lines += [
            "",
            "## Verdict drift",
            "",
            "| brand_slug | predicted_verdict | actual_verdict |",
            "|---|---|---|",
        ]
        for d in result["verdict_drift"]:
            lines.append(
                f"| {d['brand_slug']} | {d['predicted_verdict']} | {d['actual_verdict']} |"
            )

    if result["missing_in_actual"]:
        lines += ["", "## Missing in actual", ""]
        for slug in result["missing_in_actual"]:
            lines.append(f"- {slug}")

    if result["unexpected_in_actual"]:
        lines += ["", "## Unexpected in actual", ""]
        for slug in result["unexpected_in_actual"]:
            lines.append(f"- {slug}")

    if result["duplicate_in_predicted"]:
        lines += ["", "## Duplicate slugs in predicted", ""]
        for slug in result["duplicate_in_predicted"]:
            lines.append(f"- {slug}")

    if result["duplicate_in_actual"]:
        lines += ["", "## Duplicate slugs in actual", ""]
        for slug in result["duplicate_in_actual"]:
            lines.append(f"- {slug}")

    return "\n".join(lines)
