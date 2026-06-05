"""Capture computed CSS values via Playwright before the LLM call.

The Resemblio extractor previously sent raw HTML + inline `<style>` to
Sonnet and asked it to resolve CSS custom-property indirection
(`var(--ink)` -> `#0B0B0F`) by reasoning. On the Susann pathology the
LLM gave up and returned a safe-default `#f5f5f5` that appeared nowhere
in the source. This module is the deterministic pre-LLM pass that closes
the "missed computed styles (CSS-variable indirection)" diagnostic class
from the R3.1 Phase A probe.

It renders the page in a headless browser, walks a small element census
(`html`, `body`, `h1`, `h2`, `h3`, `a`, `button`, `.cta`), captures the
resolved `color`, `background-color`, `font-family`, `font-size`, and
`padding` values for each, and returns a structured `ComputedStyleReport`
the extractor passes to the LLM as ground-truth signal.

Graceful degradation: if Playwright is not installed (or its browser
binaries are missing), the helper returns a report with
`status="unavailable"` and an empty `signals` list. The extractor sets
`confidence_signals.computed_style_pass="skipped"` and continues with
raw-HTML-only reasoning. This keeps the surgery shippable behind a
YELLOW dep approval; once Playwright is approved and installed in the
extraction-path runtime, the same code activates with no further change.

Throwaway: NO. Quality floor applies. Tests in
tests/test_computed_styles.py exercise the pure-data report shape and
the script template; live-browser execution is opt-in via
RESEMBLIO_RUN_REAL_BROWSER=1.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal, TypedDict

LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Per-brand selector override map.
#
# Contract: brand_slug -> { signal_name -> CSS selector }.
# When `capture_computed_styles` is called with `brand_slug=<slug>`, any
# slot listed in this map is sampled via the brand-specific selector
# INSTEAD OF the default in ELEMENT_CENSUS for that one capture run.
#
# If the override selector matches no element on the page, the capture
# pass falls back to the default selector for that slot and a warning is
# logged. This keeps overrides surgical and reversible: adding a brand
# entry never regresses other brands, and a stale override degrades to
# default behavior rather than dropping the slot silently.
#
# Diagnosis trail: openai.com's first-`<button>` is a nav icon stub;
# aeon.co renders zero `<button>` elements until React hydrates. The
# generic `button, .cta, [role=button]` selector therefore captures
# garbage or nothing. Per-brand entries below name a real CTA selector
# discovered on each site. See
# `_handoff/inbox/claude/2026-06-02-openai-aeon-capture-diagnosis.md`.
BRAND_SELECTOR_OVERRIDES: dict[str, dict[str, str | None]] = {
    "openai": {"cta": "a[href^='https://chatgpt.com'], header a[href*='chatgpt.com']"},
    "aeon": {"cta": None},  # Vercel security-checkpoint gated; capture not possible headlessly
    # spot-check confirmed broader brands need href-pattern overrides too:
    "vercel": {"cta": "a[href*='vercel.com/signup'], header a[href*='/new'], main a.button"},
    "linear": {"cta": "a[href*='linear.app/sign-up'], header a[href*='signup'], main a[href*='/launch']"},
    "anthropic": {"cta": "a[href*='claude.ai'], header a[href*='claude.ai']"},
}
"""brand_slug -> { signal_name -> selector or None } overrides, consulted before defaults.

