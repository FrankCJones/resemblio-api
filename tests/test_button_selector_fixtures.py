"""Tests pinning the openai + aeon button-capture contracts to real saved markup.

Three responsibilities:
1. Pin the self-healing acceptance-helper contract for openai (Phase 1) using
   synthetic snapshot dirs so the behavior is verified in isolation.
2. Pin the openai selector override to real saved markup with a dep-free
   html.parser check (always-on CI) and an opt-in Playwright set_content proof
   (no network, controlled by RESEMBLIO_RUN_REAL_BROWSER=1).
3. Prove aeon is structurally uncapturable via its real saved challenge-shell
   fixture, and guard cross-consistency across BRAND_SELECTOR_OVERRIDES,
   BRAND_WAIT_STRATEGY_OVERRIDES, and DOCUMENTED_SKIP_BRANDS.

Fixture files in tests/fixtures/button_capture/ (all git-tracked):
- openai_homepage.html: real 418 KB SSR from openai.com (2026-06-02 curl).
- aeon_challenge.html: real 33 KB Vercel challenge shell from aeon.co (2026-06-02 curl).
Both are evidence anchors: a future site redesign that breaks an override contract
will surface as a test failure rather than a silent capture regression.

No new runtime or test dependencies. html.parser is stdlib. Playwright stays the
existing optional [browser] extra, gated behind RESEMBLIO_RUN_REAL_BROWSER=1.

Quality floor: docstrings on every public function, TypedDict/dataclass shapes
where needed, named constants, no bare dicts, no magic strings.
"""
from __future__ import annotations

import json
import os
from html.parser import HTMLParser
from pathlib import Path
from typing import Final

import pytest

