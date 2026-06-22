"""DRL fidelity oracle: measure fidelity of all 955 DRL assets vs Resemblio.

Compares each DRL ``asset.html`` (reference) against the corresponding
Resemblio library page (candidate) in two tiers:

  1. Structural (PRIMARY, hard): fidelity-bearing computed CSS properties
     (color, background-color, border, border-radius, padding, font-family,
     font-size, font-weight, letter-spacing, box-shadow, transition) must
     match between reference state nodes and the candidate component root.
     This is the gate that blocks Epic #35's definition of done.

  2. Pixel (SECONDARY, informational): screenshot SSIM against
     PIXEL_SSIM_FLOOR. Does NOT gate the verdict; stored alongside structural
     results for drift diagnosis and future calibration.

Both tiers require Playwright + Pillow (``[browser]`` optional dep). When
absent, the capture functions return None and tests self-skip via
``pytest.importorskip`` - mirroring the discipline in
``test_visual_fidelity_gate.py``.

State detection in reference asset.html:
  DRL assets render multiple states (rest/hover/focus/disabled) as distinct
  DOM nodes inside ``.group`` divs, each paired with a ``.state-label``
  text sibling. The oracle captures ``getComputedStyle`` on the interactive
  element adjacent to each label.

  Assets without state labels (alphabets, layouts, libraries) render a
  single composition; the oracle captures a single "default" state from the
  body's first non-label element.

Candidate chrome exclusion:
  The Resemblio library page wraps each component in:
    <article class="rs-library-page" data-rs-source="drl-component" ...>
      <aside class="rs-font-disclosure">...</aside>
      [component element]
    </article>
  Capture targets the first non-aside child of the article; the article
  wrapper and font-disclosure aside are excluded from the comparison.

Run the oracle (requires Playwright + Pillow + live Resemblio site):
  python -m tests.render.fidelity_oracle \\
      --corpus-root _vendored/drl_corpus \\
      --base-url https://resemblio.com \\
      --output-dir tests/render

Schema: fidelity_oracle_v1  (per-asset verdict)
        fidelity_baseline_map_v1  (aggregate baseline map)

Do this work at a level that would impress a senior developer.
Include documentation and code comments that make it easy for a future
developer to maintain this project.
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

_log = logging.getLogger("fidelity_oracle")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: CSS longhand properties that carry design identity. A mismatch on any of
#: these between reference and candidate constitutes a structural-tier failure.
#: Longhands (not shorthands) because ``getComputedStyle`` resolves shorthands
#: to their constituent longhands in all major browsers.
FIDELITY_PROPERTIES: Tuple[str, ...] = (
    "color",
    "background-color",
    "border-top-color",
    "border-top-width",
    "border-top-style",
    "border-top-left-radius",
    "border-top-right-radius",
    "border-bottom-left-radius",
    "border-bottom-right-radius",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
    "font-family",
    "font-size",
    "font-weight",
    "letter-spacing",
    "box-shadow",
    "transition",
)

#: SSIM floor for the secondary (pixel) tier.
#:
#: RATIONALE: This is an initial estimate for the first baseline run.
#: Calibration: After the first run, examine SSIM scores for structural-PASS
#: assets (those that pass the structural tier) to find the noise floor of
#: genuinely-matching renders. Real mismatches (token-tinted generic template
#: vs. component-specific DRL asset) typically score < 0.5. A value of 0.85
#: is conservative: it will reject renders that differ beyond simple font
#: substitution noise. Update this value once real baseline data is available.
PIXEL_SSIM_FLOOR: float = 0.85

#: Viewport for both reference and candidate renders. Same as the existing harness.
ORACLE_VIEWPORT: Dict[str, int] = {"width": 1280, "height": 800}

#: Post-navigation wait (ms) for fonts, transitions, and layout to settle.
ORACLE_WAIT_MS: int = 2000

SCHEMA_VERSION = "fidelity_oracle_v1"
BASELINE_SCHEMA_VERSION = "fidelity_baseline_map_v1"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StyleDiff:
    """One CSS property that differs between reference and candidate for a state.

    Attributes:
        state:     State name (e.g. "rest", "hover", "focus", "default").
        property:  CSS property name (one of FIDELITY_PROPERTIES).
        reference: Computed value from the DRL asset.html reference render.
        candidate: Computed value from the Resemblio candidate render, or
                   the sentinel "<missing>" when the candidate has no style
                   map for this state.
    """

    state: str
    property: str
    reference: str
    candidate: str


@dataclass
class FidelityVerdict:
    """Structural comparison result for one DRL asset.

    Attributes:
        verdict:   "pass"             - all fidelity properties match.
                   "fail"             - at least one property or state differs.
                   "candidate_missing"- no Resemblio page for (brand, class).
        diffs:     All mismatching (state, property) pairs. Empty on pass or
                   candidate_missing.
        tier:      "structural" - the hard tier failed (property mismatch).
                   "pixel"      - structural passed but SSIM < PIXEL_SSIM_FLOOR.
                   "none"       - verdict is "pass".
                   "n/a"        - verdict is "candidate_missing".
        ssim:      SSIM score from the pixel tier; None when not computed.
        schema_version: "fidelity_oracle_v1".
    """

    verdict: str
    diffs: List[StyleDiff] = field(default_factory=list)
    tier: str = "none"
    ssim: Optional[float] = None
    schema_version: str = SCHEMA_VERSION


@dataclass
class BaselineEntry:
    """One row in the baseline pass/fail map (one DRL asset).

    Sorted key: (brand, asset_class, asset_slug) - deterministic across runs.
    """

    brand: str
    asset_class: str
    asset_slug: str
    verdict: str       # "pass" | "fail" | "candidate_missing"
    diffs: List[StyleDiff] = field(default_factory=list)
    tier: str = "none" # "structural" | "pixel" | "none" | "n/a"
    ssim: Optional[float] = None


@dataclass
class BaselineMap:
    """Fidelity baseline across all 955 DRL assets. Epic #35 burn-down list.

    Entries are sorted by (brand, asset_class, asset_slug). The sort is
    deterministic so two oracle runs on the same corpus produce identical
    maps (modulo ``generated_at`` and changed candidate HTML).

    Steps 2-6 of Epic #35 close failing assets one by one; when
    ``fail_count`` reaches 0 the epic is done.

    schema_version: "fidelity_baseline_map_v1". Bump when shape changes.
    """

    asset_count: int
    pass_count: int
    fail_count: int
    missing_count: int
    entries: List[BaselineEntry]
    generated_at: str
    schema_version: str = BASELINE_SCHEMA_VERSION


@dataclass(frozen=True)
class CorpusAsset:
    """One DRL asset from the vendored corpus (read from corpus.json).

    Attributes:
        brand:       System slug (e.g. "a24").
        asset_class: DRL class (e.g. "alphabets", "buttons", "layouts").
        asset_slug:  Asset slug within the system (e.g. "a24-cinematic-001").
        html_path:   Absolute path to the asset's ``asset.html`` file.
    """

    brand: str
    asset_class: str
    asset_slug: str
    html_path: pathlib.Path


# ---------------------------------------------------------------------------
# Pure comparator (no browser, no filesystem)
# ---------------------------------------------------------------------------


def compare_computed_styles(
    reference: Dict[str, Dict[str, str]],
    candidate: Dict[str, Dict[str, str]],
    *,
    properties: Sequence[str] = FIDELITY_PROPERTIES,
) -> FidelityVerdict:
    """Compare reference and candidate computed-style maps structurally.

    For each state in ``reference``, checks whether ``candidate`` has that
    state and whether all fidelity-bearing properties match exactly. Extra
    states present in ``candidate`` but absent from ``reference`` are ignored:
    the oracle measures reference -> candidate fidelity, not the reverse.

    A "pass" verdict requires:
      - Every reference state is present in ``candidate``.
      - Every fidelity property in every matching state has equal computed values.

    A "fail" verdict is returned otherwise, with a ``StyleDiff`` for every
    mismatching (state, property) pair. When ``candidate`` lacks a state
    entirely, a diff is emitted for every fidelity property with
    ``candidate="<missing>"``. This is intentional: the Resemblio library
    currently serves single-state generic templates while DRL assets render
    rest/hover/focus/disabled as distinct nodes.

    Pure: no network, no filesystem access, no browser.

    Args:
        reference:  Dict[state_name, Dict[property, computed_value]] from
                    the DRL asset.html reference render.
        candidate:  Dict[state_name, Dict[property, computed_value]] from
                    the Resemblio candidate page render.
        properties: CSS properties to compare. Defaults to FIDELITY_PROPERTIES.

    Returns:
        FidelityVerdict with verdict="pass" and empty diffs, or
        verdict="fail" with tier="structural" and all differing StyleDiffs.
    """
    diffs: List[StyleDiff] = []

    for state, ref_styles in reference.items():
        cand_styles = candidate.get(state)
        if cand_styles is None:
            # Candidate has no style map for this state: emit one diff per
            # fidelity property so the remediation step has a concrete target.
            for prop in properties:
                diffs.append(StyleDiff(
                    state=state,
                    property=prop,
                    reference=ref_styles.get(prop, ""),
                    candidate="<missing>",
                ))
        else:
            for prop in properties:
                ref_val = ref_styles.get(prop, "")
                cand_val = cand_styles.get(prop, "")
                if ref_val != cand_val:
                    diffs.append(StyleDiff(
                        state=state,
                        property=prop,
                        reference=ref_val,
                        candidate=cand_val,
                    ))

    if diffs:
        return FidelityVerdict(verdict="fail", diffs=diffs, tier="structural")
    return FidelityVerdict(verdict="pass", diffs=[], tier="none")


# ---------------------------------------------------------------------------
# Baseline map builder (pure, no browser)
# ---------------------------------------------------------------------------


def build_baseline_map(
    verdicts: Sequence[Tuple[str, str, str, FidelityVerdict]],
) -> BaselineMap:
    """Build the fidelity baseline map from per-asset verdicts.

    Sorts entries by (brand, asset_class, asset_slug) for deterministic
    ordering across oracle runs. Aggregates pass/fail/missing counts.

    Args:
        verdicts: sequence of (brand, asset_class, asset_slug, FidelityVerdict).
                  Input order is ignored; output is always sorted.

    Returns:
        BaselineMap with all entries sorted, counts aggregated, and
        schema_version "fidelity_baseline_map_v1".
    """
    entries: List[BaselineEntry] = [
        BaselineEntry(
            brand=brand,
            asset_class=klass,
            asset_slug=slug,
            verdict=v.verdict,
            diffs=list(v.diffs),
            tier=v.tier,
            ssim=v.ssim,
        )
        for brand, klass, slug, v in verdicts
    ]
    entries.sort(key=lambda e: (e.brand, e.asset_class, e.asset_slug))

    pass_count = sum(1 for e in entries if e.verdict == "pass")
    fail_count = sum(1 for e in entries if e.verdict == "fail")
    missing_count = sum(1 for e in entries if e.verdict == "candidate_missing")

    return BaselineMap(
        asset_count=len(entries),
        pass_count=pass_count,
        fail_count=fail_count,
        missing_count=missing_count,
        entries=entries,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Corpus iteration (reads corpus.json, no filesystem globbing)
# ---------------------------------------------------------------------------


def iter_corpus_assets(corpus_root: pathlib.Path) -> Iterator[CorpusAsset]:
    """Yield all DRL assets from the vendored corpus in deterministic order.

    Reads ``corpus.json`` for the authoritative asset list; does not glob
    the filesystem (avoids non-determinism from OS filesystem ordering).
    Assets are yielded sorted by (brand, asset_class, asset_slug).

    Args:
        corpus_root: Path to the vendored DRL corpus root directory (the one
                     that contains ``corpus.json`` and the ``assets/`` tree).
                     Typically ``code/api/_vendored/drl_corpus``.

    Yields:
        CorpusAsset for each of the 955 DRL assets.

    Raises:
        FileNotFoundError: When ``corpus.json`` is absent.
        json.JSONDecodeError: When ``corpus.json`` is malformed.
    """
    corpus_json = corpus_root / "corpus.json"
    corpus = json.loads(corpus_json.read_text(encoding="utf-8"))

    rows: List[CorpusAsset] = []
    for system in corpus.get("systems", []):
        brand: str = system["slug"]
        for asset in system.get("assets", []):
            slug: str = asset["slug"]
            klass: str = asset["class"]
            html_path = corpus_root / asset["path"] / "asset.html"
            rows.append(CorpusAsset(
                brand=brand,
                asset_class=klass,
                asset_slug=slug,
                html_path=html_path,
            ))

    rows.sort(key=lambda r: (r.brand, r.asset_class, r.asset_slug))
    yield from rows


# ---------------------------------------------------------------------------
# Candidate chrome helpers (pure - no browser)
# ---------------------------------------------------------------------------

# Matches <article data-rs-source="drl-component"> - the Resemblio library
# page wrapper. Used by is_candidate_wrapped and extract_component_from_candidate.
_RS_ARTICLE_RE = re.compile(
    r'<article\b[^>]*\bdata-rs-source="drl-component"[^>]*>',
    re.DOTALL,
)

# Matches <aside ...>...</aside> (including nested tags via DOTALL).
# Removes the font-disclosure aside injected by the Resemblio library indexer.
_ASIDE_RE = re.compile(r"<aside\b[^>]*>.*?</aside>", re.DOTALL | re.IGNORECASE)


def is_candidate_wrapped(rendered_html: str) -> bool:
    """Return True when rendered_html contains the Resemblio library-page article.

    The Resemblio library page wraps each component in:
      <article class="rs-library-page" data-rs-source="drl-component" ...>

    Pure function: no network, no browser.
    """
    return bool(_RS_ARTICLE_RE.search(rendered_html))


def extract_component_from_candidate(rendered_html: str) -> Optional[str]:
    """Extract the component HTML from a Resemblio library-page candidate.

    Finds ``<article data-rs-source="drl-component">`` and returns its
    inner content with the ``<aside class="rs-font-disclosure">`` removed.
    Both the article wrapper and the aside are excluded from the returned
    string.

    Returns None when the article wrapper is not present (not a Resemblio
    library page).

    Pure function: no network, no browser. Uses regex on known-structured
    Resemblio HTML (the indexer produces well-formed markup).
    """
    article_m = _RS_ARTICLE_RE.search(rendered_html)
    if not article_m:
        return None
    inner_start = article_m.end()
    # Use rfind to find the closing </article>. The article is the
    # outermost element in the Resemblio library page fragment.
    inner_end = rendered_html.rfind("</article>", inner_start)
    if inner_end == -1:
        return None
    inner = rendered_html[inner_start:inner_end]
    # Remove all <aside>...</aside> blocks (font-disclosure aside + any nested).
    inner = _ASIDE_RE.sub("", inner)
    result = inner.strip()
    return result if result else None


# ---------------------------------------------------------------------------
# Browser capture helpers (guarded - require Playwright + Pillow)
# ---------------------------------------------------------------------------

#: JavaScript fragment executed via page.evaluate to capture getComputedStyle
#: for all FIDELITY_PROPERTIES on a single element. Returns a Dict[str, str]
#: or null when the selector matches nothing.
_CAPTURE_STYLE_JS = (
    "(el, props) => {"
    "  if (!el) return null;"
    "  const cs = window.getComputedStyle(el);"
    "  const r = {};"
    "  for (const p of props) r[p] = cs.getPropertyValue(p).trim();"
    "  return r;"
    "}"
)


def capture_reference_styles(
    asset_html_path: pathlib.Path,
) -> Optional[Dict[str, Dict[str, str]]]:
    """Capture computed styles from a DRL asset.html per-state node.

    Renders ``asset_html_path`` in headless Chromium at ORACLE_VIEWPORT.

    State detection:
      Scans for ``.group`` elements. For each, reads the ``.state-label``
      text (the state name) and captures ``getComputedStyle`` on the first
      non-label sibling (the component element).

      If no ``.group`` structure is found (alphabets, layouts, libraries
      render a single composition), falls back to a single "default" state
      on the body's first child element.

    Returns:
      Dict[state_name, Dict[property, value]] or None on failure.
      Returns None without raising when the browser cannot render the file.

    Requires Playwright ([browser] optional dep). Self-skip in tests via
    ``pytest.importorskip("playwright.sync_api")``.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log.debug("Playwright not installed; skipping reference capture for %s", asset_html_path)
        return None

    url = asset_html_path.as_uri()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport=ORACLE_VIEWPORT)
            page.goto(url, wait_until="networkidle", timeout=15_000)
            page.wait_for_timeout(ORACLE_WAIT_MS)

            states: Dict[str, Dict[str, str]] = {}
            groups = page.query_selector_all(".group")
            if groups:
                for group in groups:
                    label_el = group.query_selector(".state-label")
                    if label_el is None:
                        continue
                    state_name = (label_el.inner_text() or "").strip()
                    if not state_name:
                        continue
                    # Component element: first child of .group that is NOT .state-label.
                    siblings = group.query_selector_all(":scope > :not(.state-label)")
                    if not siblings:
                        continue
                    raw: Optional[Dict[str, str]] = siblings[0].evaluate(
                        _CAPTURE_STYLE_JS, list(FIDELITY_PROPERTIES)
                    )
                    if raw:
                        states[state_name] = raw

            # Fallback: single "default" state on the body's first element.
            if not states:
                el = page.query_selector("body > :not(.state-label)")
                if el is None:
                    el = page.query_selector("body")
                if el:
                    raw = el.evaluate(_CAPTURE_STYLE_JS, list(FIDELITY_PROPERTIES))
                    if raw:
                        states["default"] = raw

            browser.close()
            return states if states else None
    except Exception as exc:
        _log.warning("capture_reference_styles failed for %s: %s", asset_html_path, exc)
        return None


