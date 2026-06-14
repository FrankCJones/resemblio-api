"""Pre-launch readiness aggregator for the Resemblio Library visual-fidelity gate.

Takes a parsed gate_report.json (schema v5 or v6) and emits a single GO / NO-GO
public-launch verdict with explicit per-check reasons.

This is the first consumer born on the v6 JSON contract (Phase 13). It reads
`browser_eval_missing` - not the deprecated `unenforced_assertions` field that
predates Phase 13 - closing the consumer loop.

Usage:
    from tests.render.prelaunch_readiness import (
        load_gate_report,
        assess_public_readiness,
        render_readiness_markdown,
    )
    import pathlib

    report = load_gate_report(pathlib.Path("path/to/gate_report.json"))
    verdict = assess_public_readiness(report)
    print(render_readiness_markdown(verdict))

Schema: prelaunch_readiness_v1
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List

SCHEMA_VERSION = "prelaunch_readiness_v1"

# Gate report schema versions this aggregator accepts, mirroring the v5/v6
# compat window in the gate module. Reports below this window predate Phase
# 11 wordmark_leak enforcement (v4) and Phase 12 avatar_photo_leak enforcement
# (v5). A stale v3/v4 report cannot serve as a launch-readiness verdict.
# Added in Phase 14.2 GREEN.
SUPPORTED_GATE_SCHEMAS = frozenset(
    {
        "library_visual_fidelity_gate_report_v6",
        "library_visual_fidelity_gate_report_v5",
    }
)

# Minimum brand-x-category passes required for a GO verdict. Mirrors
# tolerance_config.yml acceptance.brand_x_category_pass_minimum.
# Used when the report's tolerance block does not carry the key.
DEFAULT_BXC_FLOOR = 3


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _as_report_count(value: Any) -> "int | None":
    """Return value if it is a genuine int count, else None.

    Used for the numeric coverage-floor fields. The gate module writes these
    via ``dataclasses.asdict`` on int-typed fields, so a correct report ALWAYS
    carries a JSON number that ``json.loads`` reads back as a Python int.
    Anything else (a string - even a numeric one like "3", a float, None) means
    the report was hand-edited or corrupted, so it is untrustworthy: return
    None and let the caller emit a NO-GO rather than silently coercing a
    suspect value or letting a TypeError/ValueError escape.

    Booleans are rejected explicitly: ``True``/``False`` are ints in Python,
    but a bool in a count field is itself a sign of a corrupt report. This
    strictness is deliberate - the module is the last gate before an
    irreversible public launch, where distrust of a malformed input is the
    safe default and there is zero false-positive risk on a genuine
    machine-generated report.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


# ---------------------------------------------------------------------------
# Public data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadinessReason:
    """One pass/fail line in the readiness verdict.

    `check` is the stable machine id (e.g. "trademark_clean"). `ok` is the
    gate outcome for this check. `detail` is the human-readable string the
    operator reads (e.g. "0 wordmark leaks across 6 tuples").

    Hard checks: `ok=False` propagates to `ReadinessVerdict.go=False`.
    Soft checks: `ok` is always True, but `detail` surfaces a warning count
    so the operator can see it and decide whether action is needed.
    """

    check: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ReadinessVerdict:
    """Aggregate public-launch readiness verdict over one gate report.

    `go` is True iff EVERY hard check passed. The verdict is deliberately
    conservative: any single hard failure -> NO-GO. `reasons` carries every
    check (both passed and failed) so the operator sees the full picture, not
    just the first failure.

    `gate_report_schema` records the schema_version of the assessed report so
    a human can cross-reference the verdict against the underlying report file.
    """

    schema_version: str  # "prelaunch_readiness_v1"
    gate_report_schema: str
    generated_at_utc: str
    go: bool
    reasons: List[ReadinessReason]


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------