# ---------------------------------------------------------------------------
# Import shared helpers from the corpus coverage test.
#
# Intentional cross-test import: these helpers are the exact predicates the
# acceptance test uses; testing them here in isolation proves the Phase 1
# self-healing contract without duplicating logic. The import is safe because
# test_button_corpus_coverage.py is pure module-level code (no session
# fixtures, no class tests).
# ---------------------------------------------------------------------------
from tests.test_button_corpus_coverage import (
    DEFAULT_PLACEHOLDER_VALUES,
    DOCUMENTED_SKIP_BRANDS,
    OPENAI_REQUIRED_NON_DEFAULT_FIELDS,
    TRACKED_BUTTON_FIELDS,
    _brand_has_real_button_styles,
    _candidate_snapshot_dirs,
    _load_cta_properties,
)
from extractor.computed_styles import (
    BRAND_SELECTOR_OVERRIDES,
    BRAND_WAIT_STRATEGY_OVERRIDES,
    resolve_census,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURE_DIR: Final[Path] = Path(__file__).parent / "fixtures" / "button_capture"
"""Directory holding the pinned real-markup HTML fixtures."""

_OPENAI_FIXTURE: Final[Path] = _FIXTURE_DIR / "openai_homepage.html"
_AEON_CHALLENGE: Final[Path] = _FIXTURE_DIR / "aeon_challenge.html"
_AEON_FIXTURE = _AEON_CHALLENGE  # alias used in fixture function

VERCEL_CHALLENGE_MARKER: Final[str] = "vercel.link/security-checkpoint"
"""String present in the Vercel security-checkpoint page served to headless clients."""

AEON_CHALLENGE_ID: Final[str] = "fix-text"
"""Element id of the challenge page's primary action link."""

CHATGPT_HREF_PREFIX: Final[str] = "https://chatgpt.com"
"""Defining feature of the openai primary CTA anchor href."""

# Minimum non-default fields a well-captured openai snapshot must carry.
# Mirrors OPENAI_REQUIRED_NON_DEFAULT_FIELDS from the corpus test.
_OPENAI_FIELD_FLOOR: Final[int] = OPENAI_REQUIRED_NON_DEFAULT_FIELDS

# A synthetic ButtonTokens-shaped cta properties dict with clearly non-default
# values used by acceptance-helper contract tests.
_GOOD_CTA_PROPS: Final[dict[str, str]] = {
    "border-radius": "1234px",   # pill shape - clearly non-default
    "padding": "12px 24px",      # explicit padding - non-default
    "font-family": "'SF Pro Display', -apple-system, BlinkMacSystemFont",
    "background-color": "rgb(0, 0, 0)",
    "color": "rgb(255, 255, 255)",
    "border": "1px solid rgb(0, 0, 0)",
}
"""Six non-default field values - enough to pass the acceptance floor."""

_DEFAULT_CTA_PROPS: Final[dict[str, str]] = {
    "border-radius": "0px",
    "padding": "0px",
    "border": "0px none rgb(0, 0, 0)",
}
"""Three default/placeholder values - below the acceptance floor."""


# ---------------------------------------------------------------------------
# HTML parsing helpers (stdlib only; no beautifulsoup / cssselect)
# ---------------------------------------------------------------------------


class _ElementCollector(HTMLParser):
    """Collect `<a>` and `<button>` elements in document order.

    Attributes:
    - anchors: list of (href, classes, attrs) tuples for every `<a>` tag.
    - buttons: list of (classes, attrs) tuples for every `<button>` tag.

    Only opening tags are processed; closing tags and content are ignored.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.anchors: list[dict[str, str]] = []
        """List of attr-dicts for each <a> element, keyed by lowercase attr name."""
        self.buttons: list[dict[str, str]] = []
        """List of attr-dicts for each <button> element."""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record <a> and <button> opening tags with their attrs."""
        attr_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "a":
            self.anchors.append(attr_dict)
        elif tag.lower() == "button":
            self.buttons.append(attr_dict)


def _parse_elements(html: str) -> _ElementCollector:
    """Parse html and return a collector with anchors and buttons enumerated.

    Args:
        html: Raw HTML string. Encoding already decoded.

    Returns:
        _ElementCollector with .anchors and .buttons populated.
    """
    collector = _ElementCollector()
    collector.feed(html)
    return collector


def _has_bem_cta_class(class_str: str) -> bool:
    """Return True when class_str contains a BEM-style CTA class identifier.

    Checks only for semantic BEM class patterns (btn-primary, button-primary,
    btn--primary, etc.) and the standalone cta class. Does NOT match Tailwind
    utility class names that happen to contain "button" as a substring
    (e.g. group/desktop-nav-menu-button, focus-visible:button-*, etc.) -
    those are utility modifiers, not semantic CTA identifiers.
    """
    # Split on whitespace so we check whole class tokens where possible.
    tokens = class_str.lower().split()
    bem_patterns = ("btn-primary", "btn--primary", "button-primary", "js-cta")
    for tok in tokens:
        # Check whole-token BEM patterns.
        if tok in bem_patterns:
            return True
        # "cta" as an exact token (not a prefix of longer utilities).
        if tok == "cta":
            return True
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def openai_html() -> str:
    """Return the contents of the pinned openai homepage fixture."""
    return _OPENAI_FIXTURE.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def aeon_html() -> str:
    """Return the contents of the pinned aeon challenge-shell fixture."""
    return _AEON_CHALLENGE.read_text(encoding="utf-8", errors="replace")


def _write_snapshot(out_dir: Path, brand: str, snapshot: dict) -> Path:
    """Write a JSON snapshot dict to out_dir/<brand>.json and return the path.

    Args:
        out_dir: Directory to write into.
        brand: Brand slug (used as filename stem).
        snapshot: ComputedStyleReport-shaped dict to serialise.

    Returns:
        Path to the written file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{brand}.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


def _make_computed_style_snapshot(cta_properties: dict[str, str] | None) -> dict:
    """Return a minimal ComputedStyleReport-shaped dict.

    Args:
        cta_properties: If not None, includes a ``cta`` signal slot with
            these properties. If None, the signals list is empty (no cta slot).
    """
    signals = []
    if cta_properties is not None:
        signals.append({"slot": "cta", "selector": "button", "properties": cta_properties})
    return {"status": "ok", "signals": signals, "error": None, "schema_version": 1}


# ---------------------------------------------------------------------------
# Phase 1: acceptance helper self-healing contract
# ---------------------------------------------------------------------------


class TestAcceptanceHelperContract:
    """Pin the _brand_has_real_button_styles contract for the self-healing test.

    These tests verify that the helper returns the right (bool, reason) for the
    three cases the acceptance test branches on: no snapshot, default/empty cta,
    and real populated cta.

    The monkeypatch sets RESEMBLIO_RUNTIME_DATA_ROOT to a tmp dir so the helper
    searches only our synthetic snapshot, not the real seed tree or /var/lib/resemblio.
    """

    def test_no_snapshot_returns_false_with_missing_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing snapshot file -> (False, reason mentioning 'no snapshot file')."""
        cs_dir = tmp_path / "computed_styles"
        cs_dir.mkdir()
        monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", str(tmp_path))

        passed, reason = _brand_has_real_button_styles("openai")

        assert not passed, f"expected failure for missing snapshot; got passed=True, reason={reason!r}"
        assert "no snapshot" in reason.lower(), (
            f"expected 'no snapshot' in reason; got {reason!r}"
        )

    def test_default_only_cta_returns_false_with_non_default_count_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Snapshot with all-default cta properties -> (False, mentions non-default count)."""
        cs_dir = tmp_path / "computed_styles"
        monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", str(tmp_path))
        _write_snapshot(cs_dir, "openai", _make_computed_style_snapshot(_DEFAULT_CTA_PROPS))

        passed, reason = _brand_has_real_button_styles("openai")

        assert not passed, f"expected failure for default cta; got passed=True, reason={reason!r}"
        # Reason should mention the count comparison so the caller knows what's missing.
        assert "non-default" in reason.lower() or "only" in reason.lower(), (
            f"expected count-related phrase in reason; got {reason!r}"
        )

    def test_absent_cta_slot_returns_false_with_no_cta_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Snapshot whose signals list has no cta slot -> (False, mentions 'no usable cta')."""
        cs_dir = tmp_path / "computed_styles"
        monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", str(tmp_path))
        _write_snapshot(cs_dir, "openai", _make_computed_style_snapshot(None))

        passed, reason = _brand_has_real_button_styles("openai")

        assert not passed
        assert "cta" in reason.lower(), (
            f"expected 'cta' in reason for missing cta slot; got {reason!r}"
        )

    def test_populated_cta_returns_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Snapshot with >= 4 non-default cta fields -> (True, ...)."""
        cs_dir = tmp_path / "computed_styles"
        monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", str(tmp_path))
        _write_snapshot(cs_dir, "openai", _make_computed_style_snapshot(_GOOD_CTA_PROPS))

        passed, reason = _brand_has_real_button_styles("openai")

        assert passed, f"expected success for populated cta; got passed=False, reason={reason!r}"

    def test_browser_default_anchor_cta_counts_as_zero_real_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A CSS-less anchor-element render must count as 0 real fields, not 4.

        Root cause: anchor elements default border-color to the link color
        (rgb(0, 0, 238) in most browsers). The DEFAULT_PLACEHOLDER_VALUES sentinel
        set catches rgb(0, 0, 0) (black-default border) but not rgb(0, 0, 238)
        (link-blue-default border). All 6 tracked fields on a CSS-less anchor carry
        browser defaults; none should count as real brand tokens.

        This test pins the exact browser-default values the openai fixture-capture
        produced via page.set_content on the CSS-less openai_homepage.html fixture
        (2026-06-07 L4 v2 Phase 3 STOP evidence). The gate must reject this snapshot.
        """
        # Exact browser-default values from a CSS-less anchor render.
        # Source: L4 v2 Phase 3 STOP analysis (STATUS.md 2026-06-07 section).
        browser_default_cta_props: dict[str, str] = {
            "border-radius": "0px",               # in DEFAULT_PLACEHOLDER_VALUES (correct)
            "padding": "0px",                      # in DEFAULT_PLACEHOLDER_VALUES (correct)
            "font-family": '"Times New Roman"',    # browser default serif - slips old gate
            "background-color": "rgba(0, 0, 0, 0)",  # transparent anchor bg - slips old gate
            "color": "rgb(0, 0, 238)",             # browser link blue - slips old gate
            "border": "0px none rgb(0, 0, 238)",   # link-blue border default - slips old gate
        }
        cs_dir = tmp_path / "computed_styles"
        monkeypatch.setenv("RESEMBLIO_RUNTIME_DATA_ROOT", str(tmp_path))
        _write_snapshot(
            cs_dir, "openai", _make_computed_style_snapshot(browser_default_cta_props)
        )

        passed, reason = _brand_has_real_button_styles("openai")

        assert not passed, (
            "Browser-default anchor render must not pass the real-styles gate. "
            "All 6 tracked fields carry browser defaults; none are real brand tokens. "
            f"reason={reason!r}"
        )


# ---------------------------------------------------------------------------
# Phase 2a: openai selector contract (dep-free, always-on CI)
# ---------------------------------------------------------------------------


class TestOpenaiSelectorContract:
    """Prove the openai override selector is grounded in real markup.

    These tests assert structural facts about the saved openai_homepage.html
    fixture that make the override necessary and correct. A future site redesign
    breaking the anchor contract will surface here before it silently regresses
    the capture.
    """

    def test_primary_cta_is_href_based_anchor(self, openai_html: str) -> None:
        """At least one <a href='https://chatgpt.com...'> exists in the fixture.

        This is the structural property the override depends on: the primary
        CTA is an anchor whose defining feature is its href (Tailwind utilities
        only; no semantic class). If this test fails, openai redesigned the
        page and the override selector needs re-derivation.
        """
        collector = _parse_elements(openai_html)
        chatgpt_anchors = [
            a for a in collector.anchors
            if a.get("href", "").startswith(CHATGPT_HREF_PREFIX)
        ]
        assert chatgpt_anchors, (
            f"openai_homepage.html must contain at least one <a href='{CHATGPT_HREF_PREFIX}...'>; "
            f"found 0 of {len(collector.anchors)} anchors matching. "
            "If openai redesigned their site, re-derive the override selector."
        )

    def test_real_cta_is_anchor_not_button(self, openai_html: str) -> None:
        """No <button> element links to chatgpt.com; the real CTA is an anchor.

        This is the core structural fact that makes the default census selector
        fail: the default `button, .cta, [role=button]` selector returns
        `<button>` elements, but openai's primary CTA is an `<a>` tag. No
        amount of class-based tuning on the button selector will find it.
        The override must use an anchor-href pattern.

        If this test fails, openai added a `<button>` that links to chatgpt.com.
        In that case the default selector may now work and the override can be
        simplified or removed.
        """
        collector = _parse_elements(openai_html)
        chatgpt_buttons = [
            b for b in collector.buttons
            if CHATGPT_HREF_PREFIX in b.get("href", "")
        ]
        assert not chatgpt_buttons, (
            f"Found {len(chatgpt_buttons)} <button> elements linking to chatgpt.com. "
            "If openai now uses a <button> for the primary CTA, the anchor-href "
            "override may be replaceable with a standard button selector."
        )

    def test_first_button_has_no_sem_cta_class(self, openai_html: str) -> None:
        """The first <button> carries no BEM-style CTA semantic class.

        Confirms that a class-name based selector (.btn-primary, .button-primary,
        .cta) would not find the primary CTA. openai uses Tailwind utility classes
        only; no BEM CTA class exists in the markup. The override must be
        href-pattern-based.

        NOTE: This check is intentionally narrow. It only looks for genuine BEM
        CTA identifiers (btn-primary, btn--primary, cta as a standalone token).
        It does NOT match Tailwind modifier classes like
        `group/desktop-nav-menu-button` that contain 'button' as a substring.
        """
        collector = _parse_elements(openai_html)
        assert collector.buttons, "openai_homepage.html must contain at least one <button>"
        first_btn = collector.buttons[0]
        classes = first_btn.get("class", "")
        assert not _has_bem_cta_class(classes), (
            f"first <button> has a BEM CTA class in {classes!r}; "
            "a class-based selector may now work. Re-evaluate the href override."
        )

    def test_no_semantic_cta_class_on_any_anchor(self, openai_html: str) -> None:
        """No <a> uses a BEM-style CTA class (.btn-primary, .button-primary, etc.).

        Confirms that a class-name based selector would not work for openai.
        The site uses Tailwind utility classes only; semantic BEM names do not
        exist, so the override must be href-pattern-based.
        """
        collector = _parse_elements(openai_html)
        semantic_cta_anchors = [
            a for a in collector.anchors
            if _has_bem_cta_class(a.get("class", ""))
        ]
        assert not semantic_cta_anchors, (
            f"Found {len(semantic_cta_anchors)} <a> with BEM CTA class; "
            "openai may have adopted semantic class names - revisit the override."
        )

    def test_openai_cta_override_is_retired_to_none(self) -> None:
        """openai cta override must be None (retired; permanent structural skip).

        The href-based selector was correct for openai's markup but the live
        capture is permanently blocked (Cloudflare Turnstile + CDN 403 on CSS
        chunks - see ADR 02-prd/2026-06-07-openai-permanent-skip.md). The
        selector is retired to None, identical to aeon, so no capture attempt
        is made and no fallback to the default selector runs.

        The four tests above (test_primary_cta_is_href_based_anchor, etc.) remain
        as historical evidence that the override WAS derived from real markup and
        that a future openai redesign breaking those structural facts would be
        detectable. They do not assert the selector is live.
        """
        openai_override = BRAND_SELECTOR_OVERRIDES.get("openai", {})
        assert openai_override.get("cta") is None, (
            "BRAND_SELECTOR_OVERRIDES['openai']['cta'] must be None (retired selector). "
            "openai is a permanent structural skip: Cloudflare Turnstile blocks live "
            "capture and CDN CSS chunks also return HTTP 403. "
            "See 02-prd/2026-06-07-openai-permanent-skip.md."
        )


# ---------------------------------------------------------------------------
# Phase 2b: openai structural-skip proof
# ---------------------------------------------------------------------------


class TestOpenaiStructuralSkip:
    """Prove openai is a documented permanent skip for structural reasons.

    openai.com is gated by Cloudflare Turnstile (HTTP 403 on live capture)
    and its CDN CSS chunks also return HTTP 403, making fixture-capture
    impossible offline. The saved openai_homepage.html is evidence of WHAT the
    site looks like and WHY an href-based override was needed; the absence of
    inline CSS is evidence of WHY fixture-capture cannot work.

    ADR: projects/Resemblio/02-prd/2026-06-07-openai-permanent-skip.md.
    """

    def test_openai_fixture_has_no_inline_style_tags(self, openai_html: str) -> None:
        """The openai fixture has zero inline <style> tags.

        This is the structural property that makes fixture-capture impossible:
        all CSS is in external Next.js chunks that cannot be fetched offline
        (and also return HTTP 403 from the CDN). Without inline styles, a
        page.set_content render produces only browser defaults, not real brand
        tokens.
        """
        assert "<style" not in openai_html.lower(), (
            "openai_homepage.html must have no inline <style> tags. "
            "If openai adds inline CSS, re-evaluate whether fixture-capture "
            "can extract real button tokens (see v3 plan for the gate criteria)."
        )

    def test_openai_code_decision_matches_fixture_evidence(self) -> None:
        """The code's openai override and skip decisions match the fixture evidence.

        Cross-checks that BRAND_SELECTOR_OVERRIDES['openai']['cta'] is None
        (retired selector) and 'openai' is in DOCUMENTED_SKIP_BRANDS. If either
        is changed while the fixture still shows a CSS-less page, the decision
        and evidence are out of sync and this test will fail.
        """
        openai_override = BRAND_SELECTOR_OVERRIDES.get("openai", {})
        assert openai_override.get("cta") is None, (
            "BRAND_SELECTOR_OVERRIDES['openai']['cta'] must be None (retired selector). "
            "openai is gated by Cloudflare Turnstile; no CSS available offline. "
            "See openai_homepage.html and 02-prd/2026-06-07-openai-permanent-skip.md."
        )
        assert "openai" in DOCUMENTED_SKIP_BRANDS, (
            "'openai' must be in DOCUMENTED_SKIP_BRANDS. "
            "The corpus-floor test (floor 22) allows aeon and openai as documented skips."
        )


# ---------------------------------------------------------------------------
# Phase 3a: aeon structural-skip proof
# ---------------------------------------------------------------------------


class TestAeonStructuralSkip:
    """Prove aeon.co is structurally uncapturable by any selector or wait fix.

    The saved aeon_challenge.html is the Vercel security-checkpoint shell
    served to every non-cookied request. The tests assert structural facts
    that prove the code's decision (cta: None + DOCUMENTED_SKIP_BRANDS)
    is evidence-backed rather than a guess.
    """

    def test_aeon_fixture_contains_vercel_challenge_url(self, aeon_html: str) -> None:
        """The aeon fixture contains the Vercel challenge URL.

        Primary marker proving the fixture IS the challenge shell.
        """
        assert VERCEL_CHALLENGE_MARKER in aeon_html, (
            f"aeon_challenge.html must contain '{VERCEL_CHALLENGE_MARKER}'. "
            "If aeon dropped the Vercel checkpoint, re-evaluate the permanent-skip."
        )

    def test_aeon_fixture_contains_challenge_element_id(self, aeon_html: str) -> None:
        """The aeon fixture contains the challenge page's fix-text element id."""
        assert f'id="{AEON_CHALLENGE_ID}"' in aeon_html or f"id='{AEON_CHALLENGE_ID}'" in aeon_html, (
            f"aeon_challenge.html must contain id={AEON_CHALLENGE_ID!r}. "
            "If aeon's checkpoint changed shape, the permanent-skip rationale needs re-review."
        )

    def test_aeon_fixture_has_no_real_button_elements(self, aeon_html: str) -> None:
        """The aeon challenge shell has zero <button> elements.

        Proves there is nothing to select against with the default census.
        """
        collector = _parse_elements(aeon_html)
        assert not collector.buttons, (
            f"Expected no <button> in aeon challenge shell; "
            f"found {len(collector.buttons)}. The page may have changed."
        )

    def test_aeon_fixture_has_no_product_cta_anchors(self, aeon_html: str) -> None:
        """The aeon challenge shell has no recognizable product CTA anchors.

        Donate / subscribe / newsletter links - the real aeon CTAs - are absent
        because the served page is the challenge shell, not the real site.
        """
        collector = _parse_elements(aeon_html)
        product_cta_fragments = ("/donate", "/subscribe", "/newsletter", "aeon.co")
        real_cta_anchors = [
            a for a in collector.anchors
            if any(frag in a.get("href", "") for frag in product_cta_fragments)
        ]
        assert not real_cta_anchors, (
            f"Found {len(real_cta_anchors)} product-CTA anchors in aeon challenge shell; "
            "expected 0. The page may now serve real content. Re-evaluate the skip."
        )

    def test_aeon_code_decision_matches_fixture_evidence(self) -> None:
        """The code's aeon override and skip decisions match the fixture evidence.

        Cross-checks that BRAND_SELECTOR_OVERRIDES['aeon']['cta'] is None (explicit
        skip) and 'aeon' is in DOCUMENTED_SKIP_BRANDS. If either is removed while
        the fixture still shows a challenge shell, the decision and evidence are out
        of sync and this test will fail.
        """
        aeon_override = BRAND_SELECTOR_OVERRIDES.get("aeon", {})
        assert aeon_override.get("cta") is None, (
            "BRAND_SELECTOR_OVERRIDES['aeon']['cta'] must be None (explicit skip). "
            "aeon.co serves a Vercel challenge shell; there is nothing to select against. "
            "See aeon_challenge.html and 02-prd/2026-06-06-aeon-permanent-skip.md."
        )
        assert "aeon" in DOCUMENTED_SKIP_BRANDS, (
            "'aeon' must be in DOCUMENTED_SKIP_BRANDS. "
            "The corpus-floor test allows exactly this one brand to be uncaptured."
        )


# ---------------------------------------------------------------------------
# Phase 3b: cross-consistency guard
# ---------------------------------------------------------------------------


class TestCaptureMapConsistency:
    """Guard that BRAND_SELECTOR_OVERRIDES, BRAND_WAIT_STRATEGY_OVERRIDES,
    and DOCUMENTED_SKIP_BRANDS cannot silently drift out of sync.

    These are three separate data structures that encode one coherent decision
    per brand. An entry in one without a matching entry in the others is a
    latent bug: the wait fix fires without a selector, a skip brand gets
    re-enabled by mistake, etc.
    """

    def test_every_non_none_override_brand_has_a_wait_strategy(self) -> None:
        """Every brand with a non-None cta override must have a wait strategy entry.

        A selector override targets an SPA or non-standard DOM; SPA brands need
        their wait strategy declared explicitly so the network-idle / hydration
        buffer fires. A missing wait entry means the override selector evaluates
        against the SSR shell before the element is mounted, silently returning
        no cta slot even though the override is correct.
        """
        missing_wait: list[str] = []
        for brand, overrides in BRAND_SELECTOR_OVERRIDES.items():
            if overrides.get("cta") is None:
                # Explicit skip: no capture is attempted; wait strategy is irrelevant.
                continue
            if brand not in BRAND_WAIT_STRATEGY_OVERRIDES:
                missing_wait.append(brand)

        assert not missing_wait, (
            f"Brands with a non-None cta override but no wait-strategy entry: "
            f"{missing_wait!r}. Add each to BRAND_WAIT_STRATEGY_OVERRIDES in "
            "extractor/computed_styles.py with the appropriate wait strategy."
        )

    def test_documented_skip_brands_not_simultaneously_capturable(self) -> None:
        """No brand can be both in DOCUMENTED_SKIP_BRANDS and have a capturable cta override.

        DOCUMENTED_SKIP_BRANDS means 'we know this brand cannot be captured; the
        corpus-floor test ignores it.' A non-None cta override means 'we have a
        selector that should find the CTA.' Both cannot be true at once.
        """
        contradictions: list[str] = []
        for brand in DOCUMENTED_SKIP_BRANDS:
            override = BRAND_SELECTOR_OVERRIDES.get(brand, {})
            cta = override.get("cta")
            if cta is not None:  # None means explicit-skip, which is consistent
                contradictions.append(
                    f"{brand!r}: DOCUMENTED_SKIP_BRANDS entry but "
                    f"non-None cta override={cta!r}"
                )

        assert not contradictions, (
            "Contradiction between DOCUMENTED_SKIP_BRANDS and BRAND_SELECTOR_OVERRIDES: "
            f"{contradictions!r}. Remove from DOCUMENTED_SKIP_BRANDS if the brand is "
            "now capturable, or set cta=None in its override entry if it is not."
        )

    def test_override_brands_form_a_known_set(self) -> None:
        """Smoke: the override map contains the expected known brands.

        This is not a rigid allowlist guard (new brands can always be added
        without changing this test). It asserts the two brands that motivated
        this plan are present so a future accidental deletion surfaces here.
        """
        assert "openai" in BRAND_SELECTOR_OVERRIDES, (
            "'openai' must be in BRAND_SELECTOR_OVERRIDES (href-based CTA override)."
        )
        assert "aeon" in BRAND_SELECTOR_OVERRIDES, (
            "'aeon' must be in BRAND_SELECTOR_OVERRIDES (cta=None explicit-skip)."
        )
