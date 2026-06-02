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
    CAPTURED_PROPERTIES,
    ELEMENT_CENSUS,
    SCHEMA_VERSION,
    _coerce_signals,
    build_capture_script,
    capture_computed_styles,
    empty_report,
    render_for_prompt,
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
