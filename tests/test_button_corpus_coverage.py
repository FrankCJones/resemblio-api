"""Acceptance + regression tests for the button-fidelity capture corpus.

Stage L4 (Resemblio Library Phase B) produced the button-capture pipeline.
22 of 24 DRL brands now have real button styles captured. Two brands are
permanent structural skips (L4 v3, 2026-06-07):

1. ``test_openai_permanent_structural_skip`` - asserts openai is in
   ``DOCUMENTED_SKIP_BRANDS``. openai.com is gated by Cloudflare Turnstile
   (HTTP 403 on live capture); the saved fixture has 0 inline ``<style>``
   tags; CDN CSS chunks also return HTTP 403. No honest offline path exists.
   ADR: ``02-prd/2026-06-07-openai-permanent-skip.md``.

2. ``test_corpus_coverage_floor`` - regression. Asserts at least 22 of 24
   DRL brands have non-default button styles. aeon (Vercel checkpoint) and
   openai (Cloudflare Turnstile + CDN CSS 403) are the two documented skips.
   Any additional capture-loss regression trips this.

Browser-default gate hardening (L4 v3 Phase 1): ``_count_non_default_fields``
uses a two-tier check: blanket sentinel values (DEFAULT_PLACEHOLDER_VALUES) and
anchor-default markers (_ANCHOR_DEFAULT_MARKERS + cluster guard for transparent
background). Discovered when a CSS-less anchor render slipped through with 4
"non-default" fields that were all browser defaults (link-blue color/border,
Times New Roman, transparent bg).

Snapshot location resolution mirrors what production reads via
``app.runtime_data.resolve_read_path``: runtime root first
(``RESEMBLIO_RUNTIME_DATA_ROOT`` env, defaulting to ``/var/lib/resemblio``
on prod), in-tree seed root as fallback
(``_vendored/drl/drl/_data/computed_styles/``). Tests honor both so the
same assertion works whether a capture wrote to the runtime root (prod)
or the seed root (local dev with ``--write-into-seed`` rescue).

These tests are pure-file inspection: no network, no Playwright.
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
# non-default button styles. aeon and openai are the documented-skip allowlist.
CORPUS_COVERAGE_FLOOR: Final[int] = 22

# Brands explicitly allowed to ship without a populated button snapshot
# (structurally uncapturable).
#
# aeon: Vercel security checkpoint serves a 33 KB challenge page to every
# non-cookied request. No real DOM; selector and wait fixes cannot help.
# Handoff: `_handoff/inbox/claude/2026-06-02-openai-aeon-selector-revision.md`.
# ADR: 02-prd/2026-06-06-aeon-permanent-skip.md.
#
# openai: Cloudflare Turnstile blocks live capture (HTTP 403). The saved
# fixture (openai_homepage.html, 419 KB) has 0 inline <style> tags and 12
# external Next.js CSS chunks that also return HTTP 403 from the CDN. All
# button styling is in Tailwind utility classes that require the unreachable
# CSS to resolve. There is no honest offline path to real button tokens.
# ADR: 02-prd/2026-06-07-openai-permanent-skip.md.
DOCUMENTED_SKIP_BRANDS: Final[frozenset[str]] = frozenset({"aeon", "openai"})

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

# Browser-default values that appear on anchor elements when no CSS is loaded.
# These are never legitimate brand design tokens and must be treated as defaults.
# Root cause: anchor border-color defaults to the link color (rgb(0, 0, 238)),
# not black (rgb(0, 0, 0)), so the existing "0px none rgb(0, 0, 0)" sentinel in
# DEFAULT_PLACEHOLDER_VALUES missed link-blue borders. Discovered during L4 v2
# Phase 3 STOP analysis (2026-06-07) when a CSS-less openai fixture render
# slipped through with 4 "non-default" fields that were all browser defaults.
_ANCHOR_DEFAULT_MARKERS: Final[frozenset[str]] = frozenset({
    "0px none rgb(0, 0, 238)",  # border shorthand: browser default for anchors (link-blue)
    "rgb(0, 0, 238)",           # color: browser link blue (never a real brand CTA color)
    '"Times New Roman"',        # font-family: browser default serif (never a real brand font)
})

# background-color for anchor elements defaults to rgba(0, 0, 0, 0) (transparent).
# This IS a legitimate value for ghost-button brands (intentional design token).
# It is treated as a browser default only when another _ANCHOR_DEFAULT_MARKER value
# co-occurs in the same captured set, indicating a CSS-less render rather than an
# intentional ghost-button design.
_ANCHOR_TRANSPARENT_BG: Final[str] = "rgba(0, 0, 0, 0)"


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
    """Count how many of TRACKED_BUTTON_FIELDS carry a non-default value.

    Two-tier default detection:

    1. Blanket sentinel values (DEFAULT_PLACEHOLDER_VALUES): always treated as
       default regardless of context. Covers the capture-path placeholder outputs
       and universal CSS defaults (0px, none, auto, etc.).

    2. Browser-default anchor markers (_ANCHOR_DEFAULT_MARKERS + cluster guard):
       Values that appear on ``<a>`` elements when no CSS is loaded. Three are
       unambiguously non-brand (link-blue color/border, default serif). One
       (transparent background) is legitimate for ghost buttons but is still a
       browser default for anchor elements - it is only counted as a default when
       another _ANCHOR_DEFAULT_MARKER value co-occurs (cluster guard), indicating
       a CSS-less render rather than an intentional design choice.
    """
    # Pre-scan: is any anchor-default marker value present in the tracked fields?
    # A single co-occurring marker is enough to flag transparent bg as a default too.
    anchor_marker_present = any(
        properties.get(field, "").strip() in _ANCHOR_DEFAULT_MARKERS
        for field in TRACKED_BUTTON_FIELDS
    )

    count = 0
    for field_name in TRACKED_BUTTON_FIELDS:
        value = properties.get(field_name, "").strip()
        if not value:
            continue
        if value in DEFAULT_PLACEHOLDER_VALUES:
            continue
        if value in _ANCHOR_DEFAULT_MARKERS:
            continue
        # Cluster guard: transparent bg is a browser default for anchors.
        # Count as real only when no other anchor-default markers are present
        # (i.e., this looks like a real ghost-button brand, not a CSS-less render).
        if value == _ANCHOR_TRANSPARENT_BG and anchor_marker_present:
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


def test_openai_permanent_structural_skip() -> None:
    """openai is a documented permanent structural skip, identical in tier to aeon.

    Evidence chain (all verified 2026-06-07, see ADR
    ``02-prd/2026-06-07-openai-permanent-skip.md``):

    1. openai.com serves HTTP 403 + Cloudflare Turnstile challenge shell to
       headless Playwright. No selector or wait fix helps.
    2. The saved fixture (openai_homepage.html, 419 KB) has 0 inline ``<style>``
       tags and 12 external Next.js CSS chunks (``/_next/static/chunks/*.css``).
       ``page.set_content`` cannot fetch relative-path subresources without a server.
    3. The CDN CSS chunks also return HTTP 403 Forbidden. The deployment hash in
       the URLs is rotated and the chunks are gated by Cloudflare.
    4. All button styling lives in Tailwind utility classes (``rounded-full``,
       ``border-primary-12``, etc.) that require exactly the unreachable CSS to
       resolve. There is no honest offline path to openai's real button tokens.

    Revisit trigger: openai drops Turnstile OR a real-browser capture pipeline exists.
    Evidence fixture: ``tests/fixtures/button_capture/openai_homepage.html``.
    """
    assert "openai" in DOCUMENTED_SKIP_BRANDS, (
        "'openai' must be in DOCUMENTED_SKIP_BRANDS. "
        "openai.com is gated by Cloudflare Turnstile (HTTP 403); its CDN CSS "
        "chunks also return HTTP 403, making fixture-capture impossible offline. "
        "See 02-prd/2026-06-07-openai-permanent-skip.md."
    )


def test_corpus_coverage_floor() -> None:
    """Regression: 22 of 24 DRL brands must have real button styles.

    Two documented structural skips (aeon: Vercel checkpoint; openai: Cloudflare
    Turnstile + CDN CSS 403) reduce the floor from 24 to 22. Any additional
    brand losing capture coverage trips this test. New brands added to the corpus
    inherit the floor automatically: they must capture or be added to
    ``DOCUMENTED_SKIP_BRANDS`` with a handoff note explaining the structural
    reason.
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
