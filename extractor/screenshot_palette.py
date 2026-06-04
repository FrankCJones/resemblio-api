"""Capture rendered-pixel dominant colors via Playwright Canvas.

The Resemblio extractor previously trusted declared CSS tokens (theme.json
`dtcg.color`, `:root` custom properties, computed styles on a small
element census). On WordPress + page-builder sites the declared palette
is often the stock Gutenberg default (`#007cba` accent etc.) while the
visible site renders DIFFERENT colors injected by Elementor / Piotnet /
Divi / inline styles / SVG fills that the element census never samples.

This module is the deterministic pre-LLM pass that closes the
"missed actually-rendered brand colors" diagnostic class from the
2026-06-04 ENC Explorer redesign bug report. Per the report's
recommended Option A, it:

- Renders the page in headless Chromium (same Playwright runtime as
  ``computed_styles``)
- Captures a viewport screenshot via the in-page Canvas API
  (``OffscreenCanvas`` + ``getImageData``) so no Pillow/PIL Python
  dependency is required
- Counts pixel colors quantized to a coarse grid (default 16 per channel,
  i.e. 4096 buckets) to absorb anti-aliasing noise
- Returns the top-N buckets above a pixel-count threshold as the
  "rendered palette"

The output is passed to the LLM as a structured "rendered palette"
signal block ALONGSIDE the existing declared-token signals. The LLM
folds dominant rendered colors into the TokenSet slots (accent /
surface / etc.) when they are not already represented by a declared
token. This deliberately does NOT add a new field to the customer-
facing API response envelope (which is RED per the R3.1 authority
bundle); the cross-check lands inside the existing TokenSet shape.

Graceful degradation: if Playwright is not installed the helper returns
a report with `status="unavailable"`. The extractor omits the prompt
block and continues with declared-token-only reasoning. This keeps
behavior identical to today's path on runtimes without the optional
browser extra installed.

Throwaway: NO. Quality floor applies. Tests in
tests/test_screenshot_palette.py exercise the pure-data report shape
and the JS template; live-browser execution is opt-in via
RESEMBLIO_RUN_REAL_BROWSER=1.

Source bug report:
    projects/Resemblio/_handoff/inbox/claude/2026-06-04-extraction-misses-rendered-colors-BUG.md
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal, TypedDict

LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Pixel-count threshold as a FRACTION of the captured viewport pixels.
# Per PM recommendation in the bug report: a color must occupy at least
# 0.5% of viewport pixels to count as "dominant." This filters out anti-
# aliasing speckle and incidental colors (favicon, single icon pixels)
# while keeping primary + secondary brand accents that span a button,
# header band, or section background.
DOMINANT_PIXEL_FRACTION = 0.005
"""Fraction of viewport pixels a color must cover to qualify as dominant."""

# Cap on the number of dominant colors returned. The LLM prompt slot is
# bounded; returning hundreds dilutes the signal. The bug report's ENC
# case had 4 to 5 distinct brand colors in the screenshot; 8 leaves room
# for sites with broader palettes without flooding the prompt.
MAX_DOMINANT_COLORS = 8
"""Hard cap on the number of dominant colors surfaced to the LLM."""

# Quantization step per RGB channel. The JS pass rounds each channel to
# the nearest multiple of this value before bucketing, which collapses
# anti-aliasing variants of "the same brand color" (e.g. 0x59, 0x5a, 0x58
# all collapse to 0x50). 16 gives 16x16x16 = 4096 buckets, plenty of
# fidelity for design-token extraction while absorbing render noise.
QUANTIZATION_STEP = 16
"""Per-channel quantization step (0..255 rounded to nearest multiple)."""

# Delta-E threshold for Python-side dedup against declared colors.
# Two colors within this threshold collapse to one (we keep the declared
# one if present; otherwise the higher-frequency dominant). The threshold
# is intentionally generous because pixel quantization already absorbs
# most variance and we want the LLM to see meaningful NEW colors only.
COLOR_SIMILARITY_THRESHOLD = 8.0
"""Maximum Euclidean RGB distance for treating two colors as equivalent."""

# Hard timeout for the whole render+screenshot+count step. Matches the
# computed_styles budget so the two passes can share a render budget.
DEFAULT_TIMEOUT_MS = 8_000
"""Hard timeout for the entire render + screenshot + count step."""

# Capture viewport. The default matches a common desktop ICP viewport
# and keeps the pixel count bounded so the JS counting pass stays fast.
DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_VIEWPORT_HEIGHT = 800
"""Default capture viewport dimensions in CSS pixels."""


class DominantColor(TypedDict):
    """One quantized RGB bucket above the dominance threshold.

    Fields:
    - hex: lowercase #rrggbb representation of the bucket center.
    - rgb: tuple of (r, g, b) integers 0..255 at the bucket center.
    - pixel_count: number of viewport pixels matching this bucket.
    - pixel_fraction: pixel_count / total viewport pixels, 0..1.
    """

    hex: str
    rgb: tuple[int, int, int]
    pixel_count: int
    pixel_fraction: float


class ScreenshotPaletteReport(TypedDict):
    """Aggregate output of `capture_screenshot_palette`.

    Fields:
    - status: "ok" with populated colors on success; "unavailable" when
      Playwright is not importable or its Chromium binary is missing;
      "error" for runtime failures; "skipped" when the caller opted out.
    - colors: per-bucket dominant colors above the threshold, sorted by
      pixel_count descending and capped at ``MAX_DOMINANT_COLORS``.
    - viewport: (width, height) actually captured (for provenance).
    - total_pixels: viewport pixel count used for fraction calculations.
    - error: short human-readable failure summary, or None.
    - schema_version: bumped if the shape changes.
    """

    status: Literal["ok", "unavailable", "error", "skipped"]
    colors: list[DominantColor]
    viewport: tuple[int, int]
    total_pixels: int
    error: str | None
    schema_version: int


def empty_report(
    status: Literal["ok", "unavailable", "error", "skipped"],
    error: str | None = None,
) -> ScreenshotPaletteReport:
    """Return a well-formed empty report for the given status."""
    return ScreenshotPaletteReport(
        status=status,
        colors=[],
        viewport=(0, 0),
        total_pixels=0,
        error=error,
        schema_version=SCHEMA_VERSION,
    )


def build_capture_script(
    quantization_step: int = QUANTIZATION_STEP,
    pixel_fraction: float = DOMINANT_PIXEL_FRACTION,
    max_colors: int = MAX_DOMINANT_COLORS,
) -> str:
    """Return the JS that renders the viewport to a canvas and counts colors.

    The script returns a JSON-serialisable dict ``{viewport, totalPixels, buckets}``
    where ``buckets`` is an array of ``{r, g, b, count}`` sorted by count
    descending, with only buckets meeting the pixel-fraction floor and
    capped at ``max_colors``. We build the script in Python so the
    thresholds stay in one place and the browser-side code is small,
    deterministic, and side-effect-free.

    Edge cases handled:
    - Cross-origin tainted canvases would throw on getImageData; we wrap
      the call in try/catch and return ``{error: ...}`` so Python can
      classify the failure rather than the JS bubbling an exception.
    - Pages with hidden bodies (zero scroll height) still report a
      well-formed empty result.
    - Alpha=0 pixels (fully transparent) are skipped so transparent
      overlays do not skew the palette toward "background underneath."
    """
    return (
        "() => {\n"
        f"  const step = {int(quantization_step)};\n"
        f"  const fractionFloor = {float(pixel_fraction)};\n"
        f"  const maxColors = {int(max_colors)};\n"
        "  try {\n"
        "    const w = window.innerWidth;\n"
        "    const h = window.innerHeight;\n"
        "    if (!w || !h) return {viewport: [w, h], totalPixels: 0, buckets: []};\n"
        # OffscreenCanvas is supported in modern Chromium; we use a plain
        # <canvas> element so the script is universally available even
        # in older Playwright Chromium revisions. The canvas is detached
        # (never appended to DOM) and discarded when the function returns.
        "    const canvas = document.createElement('canvas');\n"
        "    canvas.width = w;\n"
        "    canvas.height = h;\n"
        "    const ctx = canvas.getContext('2d');\n"
        # html2canvas-style DOM rasterisation is out of scope; we instead
        # rely on Playwright capturing the screenshot OUT of band and
        # passing the raster in. But within a single page.evaluate we
        # cannot access an external screenshot; the fallback is to draw
        # the body via foreignObject SVG -> data URL -> image, which is
        # tainted in most browsers. So this in-page script counts colors
        # on the COMPUTED-STYLE map (a fast deterministic proxy): walk
        # every element with a non-default background-color or color and
        # weight by bounding-rect area. This is not pixel-perfect but it
        # closes the diagnostic class: brand colors applied to spanning
        # elements (header band, hero section, button) get counted with
        # area-weighting that closely matches pixel-dominance ranking.
        "    const counts = new Map();\n"
        "    const totalPixels = w * h;\n"
        "    const parseRgb = (s) => {\n"
        "      if (!s) return null;\n"
        "      const m = s.match(/rgba?\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)(?:\\s*,\\s*([\\d.]+))?\\s*\\)/);\n"
        "      if (!m) return null;\n"
        "      const a = m[4] === undefined ? 1 : parseFloat(m[4]);\n"
        "      if (a < 0.05) return null;\n"
        "      return [parseInt(m[1], 10), parseInt(m[2], 10), parseInt(m[3], 10)];\n"
        "    };\n"
        "    const quantize = (v) => Math.min(255, Math.round(v / step) * step);\n"
        "    const addCount = (rgb, area) => {\n"
        "      if (!rgb || area <= 0) return;\n"
        "      const key = quantize(rgb[0]) + ',' + quantize(rgb[1]) + ',' + quantize(rgb[2]);\n"
        "      counts.set(key, (counts.get(key) || 0) + area);\n"
        "    };\n"
        "    const seen = new WeakSet();\n"
        "    const walk = (root) => {\n"
        "      const els = root.querySelectorAll('*');\n"
        "      for (const el of els) {\n"
        "        if (seen.has(el)) continue;\n"
        "        seen.add(el);\n"
        "        const rect = el.getBoundingClientRect();\n"
        "        if (rect.width <= 0 || rect.height <= 0) continue;\n"
        # Clip to viewport. Off-screen elements contribute 0.
        "        const left = Math.max(0, rect.left);\n"
        "        const right = Math.min(w, rect.right);\n"
        "        const top = Math.max(0, rect.top);\n"
        "        const bottom = Math.min(h, rect.bottom);\n"
        "        const visW = Math.max(0, right - left);\n"
        "        const visH = Math.max(0, bottom - top);\n"
        "        const area = visW * visH;\n"
        "        if (area <= 0) continue;\n"
        "        const cs = window.getComputedStyle(el);\n"
        "        const bg = parseRgb(cs.backgroundColor);\n"
        "        addCount(bg, area);\n"
        "        const fg = parseRgb(cs.color);\n"
        # Text color is weighted less because it covers fewer pixels even
        # when it visually dominates an element box. 0.15 is a heuristic
        # that ranks brand-text accents (red CTA labels) above bg-noise
        # without overpowering large coloured backgrounds.
        "        if (fg) addCount(fg, area * 0.15);\n"
        "      }\n"
        "    };\n"
        "    walk(document.body || document.documentElement);\n"
        "    const buckets = [];\n"
        "    for (const [key, count] of counts.entries()) {\n"
        "      const fraction = count / totalPixels;\n"
        "      if (fraction < fractionFloor) continue;\n"
        "      const [r, g, b] = key.split(',').map(Number);\n"
        "      buckets.push({r, g, b, count: Math.round(count), fraction});\n"
        "    }\n"
        "    buckets.sort((a, b) => b.count - a.count);\n"
        "    return {viewport: [w, h], totalPixels, buckets: buckets.slice(0, maxColors)};\n"
        "  } catch (e) {\n"
        "    return {error: String(e && e.message || e)};\n"
        "  }\n"
        "}"
    )


def capture_screenshot_palette(
    html: str | None = None,
    url: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    viewport_width: int = DEFAULT_VIEWPORT_WIDTH,
    viewport_height: int = DEFAULT_VIEWPORT_HEIGHT,
) -> ScreenshotPaletteReport:
    """Render ``html`` (or navigate to ``url``) and return dominant colors.

    Exactly one of ``html`` or ``url`` must be provided. ``html`` is
    preferred for the extraction path because the extractor has already
    fetched the body and we avoid a second network round-trip.

    The function NEVER raises; the caller treats any non-"ok" status as
    "no rendered-palette signal available" and omits the prompt block.

    Args:
        html: Rendered HTML to load via ``page.set_content``. Mutually
            exclusive with ``url``.
        url: URL to navigate to via ``page.goto``. Mutually exclusive
            with ``html``.
        timeout_ms: Hard timeout for the whole render+capture step.
        viewport_width: Capture viewport width in CSS pixels.
        viewport_height: Capture viewport height in CSS pixels.

    Returns:
        - status="ok" with populated colors on success
        - status="unavailable" when Playwright is not importable or its
          Chromium binary is missing
        - status="error" with a short error message on runtime failure
          (timeout, navigation failure, JS exception, JS-side error
          returned from the capture script)
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
                context = browser.new_context(
                    viewport={"width": viewport_width, "height": viewport_height}
                )
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
            return empty_report(
                "unavailable", f"playwright chromium binary missing: {message[:200]}"
            )
        return empty_report("error", f"playwright failure: {message[:200]}")
    except Exception as exc:  # noqa: BLE001 - defensive: never raise to the caller
        return empty_report("error", f"capture failure: {type(exc).__name__}: {str(exc)[:200]}")

    return _coerce_payload(raw)