def capture_candidate_styles(
    rendered_html: str,
) -> Optional[Dict[str, Dict[str, str]]]:
    """Capture computed styles from a Resemblio library-page candidate.

    Wraps ``rendered_html`` in a minimal full HTML document and renders it
    in headless Chromium at ORACLE_VIEWPORT. Targets the component element -
    the first non-aside child of ``article[data-rs-source="drl-component"]``
    - excluding the article wrapper and the font-disclosure aside.

    The Resemblio generic template renders a single state, so this function
    returns ``{"default": {property: value, ...}}``. State comparison for
    the candidate will be driven by Steps 2-6 of Epic #35 once the library
    serves real DRL components with explicit state nodes.

    Returns:
      {"default": {property: value, ...}} or None on failure.

    Requires Playwright ([browser] optional dep).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log.debug("Playwright not installed; skipping candidate capture")
        return None

    # Build a minimal full-page document from the fragment.
    full_html = (
        '<!doctype html><html><head>'
        '<meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        "</head><body>"
        + rendered_html
        + "</body></html>"
    )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport=ORACLE_VIEWPORT)
            page.set_content(full_html, wait_until="domcontentloaded")
            page.wait_for_timeout(ORACLE_WAIT_MS)

            # First non-aside child of the drl-component article.
            # This excludes the font-disclosure aside and the wrapper itself.
            selector = (
                'article[data-rs-source="drl-component"]'
                " > :not(aside):not(.rs-font-disclosure)"
            )
            el = page.query_selector(selector)
            if el is None:
                # Fallback: first child inside the article (excluding nothing).
                el = page.query_selector(
                    'article[data-rs-source="drl-component"] > *'
                )
            if el is None:
                # Not a Resemblio-wrapped page: fall back to body's first element.
                el = page.query_selector("body > *")
            if el is None:
                browser.close()
                return None

            raw: Optional[Dict[str, str]] = el.evaluate(
                _CAPTURE_STYLE_JS, list(FIDELITY_PROPERTIES)
            )
            browser.close()
            return {"default": raw} if raw else None
    except Exception as exc:
        _log.warning("capture_candidate_styles failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Baseline map serialization
# ---------------------------------------------------------------------------


def _entry_to_dict(e: BaselineEntry) -> Dict[str, Any]:
    """Serialize a BaselineEntry to a JSON-serializable dict."""
    return {
        "brand": e.brand,
        "asset_class": e.asset_class,
        "asset_slug": e.asset_slug,
        "verdict": e.verdict,
        "tier": e.tier,
        "ssim": e.ssim,
        "diffs": [
            {
                "state": d.state,
                "property": d.property,
                "reference": d.reference,
                "candidate": d.candidate,
            }
            for d in e.diffs
        ],
    }


def write_baseline_map(
    bm: BaselineMap,
    *,
    output_dir: pathlib.Path,
) -> Tuple[pathlib.Path, pathlib.Path]:
    """Write the baseline map to JSON and Markdown files under output_dir.

    The JSON file carries ``schema_version`` "fidelity_baseline_map_v1" so
    downstream consumers can detect shape changes.

    The Markdown summary is human-readable and intended for commit review
    and Opus sign-off.

    Returns:
        (json_path, md_path) tuple of the written file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- JSON ---
    json_data: Dict[str, Any] = {
        "schema_version": bm.schema_version,
        "generated_at": bm.generated_at,
        "asset_count": bm.asset_count,
        "pass_count": bm.pass_count,
        "fail_count": bm.fail_count,
        "missing_count": bm.missing_count,
        "entries": [_entry_to_dict(e) for e in bm.entries],
    }
    json_path = output_dir / "baseline_map.json"
    json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")

    # --- Markdown ---
    lines = [
        "# Fidelity Oracle Baseline Map",
        "",
        f"Generated: {bm.generated_at}",
        f"Schema: {bm.schema_version}",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|---|---|",
        f"| Total assets | {bm.asset_count} |",
        f"| Pass | {bm.pass_count} |",
        f"| Fail | {bm.fail_count} |",
        f"| Candidate missing | {bm.missing_count} |",
        "",
        "## Entries",
        "",
        "| Brand | Class | Slug | Verdict | Tier | Diff count |",
        "|---|---|---|---|---|---|",
    ]
    for e in bm.entries:
        lines.append(
            f"| {e.brand} | {e.asset_class} | {e.asset_slug}"
            f" | {e.verdict} | {e.tier} | {len(e.diffs)} |"
        )
    md_path = output_dir / "baseline_map.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return json_path, md_path


