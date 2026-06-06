"""Acceptance + regression tests for the button-fidelity capture corpus.

Stage L4 (Resemblio Library Phase B; CTO pass 2 plan
``projects/OptSus Team/cto-reviews/2026-06-05-resemblio-library-priority-revised-plan.md``
Section F) closes the openai button-capture gap. The selector override
and SPA wait strategy already exist in
``extractor/computed_styles.py``; this file's tests are the TDD red
ceremony declared in the Stage L4 Builder dispatch:

1. ``test_openai_button_capture_lands_real_styles`` - acceptance gate.
   Asserts the openai snapshot file on disk carries non-default values
   for at least 4 of 6 CSS fields the override consumes (borderRadius,
   padding, fontFamily, backgroundColor, color, boxShadow / border-width).
   Initially FAILS because the 2026-06-02 22-of-24 capture run left
   openai with default-skip placeholder values; the green-ceremony
   re-capture (Jim runs from parent shell) populates the file and
   flips this test green.

2. ``test_corpus_coverage_floor`` - regression. Asserts that at least
   23 of the 24 DRL brands have non-default button styles. aeon is the
   sole documented-skip per
   ``_handoff/inbox/claude/2026-06-02-openai-aeon-selector-revision.md``
   (structurally uncapturable behind Vercel security checkpoint). Any
   future capture-loss regression on a third brand trips this.

Snapshot location resolution mirrors what production reads via
``app.runtime_data.resolve_read_path``: runtime root first
(``RESEMBLIO_RUNTIME_DATA_ROOT`` env, defaulting to ``/var/lib/resemblio``
on prod), in-tree seed root as fallback
(``_vendored/drl/drl/_data/computed_styles/``). Tests honor both so the
same assertion works whether the green-ceremony capture writes to the
runtime root (prod) or the seed root (local dev with
``--write-into-seed`` rescue).

These tests are pure-file inspection: no network, no Playwright. The
capture itself is run out-of-band from the parent shell per Stage L4
authority (sub-agents cannot reach Playwright).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest


# --- Constants ---------------------------------------------------------------

# The 6 CSS fields the button override consumes from the snapshot. Aligned
# with `extractor.button_tokens.ButtonTokens` field names. The acceptance
# gate requires non-default values for at least this many of them.
TRACKED_BUTTON_FIELDS: Final[tuple[str, ...]] = (
    "border-radius",
    "padding",
    "font-family",
    "background-color",
    "color",
    "border",  # "boxShadow" in the dispatch brief; the captured field is `border`
              # (the snapshot does not capture box-shadow today). Border-width is
              # the closest shape-signature equivalent the capture pipeline emits.
)

# Acceptance gate: at least this many of the 6 fields must carry a
# non-default value for the openai snapshot to pass.
OPENAI_REQUIRED_NON_DEFAULT_FIELDS: Final[int] = 4

# Regression floor: at least this many of the 24 DRL brands must have
# non-default button styles. aeon is the documented-skip allowlist.
CORPUS_COVERAGE_FLOOR: Final[int] = 23

# Brands explicitly allowed to ship without a populated button snapshot
# (structurally uncapturable). aeon: Vercel security checkpoint serves a
# 33 KB challenge page to every non-cookied request; see handoff at
# `_handoff/inbox/claude/2026-06-02-openai-aeon-selector-revision.md`.
DOCUMENTED_SKIP_BRANDS: Final[frozenset[str]] = frozenset({"aeon"})

# Sentinel values the capture path writes when a slot was skipped, the
# selector matched nothing, or the property came back empty. A field
# carrying any of these is treated as "default" (not real captured data).
DEFAULT_PLACEHOLDER_VALUES: Final[frozenset[str]] = frozenset({
    "",
    "0px",
    "0px 0px",
    "0px none rgb(0, 0, 0)",
    "none",
    "normal",
    "auto",
})


# --- Path resolution ---------------------------------------------------------


def _api_root() -> Path:
    """Return the api package root (``code/api/``)."""
    # tests/test_button_corpus_coverage.py -> tests/ -> code/api/
    return Path(__file__).resolve().parents[1]


def _candidate_snapshot_dirs() -> list[Path]:
    """Return every directory the production loader would check for snapshots.

    Mirrors ``app.runtime_data.resolve_read_path``: runtime root first
    (``RESEMBLIO_RUNTIME_DATA_ROOT``, defaulting to ``/var/lib/resemblio``
    on prod), then the in-tree seed root. Tests honor both so the same
    assertion works whether the capture run wrote to runtime data (prod)
    or to the seed tree (local dev rescue path).
    """
    import os

    candidates: list[Path] = []
    runtime_env = os.environ.get("RESEMBLIO_RUNTIME_DATA_ROOT")
    if runtime_env:
        candidates.append(Path(runtime_env) / "computed_styles")
    # Always include the seed root; the runtime fallback in production
    # reads from here when the runtime root has no entry yet.
    seed_root = _api_root() / "_vendored" / "drl" / "drl" / "_data" / "computed_styles"
    candidates.append(seed_root)
    return candidates


def _find_snapshot(brand_slug: str) -> Path | None:
    """Return the first existing snapshot file for ``brand_slug``, or None."""
    for parent in _candidate_snapshot_dirs():
        candidate = parent / f"{brand_slug}.json"
        if candidate.exists():
            return candidate
    return None


# --- Snapshot field inspection -----------------------------------------------


def _load_cta_properties(snapshot_path: Path) -> dict[str, str] | None:
    """Return the ``cta`` slot properties dict, or None if missing.

    The snapshot envelope is a ``ComputedStyleReport`` with extra
    provenance keys; the indexer reads ``signals[].properties`` for the
    ``cta`` slot to derive the override. We mirror that read here.
    """
    try:
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("status") != "ok":
        return None
    signals = raw.get("signals")
    if not isinstance(signals, list):
        return None
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        if signal.get("slot") != "cta":
            continue
        props = signal.get("properties")
        if isinstance(props, dict):
            return {str(k): str(v) for k, v in props.items()}
    return None


def _count_non_default_fields(properties: dict[str, str]) -> int:
    """Count how many of TRACKED_BUTTON_FIELDS carry a non-default value."""
    count = 0
    for field_name in TRACKED_BUTTON_FIELDS:
        value = properties.get(field_name, "").strip()
        if not value:
            continue
        if value in DEFAULT_PLACEHOLDER_VALUES:
            continue
        count += 1
    return count


def _brand_has_real_button_styles(brand_slug: str) -> tuple[bool, str]:
    """Return (passed, reason).

    A brand "has real button styles" when its snapshot exists, the
    ``cta`` slot is present, and at least
    ``OPENAI_REQUIRED_NON_DEFAULT_FIELDS`` of TRACKED_BUTTON_FIELDS
    carry non-default values.
    """
    snapshot_path = _find_snapshot(brand_slug)
    if snapshot_path is None:
        return False, f"no snapshot file on disk for {brand_slug}"
    properties = _load_cta_properties(snapshot_path)
    if properties is None:
        return False, f"snapshot at {snapshot_path} has no usable cta slot"
    non_default = _count_non_default_fields(properties)
    if non_default < OPENAI_REQUIRED_NON_DEFAULT_FIELDS:
        return False, (
            f"snapshot at {snapshot_path} has only {non_default} non-default "
            f"fields of {len(TRACKED_BUTTON_FIELDS)} tracked; "
            f"required >= {OPENAI_REQUIRED_NON_DEFAULT_FIELDS}; "
            f"captured properties={properties!r}"
        )
    return True, f"snapshot at {snapshot_path} has {non_default} non-default fields"


# --- DRL corpus discovery (for the regression test) --------------------------


def _drl_corpus_brand_slugs() -> list[str]:
    """Return every brand slug present in the DRL ``_extractions/`` tree.

    Reads from the runtime DRL root (default ``/opt/resemblio-api/drl``)
    when ``RESEMBLIO_DRL_ROOT`` env is set, falling back to the workspace
    DRL project at ``projects/Design Reference Library/``. The test
    skips with a clear message if neither root is present so local-dev
    runs without a DRL checkout do not false-fail.
    """
    import os

    candidates: list[Path] = []
    env_root = os.environ.get("RESEMBLIO_DRL_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path("/opt/resemblio-api/drl"))
    # Workspace fallback for local-dev runs.
    workspace_drl = (
        _api_root().parents[2]  # code/api -> code -> Resemblio -> projects
        / "Design Reference Library"
    )
    candidates.append(workspace_drl)

    for root in candidates:
        extractions = root / "_extractions"
        if not extractions.exists():
            continue
        slugs = sorted(
            child.name
            for child in extractions.iterdir()
            if child.is_dir()
            and not child.name.startswith("_")
            and not child.name.startswith(".")
        )
        if slugs:
            return slugs
    return []


# --- Tests -------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "openai button capture impl pending - selector override + SPA wait exist "
        "in extractor/computed_styles.py but the re-capture run has not been "
        "executed yet (requires Playwright from parent shell, not a sub-agent). "
        "Green ceremony: python -m scripts.capture_all_button_snapshots "
        "--apply --single openai --force --drl-root /opt/resemblio-api/drl "
        "then remove this xfail marker. Tracked as next-action in "
        "projects/Resemblio/STATUS.md (openai+aeon button-capture follow-on)."
    ),
)
def test_openai_button_capture_lands_real_styles() -> None:
    """Acceptance gate for Stage L4 openai re-capture.

    TDD red-ceremony anchor (committed 5d36712, 2026-06-05). Marked
    xfail(strict=True) until the re-capture run is executed. When the
    run completes and the snapshot is populated, this test goes green
    and the suite will XPASS (fail) to alert the developer to remove
    the xfail marker. That XPASS failure is the intended signal.

    FAILS today: the 2026-06-02 corpus refresh left openai with the
    default-skip snapshot because the selector override + SPA wait
    landed AFTER the capture run completed. Green ceremony per Stage L4
    Builder dispatch:

        python -m scripts.capture_all_button_snapshots \\
            --apply --single openai --force \\
            --drl-root /opt/resemblio-api/drl

    (Jim runs from parent shell; sub-agents lack Playwright.)

    Acceptance criterion: at least 4 of 6 TRACKED_BUTTON_FIELDS carry
    non-default values. 4 of 6 (not 6 of 6) accommodates the openai
    palette: their primary CTA does not set an explicit ``border``
    shorthand, and ``box-shadow`` is not in the capture census; a
    perfectly-captured openai button still lands ~4 fields, not 6.
    """
    passed, reason = _brand_has_real_button_styles("openai")
    assert passed, (
        "openai button snapshot must carry real captured styles after "
        "Stage L4 re-capture. " + reason
    )


def test_corpus_coverage_floor() -> None:
    """Regression: 23 of 24 DRL brands must have real button styles.

    aeon is the lone documented-skip (Vercel security checkpoint). Any
    third brand losing capture coverage trips this test. New brands
    added to the corpus inherit the floor automatically: they must
    capture or be added to ``DOCUMENTED_SKIP_BRANDS`` with a handoff
    note explaining the structural reason.
    """
    slugs = _drl_corpus_brand_slugs()
    if not slugs:
        pytest.skip(
            "No DRL _extractions tree found at RESEMBLIO_DRL_ROOT, "
            "/opt/resemblio-api/drl, or the workspace DRL project. "
            "Regression-floor test requires the live DRL corpus."
        )

    passing: list[str] = []
    failing: dict[str, str] = {}
    for slug in slugs:
        if slug in DOCUMENTED_SKIP_BRANDS:
            continue
        ok, reason = _brand_has_real_button_styles(slug)
        if ok:
            passing.append(slug)
        else:
            failing[slug] = reason

    # The floor is expressed against the corpus minus allowed skips. If
    # the corpus grows past 24 the floor adjusts proportionally: at least
    # (total - len(skip-allowlist)) brands must pass.
    required = len(slugs) - len(DOCUMENTED_SKIP_BRANDS & set(slugs))
    # Convert the absolute floor to a relative-to-corpus check: if the
    # corpus is exactly the known-24, require CORPUS_COVERAGE_FLOOR.
    # Otherwise require (total - documented-skips) to handle future
    # corpus growth without code change.
    expected = max(CORPUS_COVERAGE_FLOOR, required)
    assert len(passing) >= expected, (
        f"corpus coverage floor: {len(passing)} of {len(slugs)} brands passed; "
        f"required >= {expected}. Failing: {failing!r}"
    )
