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
    BRAND_WAIT_STRATEGY_OVERRIDES,
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


def test_resolve_census_openai_none_override_removes_cta_slot() -> None:
    """openai's None override drops the cta slot from the census entirely.

    openai was a permanent documented skip as of L4 v3 (2026-06-07): openai.com
    is gated by Cloudflare Turnstile (HTTP 403) and its CDN CSS chunks also return
    HTTP 403, so no real button tokens are reachable offline or live. The cta
    selector (formerly an href-pattern override) is retired to ``None``, identical
    to aeon, so the slot is removed and no capture is attempted. All other default
    slots are preserved in their original order.

    ADR: 02-prd/2026-06-07-openai-permanent-skip.md.
    """
    assert BRAND_SELECTOR_OVERRIDES["openai"]["cta"] is None
    census = resolve_census("openai")
    slots = [slot for _, slot in census]
    assert "cta" not in slots
    # All other default slots are preserved in their original order.
    expected = [slot for _, slot in ELEMENT_CENSUS if slot != "cta"]
    assert slots == expected


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
    """Build script with an override census embeds the override selector.

    Uses ``vercel`` (a live href-pattern override) rather than openai: openai's
    cta override was retired to ``None`` in L4 v3 (permanent skip), so it no
    longer embeds a string selector. vercel keeps a real string override and is
    the meaningful case for this contract.
    """
    override_selector = BRAND_SELECTOR_OVERRIDES["vercel"]["cta"]
    assert override_selector is not None
    census = resolve_census("vercel")
    script = build_capture_script(census)
    assert override_selector in script
    # Default cta selector is replaced, not present alongside the override.
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


# --- Per-brand wait-strategy override map -----------------------------------


def test_brand_wait_strategy_overrides_cover_known_spa_brands() -> None:
    """openai + aeon (the 22/24 button-corpus gap brands) carry overrides.

    aeon's whole DOM is behind a Vercel security checkpoint and needs
    ``networkidle`` to render before capture.

    openai was revised to ``domcontentloaded`` on 2026-06-05 (commit 562d693):
    openai.com never reaches networkidle within Playwright's 15s default;
    a graceful 2s post-goto fallback absorbs hydration instead. The override
    entry is kept (rather than removed) because the per-brand SPA selector
    override also lives in this map and both maps must stay aligned per
    ``test_brand_wait_overrides_align_with_selector_overrides``.
    """
    # 2026-06-05 correction (562d693): openai.com times out on networkidle;
    # domcontentloaded + NETWORKIDLE_WAIT_MS graceful fallback is the fix.
    assert BRAND_WAIT_STRATEGY_OVERRIDES["openai"] == "domcontentloaded"
    assert BRAND_WAIT_STRATEGY_OVERRIDES["aeon"] == "networkidle"


def test_resolve_wait_strategy_brand_override_activates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brand-overridden strategy resolves without env or explicit arg.

    openai's override is ``domcontentloaded`` (revised 2026-06-05 per
    commit 562d693; openai.com never reaches networkidle within 15s).
    The override map value is what resolve_wait_strategy must return.
    """
    monkeypatch.delenv(WAIT_STRATEGY_ENV_VAR, raising=False)
    # 2026-06-05 correction (562d693): openai override is now domcontentloaded.
    assert resolve_wait_strategy(None, brand_slug="openai") == "domcontentloaded"


def test_resolve_wait_strategy_unknown_brand_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Brands not in the override map fall through to env/default unchanged."""
    monkeypatch.delenv(WAIT_STRATEGY_ENV_VAR, raising=False)
    assert resolve_wait_strategy(None, brand_slug="acme-totally-not-spa") == DEFAULT_WAIT_STRATEGY


def test_resolve_wait_strategy_explicit_overrides_brand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit arg wins over brand override (caller intent is authoritative)."""
    monkeypatch.delenv(WAIT_STRATEGY_ENV_VAR, raising=False)
    assert (
        resolve_wait_strategy("domcontentloaded", brand_slug="openai")
        == "domcontentloaded"
    )


def test_resolve_wait_strategy_brand_override_beats_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Brand override wins over env var.

    Justification: the env var is the global escape hatch ops uses to
    flip strategy fleet-wide; the brand override is the per-brand
    SPA-hydration fact baked into the code. If an SPA brand is being
    captured under an env-set ``domcontentloaded``, the brand-specific
    knowledge of "this site does not work without hydration wait"
    should still apply.

    Uses ``aeon`` (networkidle override) rather than ``openai`` to keep
    the invariant meaningful: after 562d693 openai's override is also
    ``domcontentloaded``, making it indistinguishable from the env value
    and unable to prove precedence. aeon's override is ``networkidle``
    while the env is ``domcontentloaded`` - clear, unambiguous proof
    that the brand override wins.
    """
    monkeypatch.setenv(WAIT_STRATEGY_ENV_VAR, "domcontentloaded")
    # aeon override is networkidle; env is domcontentloaded -> override wins.
    assert resolve_wait_strategy(None, brand_slug="aeon") == "networkidle"


def test_resolve_wait_strategy_no_brand_arg_preserves_v1_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling without the new brand_slug arg matches pre-B2 semantics."""
    monkeypatch.delenv(WAIT_STRATEGY_ENV_VAR, raising=False)
    assert resolve_wait_strategy(None) == DEFAULT_WAIT_STRATEGY
    monkeypatch.setenv(WAIT_STRATEGY_ENV_VAR, "networkidle")
    assert resolve_wait_strategy(None) == "networkidle"


def test_brand_wait_overrides_use_only_valid_strategies() -> None:
    """Every entry in the wait-override map is a recognized strategy.

    Guard against a future entry typo (e.g. ``"network-idle"``) silently
    falling back to the default and re-introducing the SPA-hydration
    miss the override was supposed to fix.
    """
    from extractor.computed_styles import VALID_WAIT_STRATEGIES
    for brand, strategy in BRAND_WAIT_STRATEGY_OVERRIDES.items():
        assert strategy in VALID_WAIT_STRATEGIES, (
            f"brand={brand} has invalid wait strategy {strategy!r}"
        )


def test_brand_wait_overrides_align_with_selector_overrides() -> None:
    """Every BRAND_WAIT_STRATEGY_OVERRIDES brand also has a selector entry.

    Soft invariant: the two override maps describe the same set of
    "this brand needs special handling" brands. A wait-only entry with
    no selector override suggests an incomplete diagnosis (or an SPA
    site whose default `button, .cta, [role=button]` selector happens to
    work post-hydration, which is rare enough to warrant a comment).
    Today every entry overlaps; this test fires the canary if they drift.
    """
    wait_keys = set(BRAND_WAIT_STRATEGY_OVERRIDES.keys())
    selector_keys = set(BRAND_SELECTOR_OVERRIDES.keys())
    missing_selector = wait_keys - selector_keys
    assert missing_selector == set(), (
        f"brands have wait override but no selector override: {missing_selector}"
    )


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
