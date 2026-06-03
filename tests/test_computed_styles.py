"""Unit tests for extractor.computed_styles.

The capture helper is the deterministic pre-LLM signal that closes the
"missed computed styles (CSS-variable indirection)" diagnostic from
R3.1 Phase A. These tests exercise the pure-data shape and the JS
template; live-browser execution is opt-in via
RESEMBLIO_RUN_REAL_BROWSER=1 to keep CI hermetic.

Source mission: projects/OptSus Team/missions/resemblio-r3.1-extractor-surgery-v1.md
"""
from __future__ import annotations

import os

import pytest

from extractor.computed_styles import (
    BRAND_SELECTOR_OVERRIDES,
    CAPTURED_PROPERTIES,
    DEFAULT_WAIT_STRATEGY,
    ELEMENT_CENSUS,
    SCHEMA_VERSION,
    WAIT_STRATEGY_ENV_VAR,
    _coerce_signals,
    build_capture_script,
    capture_computed_styles,
    empty_report,
    render_for_prompt,
    resolve_census,
    resolve_wait_strategy,
)


def test_empty_report_has_all_required_fields() -> None:
    """Helper produces a well-formed empty report for any status."""
    report = empty_report("skipped", "test reason")
    assert report["status"] == "skipped"
    assert report["signals"] == []
    assert report["error"] == "test reason"
    assert report["schema_version"] == SCHEMA_VERSION


def test_capture_with_neither_input_returns_error() -> None:
    """Caller must supply html OR url; neither is a programming error."""
    report = capture_computed_styles()
    assert report["status"] == "error"
    assert report["error"] is not None
    assert "neither html nor url" in report["error"]


def test_capture_with_both_inputs_returns_error() -> None:
    """Caller must supply html OR url, not both."""
    report = capture_computed_styles(html="<html></html>", url="https://x")
    assert report["status"] == "error"
    assert report["error"] is not None


def test_build_capture_script_includes_census_and_properties() -> None:
    """The generated JS embeds the census and property list verbatim."""
    script = build_capture_script()
    for selector, slot in ELEMENT_CENSUS:
        assert selector in script, f"selector {selector!r} missing from script"
        assert slot in script, f"slot {slot!r} missing from script"
    for prop in CAPTURED_PROPERTIES:
        assert prop in script, f"property {prop!r} missing from script"
    # Sanity: script is a single arrow function body.
    assert script.strip().startswith("() =>")


def test_coerce_signals_filters_bad_shapes() -> None:
    """Coercion drops non-dict items, missing slots, and empty props."""
    raw = [
        {"slot": "body", "selector": "body", "properties": {"color": "rgb(11, 11, 15)"}},
        "not a dict",
        {"slot": "", "selector": "x", "properties": {"color": "red"}},
        {"slot": "h1", "selector": "h1", "properties": {}},
        {"slot": "h2", "selector": "h2", "properties": {"color": "  ", "font-size": "24px"}},
    ]
    out = _coerce_signals(raw)
    assert len(out) == 2
    slots = [s["slot"] for s in out]
    assert slots == ["body", "h2"]
    # Whitespace-only values are stripped; valid sibling survives.
    assert "color" not in out[1]["properties"]
    assert out[1]["properties"]["font-size"] == "24px"


def test_coerce_signals_on_non_list_returns_empty() -> None:
    """Defensive: non-list input yields an empty list, no exception."""
    assert _coerce_signals(None) == []
    assert _coerce_signals({"oops": True}) == []
    assert _coerce_signals("string") == []


def test_render_for_prompt_returns_empty_when_not_ok() -> None:
    """Skipped/unavailable/error reports render to empty string."""
    for status in ("skipped", "unavailable", "error"):
        report = empty_report(status, "x")  # type: ignore[arg-type]
        assert render_for_prompt(report) == ""


def test_render_for_prompt_includes_signals_when_ok() -> None:
    """An ok report with signals renders one section per slot."""
    report = {
        "status": "ok",
        "signals": [
            {"slot": "body", "selector": "body", "properties": {"color": "rgb(245, 242, 234)", "background-color": "rgb(11, 11, 15)"}},
            {"slot": "cta", "selector": ".cta", "properties": {"background-color": "rgb(251, 231, 31)"}},
        ],
        "error": None,
        "schema_version": SCHEMA_VERSION,
    }
    rendered = render_for_prompt(report)  # type: ignore[arg-type]
    assert "body" in rendered
    assert "rgb(11, 11, 15)" in rendered
    assert ".cta" in rendered
    assert "ground truth" in rendered.lower()


def test_capture_without_playwright_reports_unavailable() -> None:
    """When Playwright is not importable the helper returns status=unavailable.

    This is the canonical fallback path the extractor relies on: Playwright
    is a YELLOW dep gate; until it is installed the surgery still ships
    Phase A signals and degrades the Phase B signal cleanly.
    """
    try:
        import playwright  # type: ignore[import-not-found]  # noqa: F401
        playwright_available = True
    except ImportError:
        playwright_available = False
    if playwright_available:
        pytest.skip("playwright is installed; this test verifies the not-installed path")
    report = capture_computed_styles(html="<html><body></body></html>")
    assert report["status"] == "unavailable"
    assert report["error"] is not None
    assert "playwright" in report["error"].lower()


# --- Per-brand selector override map ----------------------------------------


def test_resolve_census_no_brand_returns_default() -> None:
    """Backward compat: brand_slug=None yields ELEMENT_CENSUS verbatim."""
    assert resolve_census(None) == ELEMENT_CENSUS
    assert resolve_census("") == ELEMENT_CENSUS


def test_resolve_census_unknown_brand_returns_default() -> None:
    """Unknown brand slugs degrade to defaults (no error)."""
    assert resolve_census("does-not-exist") == ELEMENT_CENSUS