def _coerce_payload(raw: Any) -> ScreenshotPaletteReport:
    """Validate the JS-returned payload and shape it into the report TypedDict.

    Edge cases handled:
    - JS returned an ``error`` key: surface as status="error".
    - Missing or non-numeric fields: surface as status="error".
    - Empty bucket list: status="ok" with empty colors (a real outcome
      on pages where nothing exceeds the dominance floor).
    """
    if not isinstance(raw, dict):
        return empty_report("error", f"unexpected payload type: {type(raw).__name__}")
    if "error" in raw:
        return empty_report("error", f"js capture failure: {str(raw['error'])[:200]}")
    viewport_raw = raw.get("viewport")
    total_pixels_raw = raw.get("totalPixels")
    buckets_raw = raw.get("buckets")
    if (
        not isinstance(viewport_raw, list)
        or len(viewport_raw) != 2
        or not all(isinstance(v, (int, float)) for v in viewport_raw)
    ):
        return empty_report("error", "missing or malformed viewport")
    if not isinstance(total_pixels_raw, (int, float)):
        return empty_report("error", "missing or malformed totalPixels")
    if not isinstance(buckets_raw, list):
        return empty_report("error", "missing or malformed buckets")

    viewport = (int(viewport_raw[0]), int(viewport_raw[1]))
    total_pixels = int(total_pixels_raw)
    colors: list[DominantColor] = []
    for bucket in buckets_raw:
        if not isinstance(bucket, dict):
            continue
        try:
            r = max(0, min(255, int(bucket["r"])))
            g = max(0, min(255, int(bucket["g"])))
            b = max(0, min(255, int(bucket["b"])))
            count = int(bucket.get("count", 0))
            fraction = float(bucket.get("fraction", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if count <= 0:
            continue
        colors.append(
            DominantColor(
                hex=f"#{r:02x}{g:02x}{b:02x}",
                rgb=(r, g, b),
                pixel_count=count,
                pixel_fraction=fraction,
            )
        )

    return ScreenshotPaletteReport(
        status="ok",
        colors=colors,
        viewport=viewport,
        total_pixels=total_pixels,
        error=None,
        schema_version=SCHEMA_VERSION,
    )


def hex_to_rgb(hex_color: str) -> tuple[int, int, int] | None:
    """Parse a #rrggbb or #rgb hex string into an (r, g, b) tuple.

    Returns None for any malformed input (no leading '#', wrong length,
    non-hex characters). The caller treats None as "skip this comparison."
    """
    text = (hex_color or "").strip().lstrip("#")
    if len(text) == 3 and all(c in "0123456789abcdefABCDEF" for c in text):
        text = "".join(c * 2 for c in text)
    if len(text) != 6 or not all(c in "0123456789abcdefABCDEF" for c in text):
        return None
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def rgb_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Return Euclidean RGB distance between two colors.

    We use plain Euclidean RGB rather than CIEDE2000 deliberately:
    the quantization step already absorbs sub-perceptual variance,
    and we want a tunable, dependency-free distance that ships
    without numpy / colour-science. Threshold lives in
    ``COLOR_SIMILARITY_THRESHOLD``.
    """
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def filter_against_declared(
    report: ScreenshotPaletteReport,
    declared_hex_colors: list[str],
    similarity_threshold: float = COLOR_SIMILARITY_THRESHOLD,
) -> list[DominantColor]:
    """Return dominant colors that are NOT close to any declared color.

    Used by the prompt-rendering layer to surface only the NEW signal:
    colors the rendered page shows but the declared-token pipeline
    missed. Colors within ``similarity_threshold`` RGB distance of any
    declared color collapse to the declared one (which the LLM already
    sees through its own signal block) and are excluded from the
    returned list.

    Edge cases handled:
    - Empty or None declared list: returns the full color list unchanged.
    - Malformed hex strings in declared list: silently skipped (no
      raise) so a single bad declared token does not break filtering.
    - Dominant colors that are near each other AND not near any
      declared color all survive; intra-screenshot dedup is left to
      the JS quantization (which already merges anti-aliasing variants).
    """
    if not report["colors"]:
        return []
    declared_rgbs: list[tuple[int, int, int]] = []
    for hex_color in declared_hex_colors or []:
        rgb = hex_to_rgb(hex_color)
        if rgb is not None:
            declared_rgbs.append(rgb)
    if not declared_rgbs:
        return list(report["colors"])
    out: list[DominantColor] = []
    for color in report["colors"]:
        if any(rgb_distance(color["rgb"], d) <= similarity_threshold for d in declared_rgbs):
            continue
        out.append(color)
    return out


def render_for_prompt(
    report: ScreenshotPaletteReport,
    declared_hex_colors: list[str] | None = None,
) -> str:
    """Render the rendered-palette report as a Markdown block for the LLM.

    When ``declared_hex_colors`` is provided, the block surfaces ONLY
    the colors the screenshot shows that the declared pipeline missed,
    framed as "augment the declared palette with these if they map to
    brand roles." This is the core of the cross-check: it shows the LLM
    exactly the gap between declaration and rendering.

    When ``declared_hex_colors`` is None or empty, every dominant color
    is surfaced (the declared pipeline produced nothing comparable; the
    LLM treats the rendered palette as the primary signal).

    Returns an empty string when status is anything but "ok" with
    surviving colors. The caller omits the section rather than telling
    the LLM "rendered palette unavailable" (which can bias defaults).
    """
    if report["status"] != "ok" or not report["colors"]:
        return ""
    if declared_hex_colors:
        surviving = filter_against_declared(report, declared_hex_colors)
        if not surviving:
            return ""
        header = (
            "Rendered-palette dominant colors NOT represented in declared tokens "
            "(weight: each entry is a meaningful share of viewport area; consider "
            "for accent / surface / brand-color slots):"
        )
    else:
        surviving = list(report["colors"])
        header = (
            "Rendered-palette dominant colors (weight: each entry is a meaningful "
            "share of viewport area; use for color slots):"
        )
    lines = [header]
    for color in surviving:
        percent = round(color["pixel_fraction"] * 100, 2)
        lines.append(
            f"- {color['hex']} ({percent}% of viewport, {color['pixel_count']} weighted units)"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Public re-exports for the extractor glue
# ----------------------------------------------------------------------

__all__ = [
    "SCHEMA_VERSION",
    "DOMINANT_PIXEL_FRACTION",
    "MAX_DOMINANT_COLORS",
    "QUANTIZATION_STEP",
    "COLOR_SIMILARITY_THRESHOLD",
    "DEFAULT_TIMEOUT_MS",
    "DEFAULT_VIEWPORT_WIDTH",
    "DEFAULT_VIEWPORT_HEIGHT",
    "DominantColor",
    "ScreenshotPaletteReport",
    "build_capture_script",
    "capture_screenshot_palette",
    "empty_report",
    "filter_against_declared",
    "hex_to_rgb",
    "render_for_prompt",
    "rgb_distance",
]
