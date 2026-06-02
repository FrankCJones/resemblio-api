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
from typing import Any, Literal, TypedDict

SCHEMA_VERSION = 1

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


def build_capture_script() -> str:
    """Return the JS that captures computed styles for every census element.

    The script returns a JSON-serialisable array of `{slot, selector, properties}`
    objects. We build the script in Python so the property list and census
    stay synchronised and easy to extend; the browser-side code is small,
    deterministic, and side-effect-free.
    """
    census_json = json.dumps([{"selector": sel, "slot": slot} for sel, slot in ELEMENT_CENSUS])
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


def capture_computed_styles(
    html: str | None = None,
    url: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> ComputedStyleReport:
    """Render `html` (or navigate to `url`) and capture computed styles.

    Exactly one of `html` or `url` must be provided. `html` is preferred
    for the extraction path because the extractor has already fetched
    the body and we avoid a second network round-trip.

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

    script = build_capture_script()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                if html is not None:
                    page.set_content(html, wait_until="domcontentloaded")
                else:
                    page.goto(url or "", wait_until="domcontentloaded")
                raw = page.evaluate(script)
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