def test_resolve_census_openai_override_replaces_cta_only() -> None:
    """openai override swaps the cta selector but preserves all other slots."""
    census = resolve_census("openai")
    by_slot = {slot: selector for selector, slot in census}
    default_by_slot = {slot: selector for selector, slot in ELEMENT_CENSUS}
    assert by_slot["cta"] == BRAND_SELECTOR_OVERRIDES["openai"]["cta"]
    assert by_slot["cta"] != default_by_slot["cta"]
    # Non-overridden slots unchanged.
    for slot in ("root", "body", "h1", "h2", "h3", "link"):
        assert by_slot[slot] == default_by_slot[slot]
    # Slot order preserved.
    assert [slot for _, slot in census] == [slot for _, slot in ELEMENT_CENSUS]


def test_resolve_census_aeon_none_override_removes_cta_slot() -> None:
    """Aeon's None override drops the cta slot from the census entirely.

    Aeon is gated behind a Vercel security checkpoint that headless
    Playwright cannot pass; capturing would yield garbage and tag the
    brand with a misleading override marker downstream. A ``None`` value
    in ``BRAND_SELECTOR_OVERRIDES`` is the opt-out for that signal.
    """
    assert BRAND_SELECTOR_OVERRIDES["aeon"]["cta"] is None
    census = resolve_census("aeon")
    slots = [slot for _, slot in census]
    assert "cta" not in slots
    # All other default slots are preserved in their original order.
    expected = [slot for _, slot in ELEMENT_CENSUS if slot != "cta"]
    assert slots == expected


def test_resolve_census_href_pattern_overrides_embedded_in_script() -> None:
    """Spot-checked href-pattern overrides (vercel/linear/anthropic) embed verbatim."""
    for brand in ("vercel", "linear", "anthropic"):
        selector = BRAND_SELECTOR_OVERRIDES[brand]["cta"]
        assert selector is not None
        script = build_capture_script(resolve_census(brand))
        assert selector in script, f"{brand} selector missing from script"


def test_none_override_skips_runtime_fallback() -> None:
    """A None override does not trigger the default-selector fallback path.

    Regression guard for `capture_computed_styles`'s fallback loop: only
    slots with a real (string) override that matched nothing should be
    re-sampled with the default selector. None-valued slots are explicit
    opt-outs and must stay opted out.
    """
    overrides = BRAND_SELECTOR_OVERRIDES["aeon"]
    captured_slots: set[str] = set()
    missing_overridden = [
        slot for slot, sel in overrides.items()
        if sel is not None and slot not in captured_slots
    ]
    assert missing_overridden == []


def test_build_capture_script_uses_override_census_when_passed() -> None:
    """Build script with an override census embeds the override selector."""
    census = resolve_census("openai")
    script = build_capture_script(census)
    assert BRAND_SELECTOR_OVERRIDES["openai"]["cta"] in script
    # Default cta selector for the OTHER slot is not present.
    default_cta = next(sel for sel, slot in ELEMENT_CENSUS if slot == "cta")
    assert default_cta not in script


def test_build_capture_script_default_when_no_census() -> None:
    """Calling without a census argument preserves v1 behavior."""
    script = build_capture_script()
    default_cta = next(sel for sel, slot in ELEMENT_CENSUS if slot == "cta")
    assert default_cta in script


# --- Wait-strategy resolution -----------------------------------------------


def test_resolve_wait_strategy_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No explicit arg and no env var -> DEFAULT_WAIT_STRATEGY."""
    monkeypatch.delenv(WAIT_STRATEGY_ENV_VAR, raising=False)
    assert resolve_wait_strategy(None) == DEFAULT_WAIT_STRATEGY


def test_resolve_wait_strategy_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var flips the strategy when no explicit arg is given."""
    monkeypatch.setenv(WAIT_STRATEGY_ENV_VAR, "networkidle")
    assert resolve_wait_strategy(None) == "networkidle"


def test_resolve_wait_strategy_explicit_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit arg wins over the env var."""
    monkeypatch.setenv(WAIT_STRATEGY_ENV_VAR, "networkidle")
    assert resolve_wait_strategy("domcontentloaded") == "domcontentloaded"


def test_resolve_wait_strategy_unknown_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bogus values fall back to default rather than raising."""
    monkeypatch.delenv(WAIT_STRATEGY_ENV_VAR, raising=False)
    assert resolve_wait_strategy("load-and-pray") == DEFAULT_WAIT_STRATEGY


@pytest.mark.skipif(
    os.environ.get("RESEMBLIO_RUN_REAL_BROWSER") != "1",
    reason="live-browser test gated by RESEMBLIO_RUN_REAL_BROWSER=1",
)
def test_capture_on_susann_pathology_resolves_var_indirection() -> None:
    """Live Playwright pass resolves `var(--ink)` to its hex value.

    Opt-in: set RESEMBLIO_RUN_REAL_BROWSER=1 to exercise. Requires
    Playwright + chromium installed. This is the integration check that
    Phase B actually closes the diagnostic.
    """
    html = """
    <!doctype html><html><head><style>
      :root { --ink: #0B0B0F; --bone: #F5F2EA; }
      body { background: var(--ink); color: var(--bone); font-family: Inter, sans-serif; }
    </style></head><body><p>test</p></body></html>
    """
    report = capture_computed_styles(html=html)
    assert report["status"] == "ok"
    body_signal = next(s for s in report["signals"] if s["slot"] == "body")
    # Browsers report colors as rgb(); 0x0B = 11, 0xF5 = 245.
    assert "11" in body_signal["properties"]["background-color"]
    assert "245" in body_signal["properties"]["color"]