def load_gate_report(path: pathlib.Path) -> Dict[str, Any]:
    """Load and parse a gate_report.json from disk.

    Raises:
        FileNotFoundError: if the path does not exist.
        ValueError: if the file contains invalid JSON, with a message naming
            the path and the parse error.

    Does not validate the report's schema or field set; that is
    `assess_public_readiness`'s responsibility.
    """
    if not path.exists():
        raise FileNotFoundError(f"Gate report not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in gate report {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Core assessment
# ---------------------------------------------------------------------------


def assess_public_readiness(report: Dict[str, Any]) -> ReadinessVerdict:
    """Assess a parsed gate report for public-launch readiness.

    Returns a ReadinessVerdict with `go=True` iff ALL hard checks pass.
    Never raises; a malformed report results in a NO-GO verdict with an
    explanatory reason rather than an uncaught exception.

    Hard checks (any failure -> go=False):

    1. schema_supported  [added in Phase 14.2 GREEN]
       The report's schema_version must be in SUPPORTED_GATE_SCHEMAS (v5 or
       v6). A stale v3/v4 report predates Phase 11/12 enforcement and can
       never serve as a launch-readiness verdict.

    2. trademark_clean
       Zero tuples carry "wordmark_leak" in drift_dimensions.

    3. pii_clean
       Zero tuples carry "avatar_photo_leak" in drift_dimensions.

    4. coverage_floor_met
       brand_x_category_passes >= bxc floor.

    5. aggregate_pass
       The report's own aggregate field equals "PASS".

    Soft checks (never block go; appear in reasons with ok=True):

    6. browser_eval_complete
       Total browser_eval_missing count across all tuples. Missing != leak.
       An assertion the browser evaluator could not run does not produce a
       leak signal. The count is surfaced for operator visibility; it does NOT
       block launch because no observed leak means no observed safety failure.
    """
    report_schema = report.get("schema_version", "<missing>")
    generated_at = datetime.now(tz=timezone.utc).isoformat()

    reasons: List[ReadinessReason] = []
    hard_failures: List[str] = []

    # --- Hard check 1: schema_supported ---
    # This is the guard the Gate 13 audit identified as missing: the v3 report
    # on disk carried aggregate=PASS, but that verdict was computed before
    # wordmark_leak (Phase 11) and avatar_photo_leak (Phase 12) enforcement
    # existed. A schema below v5 is a stale pre-enforcement report and CANNOT
    # serve as a launch-readiness verdict regardless of its aggregate field.
    if report_schema in SUPPORTED_GATE_SCHEMAS:
        reasons.append(
            ReadinessReason(
                check="schema_supported",
                ok=True,
                detail=f"report schema {report_schema} is in the supported v5/v6 window",
            )
        )
    else:
        detail = (
            f"report schema '{report_schema}' is not in the supported window "
            f"{{v5, v6}}. This report predates Phase 11/12 enforcement "
            f"(wordmark_leak + avatar_photo_leak) and cannot be used as a "
            f"launch-readiness verdict. Run the live v6 gate and assess the fresh report."
        )
        reasons.append(ReadinessReason(check="schema_supported", ok=False, detail=detail))
        hard_failures.append("schema_supported")

    # --- Hard check 2: trademark_clean ---
    tuples: List[Dict[str, Any]] = report.get("tuples", [])
    wordmark_leakers: List[str] = []
    photo_leakers: List[str] = []
    browser_missing_total = 0

    for t in tuples:
        tid = t.get("tuple_id", "?")
        dims: List[str] = t.get("drift_dimensions", [])
        if "wordmark_leak" in dims:
            wordmark_leakers.append(tid)
        if "avatar_photo_leak" in dims:
            photo_leakers.append(tid)
        browser_missing_total += len(t.get("browser_eval_missing", []))

    if not wordmark_leakers:
        reasons.append(
            ReadinessReason(
                check="trademark_clean",
                ok=True,
                detail=f"0 wordmark leaks across {len(tuples)} tuples",
            )
        )
    else:
        detail = (
            f"{len(wordmark_leakers)} tuple(s) leak the wordmark: "
            + ", ".join(wordmark_leakers)
        )
        reasons.append(ReadinessReason(check="trademark_clean", ok=False, detail=detail))
        hard_failures.append("trademark_clean")

    # --- Hard check 3: pii_clean ---
    if not photo_leakers:
        reasons.append(
            ReadinessReason(
                check="pii_clean",
                ok=True,
                detail=f"0 avatar-photo leaks across {len(tuples)} tuples",
            )
        )
    else:
        detail = (
            f"{len(photo_leakers)} tuple(s) leak avatar photos: "
            + ", ".join(photo_leakers)
        )
        reasons.append(ReadinessReason(check="pii_clean", ok=False, detail=detail))
        hard_failures.append("pii_clean")

    # --- Hard check 4: coverage_floor_met ---
    # Both the floor and the actual count come from a (usually machine-
    # generated) JSON report, but a hand-edited or partially-corrupt report
    # during the launch ceremony could carry a non-numeric value. Coerce
    # defensively: a value that will not parse as an int is itself a NO-GO
    # signal (the report is untrustworthy), not a crash. This keeps the
    # module's "never raises" contract real - critical for a tool that is the
    # last gate before Frank's irreversible Phase 7 CTA flip.
    tolerance: Dict[str, Any] = report.get("tolerance", {})
    if "brand_x_category_pass_minimum" in tolerance:
        bxc_floor = _as_report_count(tolerance["brand_x_category_pass_minimum"])
        floor_source = f"tolerance block (brand_x_category_pass_minimum={bxc_floor})"
    else:
        bxc_floor = DEFAULT_BXC_FLOOR
        floor_source = f"default constant (DEFAULT_BXC_FLOOR={bxc_floor})"

    bxc_actual = _as_report_count(report.get("brand_x_category_passes", 0))

    if bxc_floor is None or bxc_actual is None:
        detail = (
            "could not assess coverage floor: a numeric field is malformed "
            f"(brand_x_category_passes={report.get('brand_x_category_passes')!r}, "
            f"tolerance.brand_x_category_pass_minimum="
            f"{tolerance.get('brand_x_category_pass_minimum')!r}). "
            "A non-numeric value means the report is untrustworthy -> NO-GO."
        )
        reasons.append(ReadinessReason(check="coverage_floor_met", ok=False, detail=detail))
        hard_failures.append("coverage_floor_met")
    elif bxc_actual >= bxc_floor:
        reasons.append(
            ReadinessReason(
                check="coverage_floor_met",
                ok=True,
                detail=f"brand_x_category_passes={bxc_actual} >= floor={bxc_floor} (from {floor_source})",
            )
        )
    else:
        detail = (
            f"brand_x_category_passes={bxc_actual} < floor={bxc_floor} "
            f"(from {floor_source})"
        )
        reasons.append(ReadinessReason(check="coverage_floor_met", ok=False, detail=detail))
        hard_failures.append("coverage_floor_met")

    # --- Hard check 5: aggregate_pass ---
    aggregate = report.get("aggregate", "<missing>")
    if aggregate == "PASS":
        reasons.append(
            ReadinessReason(
                check="aggregate_pass",
                ok=True,
                detail="gate aggregate is PASS",
            )
        )
    else:
        detail = f"gate aggregate is '{aggregate}' (expected PASS)"
        reasons.append(ReadinessReason(check="aggregate_pass", ok=False, detail=detail))
        hard_failures.append("aggregate_pass")

    # --- Soft check 6: browser_eval_complete ---
    if browser_missing_total == 0:
        browser_detail = "0 browser_eval_missing across all tuples (all browser evals completed)"
    else:
        browser_detail = (
            f"{browser_missing_total} browser_eval_missing assertion(s) across "
            f"{len(tuples)} tuples. Missing != leak; the evaluator could not run "
            f"these assertions but found no positive leak signal. Re-run the gate "
            f"for a cleaner report; this count does not block launch."
        )
    reasons.append(
        ReadinessReason(check="browser_eval_complete", ok=True, detail=browser_detail)
    )

    go = len(hard_failures) == 0
    return ReadinessVerdict(
        schema_version=SCHEMA_VERSION,
        gate_report_schema=report_schema,
        generated_at_utc=generated_at,
        go=go,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_readiness_markdown(verdict: ReadinessVerdict) -> str:
    """Render a ReadinessVerdict as a human-readable Markdown string.

    The output carries a top-line GO or NO-GO headline, metadata about the
    assessed report, and one bullet per reason with its check id and detail.
    Designed to be pasted directly into STATUS.md or the PRD.

    A GO headline looks like:
        ## Readiness verdict: GO

    A NO-GO headline looks like:
        ## Readiness verdict: NO-GO
    """
    lines: List[str] = []
    verdict_label = "GO" if verdict.go else "NO-GO"
    lines.append(f"## Readiness verdict: {verdict_label}")
    lines.append("")
    lines.append(f"- Generated: {verdict.generated_at_utc}")
    lines.append(f"- Gate report schema assessed: {verdict.gate_report_schema}")
    lines.append(f"- Aggregator schema: {verdict.schema_version}")
    lines.append("")
    lines.append("### Checks")
    lines.append("")

    for r in verdict.reasons:
        status_icon = "PASS" if r.ok else "FAIL"
        lines.append(f"- [{status_icon}] `{r.check}`: {r.detail}")

    lines.append("")
    if verdict.go:
        lines.append(
            "**All hard checks passed. Library is ready for Phase 7 (homepage CTA flip) - "
            "Frank's irreversible gate.**"
        )
    else:
        failing = [r.check for r in verdict.reasons if not r.ok]
        lines.append(
            f"**NO-GO. Failing checks: {', '.join(failing)}. "
            f"Resolve before Frank's Phase 7 flip.**"
        )

    return "\n".join(lines)