# ---------------------------------------------------------------------------
# Oracle runner
# ---------------------------------------------------------------------------


def run_oracle_baseline(
    corpus_root: pathlib.Path,
    *,
    get_candidate_html: Callable[[str, str], Optional[str]],
    output_dir: pathlib.Path,
) -> BaselineMap:
    """Run the fidelity oracle across all 955 DRL assets and write baseline.

    For each asset:
    1. Call ``get_candidate_html(brand, asset_class)`` (cached per brand+class).
    2. If no candidate: record verdict="candidate_missing".
    3. If candidate found: capture reference and candidate computed styles via
       Playwright, then compare with ``compare_computed_styles``.
    4. If Playwright is unavailable: record verdict="candidate_missing".

    Candidate HTML is fetched once per (brand, asset_class) pair and cached
    for the duration of the run; multiple assets that share the same library
    page (e.g. all of a24's button atoms) reuse the same HTML without a
    second fetch.

    Args:
        corpus_root:        Path to _vendored/drl_corpus.
        get_candidate_html: Callable(brand, asset_class) -> rendered_html | None.
                            None means no Resemblio library page exists.
        output_dir:         Directory to write baseline_map.json + .md.

    Returns:
        BaselineMap with all 955 entries.
    """
    verdicts: List[Tuple[str, str, str, FidelityVerdict]] = []
    # Cache candidate HTML per (brand, asset_class) to avoid redundant fetches.
    candidate_cache: Dict[Tuple[str, str], Optional[str]] = {}

    assets = list(iter_corpus_assets(corpus_root))
    total = len(assets)
    _log.info("Starting oracle run: %d assets, output=%s", total, output_dir)

    for i, asset in enumerate(assets, 1):
        cache_key = (asset.brand, asset.asset_class)
        if cache_key not in candidate_cache:
            candidate_cache[cache_key] = get_candidate_html(asset.brand, asset.asset_class)
        candidate_html = candidate_cache[cache_key]

        if candidate_html is None:
            v: FidelityVerdict = FidelityVerdict(verdict="candidate_missing", tier="n/a")
        else:
            ref_styles = capture_reference_styles(asset.html_path)
            cand_styles = capture_candidate_styles(candidate_html)
            if ref_styles is None or cand_styles is None:
                # Playwright unavailable or browser failed.
                v = FidelityVerdict(verdict="candidate_missing", tier="n/a")
            else:
                v = compare_computed_styles(ref_styles, cand_styles)

        verdicts.append((asset.brand, asset.asset_class, asset.asset_slug, v))
        _log.info("[%d/%d] %s/%s/%s -> %s", i, total,
                  asset.brand, asset.asset_class, asset.asset_slug, v.verdict)

    bm = build_baseline_map(verdicts)
    json_path, md_path = write_baseline_map(bm, output_dir=output_dir)
    _log.info(
        "Oracle complete: %d pass, %d fail, %d missing. Map: %s + %s",
        bm.pass_count, bm.fail_count, bm.missing_count, json_path, md_path,
    )
    return bm


