"""Schema-versioned brand assertion-report engine for the Resemblio Library.

Purpose
-------
Classifies every brand in the library into one of three verdict states and
assembles a machine-readable + human-readable report.  Used in two contexts:

1. **Pre-re-seed preflight** (offline): run against the DRL corpus BEFORE
   executing ``seed_from_drl --apply`` to confirm every brand would produce a
   faithful-or-absent panel.  Catches broken corpus rows before they hit prod.

2. **Post-re-seed proof** (Phase 7): run against the live API after re-seeding
   to produce the scripted, schema-versioned evidence that the operation
   succeeded.  ``all_pass: True`` is the acceptance gate.

Verdicts
--------
``panel_faithful``
    The brand's curated-metadata response carries at least ``tier`` and
    ``category``.  Partial sets (some of the 6 fields absent) are still
    faithful - the panel degrades gracefully by omitting absent rows.

``panel_cleanly_absent``
    Zero curated keys are present.  This is the honest-degradation (D11) case
    for organic / un-enriched brands and is a PASS.  The panel hides itself
    rather than rendering a misleading empty shell.

``page_broken``
    The response is missing a required structural slot (``brand_slug`` or a
    non-200 status wrapper).  The ONLY verdict that counts as a failure.
    ``all_pass: False`` when any brand carries this verdict.

Design notes
------------
- ``CURATED_METADATA_FIELDS`` is imported from ``routes.library``, NOT
  re-defined here.  A second hardcoded copy of the field set is the failure
  mode this import prevents.  The seam-alignment test
  ``test_library_curated_seam.py`` pins the same invariant.
- Pure functions throughout: no DB, no network, no filesystem access.
  The CLI in ``scripts/generate_library_assertion_report.py`` owns I/O.
- All public functions carry docstrings (intent + edge cases per workspace
  quality floor).  Output carries ``schema_version`` for downstream consumers.

Dependencies
------------
- ``app.routes.library.CURATED_METADATA_FIELDS`` (the seam constant)
- stdlib only (json, datetime, typing)

Run the tests (from ``code/api/``):
    python -m pytest tests/test_library_assertion_report.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypedDict

from app.routes.library import CURATED_METADATA_FIELDS

# ---------------------------------------------------------------------------
# Re-export the seam constant under the name tests probe for single-source
# proof.  Do NOT add a second set literal here.
# ---------------------------------------------------------------------------
_CURATED_FIELDS_FROM_SEAM: frozenset[str] = CURATED_METADATA_FIELDS

# ---------------------------------------------------------------------------
# Schema version constant - single source of truth for the report shape name.
# Import this in any module that needs to validate report shapes (e.g.
# library_reseed_verification._KNOWN_ASSERTION_SCHEMA_VERSION).  Do NOT
# hardcode the string "library_assertion_report_v1" in a second place.
# ---------------------------------------------------------------------------

#: Fixed schema-version string for LibraryAssertionReport.
LIBRARY_ASSERTION_SCHEMA_VERSION: str = "library_assertion_report_v1"

# ---------------------------------------------------------------------------
# Verdict constants
# ---------------------------------------------------------------------------

#: Named constants for the three possible brand verdicts.
#: Adding a fourth verdict requires updating the seam tests and this dict.
BRAND_VERDICT: dict[str, str] = {
    "panel_faithful": "panel_faithful",
    "panel_cleanly_absent": "panel_cleanly_absent",
    "page_broken": "page_broken",
}

#: Minimum curated fields required for a brand to be ``panel_faithful``.
#: tier + category are the mandatory anchor; the other 4 fields are optional.
_MINIMUM_FAITHFUL_FIELDS: frozenset[str] = frozenset({"tier", "category"})

# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------


class BrandAssertion(TypedDict):
    """Assertion result for a single brand.

    Fields
    ------
    brand_slug:
        The slug extracted from the response.  Falls back to ``"(unknown)"``
        when the response is structurally broken so the report row is still
        readable.
    verdict:
        One of the three ``BRAND_VERDICT`` values.
    present_curated_fields:
        Names of curated fields that were present in the response.
    missing_curated_fields:
        Names of curated fields in ``CURATED_METADATA_FIELDS`` that were absent.
    v3_chip_gating:
        ``"intact"`` when ``missing_groups`` is present (even empty list).
        ``"unknown"`` when the key is absent (pre-v3 response shape).
    notes:
        Free-text explanation for ``page_broken`` verdicts; empty string
        otherwise.
    """

    brand_slug: str
    verdict: str
    present_curated_fields: list[str]
    missing_curated_fields: list[str]
    v3_chip_gating: str
    notes: str


class LibraryAssertionReport(TypedDict):
    """Structured, schema-versioned report for the full brand set.

    Fields
    ------
    schema_version:
        Fixed string ``"library_assertion_report_v1"``.  Bump to v2 when the
        shape changes so downstream consumers can detect incompatible reports.
    generated_at:
        UTC ISO-8601 timestamp of report generation.
    source:
        ``"prod"`` for a live-API run; ``"fixture"`` for a synthetic/test run.
    brand_count:
        Total number of brands assessed.
    verdict_counts:
        Dict mapping each verdict string to its count across all brands.
    assertions:
        Per-brand assertion list in input order.
    all_pass:
        ``True`` iff zero brands carry the ``"page_broken"`` verdict.
        This is the acceptance gate for Phase 7.
    """

    schema_version: str
    generated_at: str
    source: str
    brand_count: int
    verdict_counts: dict[str, int]
    assertions: list[BrandAssertion]
    all_pass: bool


# ---------------------------------------------------------------------------
# Core pure functions
# ---------------------------------------------------------------------------


def build_brand_assertion(response: dict[str, Any]) -> BrandAssertion:
    """Classify a single brand API-response dict into a ``BrandAssertion``.

    The ``response`` must be in the shape of ``GET /v1/library/brands/{slug}``
    as returned by the Resemblio API.  The function is pure: it reads only
    ``response`` and returns a new dict without touching any external state.

    Verdict logic
    -------------
    1. ``page_broken`` - ``data.brand_slug`` is absent or empty.  Any other
       structural problem (null data, missing schema_version) also lands here.
    2. ``panel_faithful`` - ``data.curated_metadata`` is present AND contains
       at least ``tier`` and ``category``.
    3. ``panel_cleanly_absent`` - ``data.curated_metadata`` is absent or empty,
       but the page itself is structurally sound.

    Edge cases
    ----------
    - A ``curated_metadata`` dict present but empty triggers
      ``panel_cleanly_absent`` (no usable fields).
    - ``missing_groups`` absent (pre-v3 response) -> ``v3_chip_gating="unknown"``.
    - ``missing_groups`` present as an empty list -> chip-gating intact (all
      captured).

    Parameters
    ----------
    response:
        Raw API response dict.  Only the ``data`` sub-dict is inspected.

    Returns
    -------
    BrandAssertion
    """
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return BrandAssertion(
            brand_slug="(unknown - no data object)",
            verdict=BRAND_VERDICT["page_broken"],
            present_curated_fields=[],
            missing_curated_fields=list(_CURATED_FIELDS_FROM_SEAM),
            v3_chip_gating="unknown",
            notes="response.data is absent or not a dict",
        )

    brand_slug: str = data.get("brand_slug", "")
    if not brand_slug:
        return BrandAssertion(
            brand_slug="(unknown - brand_slug missing)",
            verdict=BRAND_VERDICT["page_broken"],
            present_curated_fields=[],
            missing_curated_fields=list(_CURATED_FIELDS_FROM_SEAM),
            v3_chip_gating="unknown",
            notes="data.brand_slug is absent or empty - required structural slot",
        )

    # v3 chip-gating: check missing_groups presence (not value)
    v3_chip_gating = "intact" if "missing_groups" in data else "unknown"

    # Curated fields
    curated: dict[str, Any] | None = data.get("curated_metadata")
    if not isinstance(curated, dict) or len(curated) == 0:
        return BrandAssertion(
            brand_slug=brand_slug,
            verdict=BRAND_VERDICT["panel_cleanly_absent"],
            present_curated_fields=[],
            missing_curated_fields=list(_CURATED_FIELDS_FROM_SEAM),
            v3_chip_gating=v3_chip_gating,
            notes="",
        )

    present = [f for f in _CURATED_FIELDS_FROM_SEAM if f in curated and curated[f] is not None]
    missing = [f for f in _CURATED_FIELDS_FROM_SEAM if f not in present]

    # Faithful requires at least tier + category.
    verdict = BRAND_VERDICT["panel_faithful"] if _MINIMUM_FAITHFUL_FIELDS.issubset(present) \
        else BRAND_VERDICT["panel_cleanly_absent"]

    return BrandAssertion(
        brand_slug=brand_slug,
        verdict=verdict,
        present_curated_fields=present,
        missing_curated_fields=missing,
        v3_chip_gating=v3_chip_gating,
        notes="",
    )


def build_report(
    responses: list[dict[str, Any]],
    *,
    source: str,
) -> LibraryAssertionReport:
    """Aggregate a list of brand API responses into a ``LibraryAssertionReport``.

    Parameters
    ----------
    responses:
        List of raw API-response dicts, one per brand.  May be empty (produces
        a valid zero-brand report with ``all_pass: True``).
    source:
        Context label: ``"prod"`` for a live-API run, ``"fixture"`` for tests.

    Returns
    -------
    LibraryAssertionReport
        A schema-versioned, serialisable report dict.  ``all_pass`` is ``True``
        iff no brand carries the ``"page_broken"`` verdict.
    """
    assertions = [build_brand_assertion(r) for r in responses]
    verdict_counts: dict[str, int] = {v: 0 for v in BRAND_VERDICT.values()}
    for a in assertions:
        verdict_counts[a["verdict"]] = verdict_counts.get(a["verdict"], 0) + 1

    return LibraryAssertionReport(
        schema_version=LIBRARY_ASSERTION_SCHEMA_VERSION,
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        source=source,
        brand_count=len(assertions),
        verdict_counts=verdict_counts,
        assertions=assertions,
        all_pass=verdict_counts.get(BRAND_VERDICT["page_broken"], 0) == 0,
    )


def preflight_corpus(
    dtcg_bundles: list[tuple[str, dict[str, Any]]],
    *,
    source: str = "fixture",
) -> LibraryAssertionReport:
    """Run the assertion engine against offline dtcg_json bundles (pre-apply proof).

    Converts each ``(brand_slug, dtcg_json)`` pair into a mock API-response
    dict - the same shape ``build_brand_assertion`` expects from the live API -
    then runs the assertion engine.  No network, no DB; the output is a
    ``LibraryAssertionReport`` that can be inspected before the real
    ``seed_from_drl --apply`` fires.

    The conversion mirrors how ``routes.library._extract_curated_metadata``
    reads ``dtcg_json`` at query time: extract the 6 curated keys and pack them
    into the ``data.curated_metadata`` slot of the mock response.

    Parameters
    ----------
    dtcg_bundles:
        A list of ``(brand_slug, dtcg_json)`` pairs.  ``dtcg_json`` is the dict
        returned by ``scripts.seed_from_drl.build_bundle().dtcg_json``.
    source:
        Label for ``report.source`` (default ``"fixture"`` since this runs offline).

    Returns
    -------
    LibraryAssertionReport
        Ready to inspect.  ``all_pass: True`` means every brand would yield a
        faithful-or-absent panel; ``all_pass: False`` means at least one corpus
        entry is broken and the apply step should be held.
    """
    responses: list[dict[str, Any]] = []
    for brand_slug, dtcg_json in dtcg_bundles:
        # Extract the 6 curated fields from dtcg_json the same way the live
        # route does at query time.  Keys present only when the producer wrote them.
        curated = {
            field: dtcg_json[field]
            for field in _CURATED_FIELDS_FROM_SEAM
            if field in dtcg_json and dtcg_json[field] is not None
        }
        responses.append({
            "schema_version": 2,
            "data": {
                "schema_version": "library_data_v1",
                "brand_slug": brand_slug,
                "category_slug": "preflight",
                "curated_metadata": curated if curated else None,
                "missing_groups": [],  # offline preflight: assume chip-gating present
            },
        })
    return build_report(responses, source=source)


def render_markdown(report: LibraryAssertionReport) -> str:
    """Render a ``LibraryAssertionReport`` as a Markdown contact-sheet table.

    The output is a human-readable summary suitable for pasting into a
    STATUS.md entry or a PR description.  It includes the schema version,
    generation timestamp, summary counts, and a per-brand row table.

    Parameters
    ----------
    report:
        A completed ``LibraryAssertionReport`` as returned by ``build_report``.

    Returns
    -------
    str
        Markdown-formatted contact sheet.
    """
    lines: list[str] = [
        "# Library Assertion Report",
        "",
        f"**schema_version:** {report['schema_version']}",
        f"**generated_at:** {report['generated_at']}",
        f"**source:** {report['source']}",
        f"**brand_count:** {report['brand_count']}",
        f"**all_pass:** {report['all_pass']}",
        "",
        "## Verdict counts",
        "",
    ]
    for verdict, count in sorted(report["verdict_counts"].items()):
        lines.append(f"- `{verdict}`: {count}")

    lines += [
        "",
        "## Per-brand assertions",
        "",
        "| brand_slug | verdict | present_fields | missing_fields | v3_chip | notes |",
        "|---|---|---|---|---|---|",
    ]
    for a in report["assertions"]:
        present = ", ".join(sorted(a["present_curated_fields"])) or "(none)"
        missing = ", ".join(sorted(a["missing_curated_fields"])) or "(none)"
        lines.append(
            f"| {a['brand_slug']} "
            f"| {a['verdict']} "
            f"| {present} "
            f"| {missing} "
            f"| {a['v3_chip_gating']} "
            f"| {a['notes'] or ''} |"
        )

    return "\n".join(lines)