A value of ``None`` means "skip this slot entirely for this brand": the slot is
removed from the census, no capture attempt is made, and no fallback to the
default selector runs. Use ``None`` when the site cannot be captured for that
signal (e.g. Vercel security-checkpoint gating) so the override layer does not
inject a misleading marker downstream.
"""

# Wait-strategy controls for SPA-hydration tolerance.
#
# `domcontentloaded` was the v1 default and is preserved for backward
# compatibility. Modern marketing sites (aeon, vercel, linear, etc.)
# render their primary CTA client-side, so the capture script ran before
# the element existed. `networkidle` adds a real wait for the network to
# settle plus an explicit hydration buffer, closing that whole class of
# misses at the cost of <3s per capture (well inside DEFAULT_TIMEOUT_MS).
#
# Selection precedence: explicit `wait_strategy=` arg > env var
# RESEMBLIO_CAPTURE_WAIT_STRATEGY > module default.
WAIT_STRATEGY_ENV_VAR = "RESEMBLIO_CAPTURE_WAIT_STRATEGY"
"""Env var the capture script reads when no explicit strategy is passed."""

VALID_WAIT_STRATEGIES: tuple[str, ...] = ("domcontentloaded", "networkidle")
"""Wait-until values we accept; anything else falls back to the default."""

DEFAULT_WAIT_STRATEGY: Literal["domcontentloaded", "networkidle"] = "domcontentloaded"
"""Module-level default. Pass `wait_strategy='networkidle'` or set the env var to switch."""

# Per-brand wait-strategy override map.
#
# Some brands cannot be captured under `domcontentloaded` because their
# primary CTA (and often the rest of their meaningful DOM) only exists
# after React/Next/Vue hydration. The override layer above carries the
# selector half of the fix; this map carries the wait half. Without the
# wait, the override selector evaluates against an SSR shell that does
# not contain the element yet and the per-slot fallback runs against
# the default selector on the same un-hydrated DOM (also nothing).
#
# Precedence (decided in `resolve_wait_strategy`):
#   explicit arg > brand-override > env var > module default
#
# Surgical + reversible: an entry here cannot regress non-SPA brands
# (they never look up the map). The networkidle path adds <3s per
# capture, well inside DEFAULT_TIMEOUT_MS. Diagnosis trail in
# `_handoff/inbox/claude/2026-06-02-openai-aeon-capture-diagnosis.md`
# and `_handoff/inbox/claude/2026-06-02-openai-aeon-selector-revision.md`.
#
# Entries here apply even when the brand's selector override is `None`
# (the explicit-skip case). The wait shim is harmless for skipped slots
# and keeps the per-brand contract internally consistent: an SPA brand
# is an SPA brand whether or not we capture its CTA.
BRAND_WAIT_STRATEGY_OVERRIDES: dict[str, Literal["domcontentloaded", "networkidle"]] = {
    "openai": "domcontentloaded",   # 2026-06-05: openai.com never reaches networkidle within Playwright's 15s default; graceful 2s post-goto NETWORKIDLE_WAIT_MS fallback absorbs hydration
    "aeon": "networkidle",     # Vercel-gated; networkidle is harmless and keeps contract consistent
    "vercel": "networkidle",   # spot-checked: hero CTA hydrates client-side (Tailwind utility shell)
    "linear": "networkidle",   # spot-checked: same pattern as vercel
    "anthropic": "networkidle",  # spot-checked: same pattern
}
"""brand_slug -> wait strategy. Closes the SPA-hydration class for the
modern marketing-site brands whose primary CTA is not in the SSR HTML."""

HYDRATION_BUFFER_MS = 1_000
"""Extra wait after `networkidle` settles, to absorb late React mounts."""

NETWORKIDLE_WAIT_MS = 2_000
"""Explicit `wait_for_load_state('networkidle')` timeout (after the initial nav)."""

# Element census kept short on purpose. Each entry: a CSS selector and a
# stable slot name the LLM can correlate with TokenSet keys.
ELEMENT_CENSUS: tuple[tuple[str, str], ...] = (
    ("html", "root"),
    ("body", "body"),
    ("h1", "h1"),
    ("h2", "h2"),
    ("h3", "h3"),
    ("a", "link"),
    ("button, .cta, [role=button]", "cta"),
)
"""(selector, slot) pairs sampled for computed-style capture."""

# Properties captured per element. Order matches the slots the LLM cares
# about: color first because it drives every color slot in the TokenSet.
CAPTURED_PROPERTIES: tuple[str, ...] = (
    "color",
    "background-color",
    "font-family",
    "font-size",
    "font-weight",
    "line-height",
    "letter-spacing",
    "padding",
    "border-radius",
    "border",
)
"""CSS properties pulled from `window.getComputedStyle()` per element."""

# Hard timeout for the whole render+capture step. Brief Section 9 caps
# the extraction-path latency budget; we fall back to skipped on timeout.
DEFAULT_TIMEOUT_MS = 8_000
"""Hard timeout for the entire render + computed-style capture step."""


class ComputedSignal(TypedDict):
    """One element's computed-style snapshot.

    Fields:
    - slot: stable role name ("body", "h1", "cta", ...) matching
      ELEMENT_CENSUS.
    - selector: the CSS selector used to find the element.
    - properties: map of CSS property -> resolved value as the browser
      reports it (e.g. "rgb(11, 11, 15)" for `--ink`).
    """

    slot: str
    selector: str
    properties: dict[str, str]


class ComputedStyleReport(TypedDict):
    """Aggregate output of `capture_computed_styles`.

    Fields:
    - status: "ok" when the browser ran and we have signals;
      "unavailable" when Playwright is not installed or its browser
      binaries are missing; "error" for runtime failures (timeout,
      navigation failure); "skipped" when the caller opted out.
    - signals: per-element computed values. Empty unless status="ok".
    - error: short human-readable failure summary, or None.
    - schema_version: bumped if the shape changes.
    """

    status: Literal["ok", "unavailable", "error", "skipped"]
    signals: list[ComputedSignal]
    error: str | None
    schema_version: int


def empty_report(status: Literal["ok", "unavailable", "error", "skipped"], error: str | None = None) -> ComputedStyleReport:
    """Return a well-formed empty report for the given status."""
    return ComputedStyleReport(
        status=status,
        signals=[],
        error=error,
        schema_version=SCHEMA_VERSION,
    )


def resolve_census(brand_slug: str | None) -> tuple[tuple[str, str], ...]:
    """Return the (selector, slot) census for one capture run.

    Behavior:
    - When ``brand_slug`` is None or unknown, returns ``ELEMENT_CENSUS``
      unchanged (the v1 default behavior; zero regression risk).
    - When ``brand_slug`` is in ``BRAND_SELECTOR_OVERRIDES``, returns a
      census where any slot listed in the override map with a string
      selector uses the brand's selector; every other slot keeps the
      default. Slot order is preserved so the LLM-prompt rendering stays
      stable.
    - When an override slot maps to ``None``, the slot is REMOVED from
      the census entirely for this brand. No capture is attempted and no
      fallback to the default selector runs. A debug line is logged so
      the omission is auditable.

    No on-miss fallback is encoded here; that is a runtime decision
    handled in ``capture_computed_styles`` after the JS evaluates, so we
    can detect "override matched nothing" and re-try with the default.
    """
    if not brand_slug:
        return ELEMENT_CENSUS
    overrides = BRAND_SELECTOR_OVERRIDES.get(brand_slug)
    if not overrides:
        return ELEMENT_CENSUS
    out: list[tuple[str, str]] = []
    for selector, slot in ELEMENT_CENSUS:
        if slot in overrides:
            override_selector = overrides[slot]
            if override_selector is None:
                LOG.info("brand=%s slot=%s skipped per override (None)", brand_slug, slot)
                continue
            out.append((override_selector, slot))
        else:
            out.append((selector, slot))
    return tuple(out)


def build_capture_script(census: tuple[tuple[str, str], ...] | None = None) -> str:
    """Return the JS that captures computed styles for every census element.

    The script returns a JSON-serialisable array of `{slot, selector, properties}`
    objects. We build the script in Python so the property list and census
    stay synchronised and easy to extend; the browser-side code is small,
    deterministic, and side-effect-free.

    When ``census`` is None, ELEMENT_CENSUS is used (backward compat).
    Brand-override callers pass the resolved census from
    ``resolve_census(brand_slug)``.
    """
    effective_census = census if census is not None else ELEMENT_CENSUS
    census_json = json.dumps([{"selector": sel, "slot": slot} for sel, slot in effective_census])
    properties_json = json.dumps(list(CAPTURED_PROPERTIES))
    return (
        "() => {\n"
        f"  const census = {census_json};\n"
        f"  const properties = {properties_json};\n"
        "  const out = [];\n"
        "  for (const {selector, slot} of census) {\n"
        "    const el = document.querySelector(selector);\n"
        "    if (!el) continue;\n"
        "    const cs = window.getComputedStyle(el);\n"
        "    const props = {};\n"
        "    for (const name of properties) {\n"
        "      const v = cs.getPropertyValue(name);\n"
        "      if (v && v.trim()) props[name] = v.trim();\n"
        "    }\n"
        "    out.push({slot, selector, properties: props});\n"
        "  }\n"
        "  return out;\n"
        "}"
    )


def resolve_wait_strategy(
    explicit: str | None,
    brand_slug: str | None = None,
) -> Literal["domcontentloaded", "networkidle"]:
    """Pick the wait-until value for one capture run.

    Precedence: explicit arg > ``BRAND_WAIT_STRATEGY_OVERRIDES[brand_slug]``
    > ``RESEMBLIO_CAPTURE_WAIT_STRATEGY`` env var > ``DEFAULT_WAIT_STRATEGY``.

    The brand-override slot sits between explicit and env so prod can
    still globally flip strategy via the env var when needed, but the
    per-brand SPA-hydration fix activates automatically on every capture
    of a known-SPA brand without requiring a sidecar env var. Unknown
    values at any layer fall back to the default and emit a warning
    rather than failing the capture.
    """
    brand_override: str | None = None
    if brand_slug:
        brand_override = BRAND_WAIT_STRATEGY_OVERRIDES.get(brand_slug)
    candidate = (
        explicit
        or brand_override
        or os.environ.get(WAIT_STRATEGY_ENV_VAR)
        or DEFAULT_WAIT_STRATEGY
    )
    if candidate not in VALID_WAIT_STRATEGIES:
        LOG.warning(
            "unknown wait_strategy=%r; falling back to %s", candidate, DEFAULT_WAIT_STRATEGY
        )
        return DEFAULT_WAIT_STRATEGY
    return candidate  # type: ignore[return-value]


def capture_computed_styles(
    html: str | None = None,
    url: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    brand_slug: str | None = None,
    wait_strategy: str | None = None,
) -> ComputedStyleReport:
    """Render `html` (or navigate to `url`) and capture computed styles.

    Exactly one of `html` or `url` must be provided. `html` is preferred
    for the extraction path because the extractor has already fetched
    the body and we avoid a second network round-trip.

    Args:
        html: Rendered HTML to load via ``page.set_content``. Mutually
            exclusive with ``url``.
        url: URL to navigate to via ``page.goto``. Mutually exclusive
            with ``html``.
        timeout_ms: Hard timeout for the whole render+capture step.
        brand_slug: When set and present in ``BRAND_SELECTOR_OVERRIDES``,
            the override map's per-slot selectors replace the defaults
            in ``ELEMENT_CENSUS`` for this single run. If the override
            matches no element, a second pass runs with the default
            selector for that slot and a warning is logged. Unknown
            slugs are treated as None (no overrides).
        wait_strategy: ``"domcontentloaded"`` or ``"networkidle"``. When
            ``"networkidle"``, the capture additionally calls
            ``wait_for_load_state("networkidle")`` (up to
            ``NETWORKIDLE_WAIT_MS``) and a ``HYDRATION_BUFFER_MS``
            timeout, to absorb SPA hydration. Precedence resolved by
            ``resolve_wait_strategy``: explicit arg >
            ``BRAND_WAIT_STRATEGY_OVERRIDES[brand_slug]`` > env var
            ``RESEMBLIO_CAPTURE_WAIT_STRATEGY`` > ``DEFAULT_WAIT_STRATEGY``.

    Returns:
        - status="ok" with populated signals on success
        - status="unavailable" when Playwright is not importable or its
          Chromium binary is missing
        - status="error" with a short error message on runtime failure
          (timeout, navigation failure, JS exception)

    Never raises. The caller treats any non-"ok" status as "use
    raw-HTML-only mode" and records the skip in confidence_signals.
    """
    if html is None and url is None:
        return empty_report("error", "neither html nor url supplied")
    if html is not None and url is not None:
        return empty_report("error", "supply html OR url, not both")

    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError:
        return empty_report("unavailable", "playwright is not installed in this runtime")

    try:
        from playwright.sync_api import Error as PlaywrightError  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - sync_api always ships Error
        PlaywrightError = Exception  # type: ignore[assignment,misc]

    effective_wait = resolve_wait_strategy(wait_strategy, brand_slug)
    primary_census = resolve_census(brand_slug)
    primary_script = build_capture_script(primary_census)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                if html is not None:
                    page.set_content(html, wait_until=effective_wait)
                else:
                    page.goto(url or "", wait_until=effective_wait)
                if effective_wait == "networkidle":
                    # Belt-and-braces: a second explicit wait closes
                    # late-hydrating SPAs that satisfy `goto`'s
                    # networkidle before the React tree has mounted.
                    try:
                        page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_WAIT_MS)
                    except PlaywrightError as exc:
                        LOG.debug("networkidle wait timed out: %s", exc)
                    page.wait_for_timeout(HYDRATION_BUFFER_MS)
                raw = page.evaluate(primary_script)
                # Per-slot fallback: any override slot whose primary
                # selector matched nothing gets re-sampled with the
                # default selector, and a warning is logged. This keeps
                # stale brand overrides from regressing capture below
                # the v1 baseline.
                if brand_slug and brand_slug in BRAND_SELECTOR_OVERRIDES:
                    primary_signals = _coerce_signals(raw)
                    captured_slots = {s["slot"] for s in primary_signals}
                    overrides = BRAND_SELECTOR_OVERRIDES[brand_slug]
                    # Skip None-valued slots: those are explicit "do not
                    # capture this signal for this brand" markers. The
                    # default-selector fallback would defeat the opt-out.
                    missing_overridden = [
                        slot for slot, sel in overrides.items()
                        if sel is not None and slot not in captured_slots
                    ]
                    if missing_overridden:
                        fallback_census = tuple(
                            (selector, slot)
                            for selector, slot in ELEMENT_CENSUS
                            if slot in missing_overridden
                        )
                        if fallback_census:
                            LOG.warning(
                                "brand=%s override slots matched nothing; falling back to defaults: %s",
                                brand_slug,
                                ",".join(missing_overridden),
                            )
                            fallback_script = build_capture_script(fallback_census)
                            fallback_raw = page.evaluate(fallback_script)
                            fallback_signals = _coerce_signals(fallback_raw)
                            raw = list(primary_signals) + list(fallback_signals)
            finally:
                browser.close()
    except PlaywrightError as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or "browserType.launch" in message:
            return empty_report("unavailable", f"playwright chromium binary missing: {message[:200]}")
        return empty_report("error", f"playwright failure: {message[:200]}")
    except Exception as exc:  # noqa: BLE001 - defensive: never raise to the caller
        return empty_report("error", f"capture failure: {type(exc).__name__}: {str(exc)[:200]}")

    signals = _coerce_signals(raw)
    return ComputedStyleReport(
        status="ok",
        signals=signals,
        error=None,
        schema_version=SCHEMA_VERSION,
    )


def _coerce_signals(raw: Any) -> list[ComputedSignal]:
    """Validate and coerce the JS-returned payload into ComputedSignal list."""
    out: list[ComputedSignal] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot") or "").strip()
        selector = str(item.get("selector") or "").strip()
        props_raw = item.get("properties")
        if not slot or not isinstance(props_raw, dict):
            continue
        properties: dict[str, str] = {}
        for key, value in props_raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            v = value.strip()
            if v:
                properties[key] = v
        if properties:
            out.append(ComputedSignal(slot=slot, selector=selector, properties=properties))
    return out


def render_for_prompt(report: ComputedStyleReport) -> str:
    """Render a ComputedStyleReport as a short Markdown block for the LLM prompt.

    Returns an empty string when status is anything but "ok" with signals
    present; the caller should omit the section rather than tell the LLM
    "computed styles unavailable" (which can bias it toward defaults).
    """
    if report["status"] != "ok" or not report["signals"]:
        return ""
    lines = ["Computed styles (browser-resolved, ground truth - prefer these over inline CSS):"]
    for signal in report["signals"]:
        lines.append(f"- {signal['slot']} ({signal['selector']}):")
        for prop, value in signal["properties"].items():
            lines.append(f"    {prop}: {value}")
    return "\n".join(lines)