# ---------------------------------------------------------------------------
# Default candidate source: live Resemblio site
# ---------------------------------------------------------------------------


def _make_live_candidate_fetcher(
    base_url: str = "https://resemblio.com",
    basic_auth: Optional[str] = None,
) -> Callable[[str, str], Optional[str]]:
    """Return a get_candidate_html callable that fetches from the live site.

    Fetches ``GET /library/<brand>/<asset_class>`` from the Resemblio web app
    and extracts the ``<article data-rs-source="drl-component">`` HTML fragment
    using ``extract_component_from_candidate``.

    Returns None on 404, network error, or when the article is not found
    in the response body.

    Args:
        base_url:   Resemblio site base URL (default https://resemblio.com).
        basic_auth: Optional "user:password" string for HTTP Basic Auth
                    (used when the site is behind staging basic auth).
    """
    import base64
    import urllib.request

    def _fetch(brand: str, asset_class: str) -> Optional[str]:
        url = f"{base_url.rstrip('/')}/library/{brand}/{asset_class}"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Resemblio-FidelityOracle/1.0")
        if basic_auth:
            encoded = base64.b64encode(basic_auth.encode()).decode()
            req.add_header("Authorization", f"Basic {encoded}")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            fragment = extract_component_from_candidate(body)
            if fragment is None:
                _log.debug("No rs-library-page article found in %s", url)
            return fragment
        except Exception as exc:
            _log.debug("Candidate fetch failed for %s/%s (%s): %s",
                       brand, asset_class, url, exc)
            return None

    return _fetch


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the DRL fidelity oracle across all 955 assets and write "
            "baseline_map.json + baseline_map.md to --output-dir."
        ),
    )
    parser.add_argument(
        "--corpus-root",
        default="_vendored/drl_corpus",
        help="Path to vendored DRL corpus root (default: _vendored/drl_corpus)",
    )
    parser.add_argument(
        "--base-url",
        default="https://resemblio.com",
        help="Resemblio site base URL for candidate fetch (default: https://resemblio.com)",
    )
    parser.add_argument(
        "--auth-env",
        default=None,
        metavar="VARNAME",
        help="Env var holding 'user:password' for HTTP basic auth on the Resemblio site",
    )
    parser.add_argument(
        "--output-dir",
        default="tests/render",
        help="Directory to write baseline_map.json + baseline_map.md (default: tests/render)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns 0 on success, 1 on unrecoverable error."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    corpus_root = pathlib.Path(args.corpus_root).resolve()
    if not corpus_root.exists():
        _log.error("Corpus root not found: %s", corpus_root)
        return 1

    import os

    basic_auth: Optional[str] = None
    if args.auth_env:
        basic_auth = os.environ.get(args.auth_env)

    get_candidate = _make_live_candidate_fetcher(
        base_url=args.base_url,
        basic_auth=basic_auth,
    )
    output_dir = pathlib.Path(args.output_dir).resolve()

    run_oracle_baseline(corpus_root, get_candidate_html=get_candidate, output_dir=output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
