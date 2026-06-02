"""DTCG manifest -> shadcn/ui theme conversion.

Pure-data transforms only; no I/O, no network. Every public function is
deterministic and round-trip stable (calling twice on the same input yields
identical output, byte-for-byte).

The high-level path:

    Resemblio DTCG manifest
        -> extract palette (list of hex colors from the ``color`` group)
        -> extract font families (from the ``fontFamily`` group)
        -> extract radius (from the ``dimension`` group, if present)
        -> map palette to shadcn semantic slots via heuristics
        -> emit light + dark ``ShadcnColorVariables`` (HSL triples)
        -> bundle into ``ShadcnTheme``

Heuristic ordering for slot assignment is documented inline; the goal is
"sensible defaults that a senior designer would not be embarrassed by",
not "perfect WCAG-tuned palette inference". Customers iterating on the
output is expected and welcomed.
"""
from __future__ import annotations

import colorsys
from typing import Any, Iterable

from resemblio_shadcn.constants import (
    FOREGROUND_LIGHTNESS_PIVOT,
    LIGHT_BACKGROUND_FLOOR,
    MONO_FAMILY_HINTS,
    NEUTRAL_SATURATION_CEILING,
    SHADCN_COLOR_SLOTS,
    SHADCN_DEFAULT_DARK,
    SHADCN_DEFAULT_LIGHT,
    SHADCN_DEFAULT_RADIUS_REM,
    SHADCN_SCHEMA_VERSION,
)
from resemblio_shadcn.types import (
    DTCGManifest,
    ShadcnColorVariables,
    ShadcnTheme,
)


# ----------------------------------------------------------------------
# Color-space helpers
# ----------------------------------------------------------------------

def _normalize_hex(value: str) -> str | None:
    """Normalize a hex color string to 6-digit ``#rrggbb`` form.

    Accepts ``#rgb``, ``#rrggbb``, and the same without the leading ``#``.
    Returns ``None`` for anything that does not parse as hex - including
    ``rgb()``, ``hsl()``, and named colors. Those are out of scope for v1;
    callers fall back to defaults when this returns ``None``.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip().lstrip("#")
    if len(raw) == 3 and all(c in "0123456789abcdefABCDEF" for c in raw):
        raw = "".join(c * 2 for c in raw)
    if len(raw) != 6 or not all(c in "0123456789abcdefABCDEF" for c in raw):
        return None
    return f"#{raw.lower()}"


def hex_to_hsl_triple(hex_color: str) -> str:
    """Convert a ``#rrggbb`` hex string to a shadcn HSL triple.

    Output: ``"H S% L%"`` where H is degrees (0-360) and S/L are percentages
    rounded to one decimal place. Example::

        >>> hex_to_hsl_triple("#3366cc")
        '220.0 60.0% 50.0%'

    Edge cases:
        - Achromatic colors (R == G == B) have H = 0 by convention.
        - Output values are deterministic for round-trip stability; the
          rounding precision (1 decimal) matches shadcn's published themes.
    """
    normalized = _normalize_hex(hex_color)
    if normalized is None:
        raise ValueError(f"not a hex color: {hex_color!r}")
    r = int(normalized[1:3], 16) / 255.0
    g = int(normalized[3:5], 16) / 255.0
    b = int(normalized[5:7], 16) / 255.0
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    h_deg = round(h * 360, 1)
    s_pct = round(s * 100, 1)
    l_pct = round(l * 100, 1)
    return f"{h_deg} {s_pct}% {l_pct}%"


def _hsl_components(hsl_triple: str) -> tuple[float, float, float]:
    """Parse ``"H S% L%"`` back to ``(h, s, l)`` floats. Internal use only."""
    parts = hsl_triple.replace("%", "").split()
    if len(parts) != 3:
        raise ValueError(f"not an HSL triple: {hsl_triple!r}")
    return float(parts[0]), float(parts[1]), float(parts[2])


def _foreground_for(hsl_triple: str) -> str:
    """Pick a light or dark foreground triple for a background HSL triple.

    Crude lightness pivot - sufficient for v1. v2 should compute WCAG
    contrast against both candidates and pick the higher one.
    """
    _, _, l = _hsl_components(hsl_triple)
    if l <= FOREGROUND_LIGHTNESS_PIVOT:
        return "210 40% 98%"  # near-white
    return "222.2 47.4% 11.2%"  # near-black


def _invert_lightness(hsl_triple: str) -> str:
    """Return the same hue/saturation with lightness mirrored around 50%.

    Used by the dark-mode auto-inversion heuristic when the input manifest
    does not carry an explicit dark variant.
    """
    h, s, l = _hsl_components(hsl_triple)
    inverted_l = round(max(0.0, min(100.0, 100.0 - l)), 1)
    return f"{h} {s}% {inverted_l}%"


# ----------------------------------------------------------------------
# DTCG extraction
# ----------------------------------------------------------------------

def _iter_color_leaves(manifest: DTCGManifest) -> Iterable[tuple[str, str]]:
    """Yield ``(leaf_name, hex_value)`` for every color leaf in the manifest.

    Walks only the ``color`` top-level group. Non-hex values are skipped;
    they will not contribute to slot assignment.
    """
    color_group = manifest.get("color") or {}
    if not isinstance(color_group, dict):
        return
    for leaf_name, leaf in color_group.items():
        if not isinstance(leaf, dict):
            continue
        raw_value = leaf.get("$value")
        if not isinstance(raw_value, str):
            continue
        normalized = _normalize_hex(raw_value)
        if normalized is not None:
            yield leaf_name, normalized


def _iter_font_families(manifest: DTCGManifest) -> list[tuple[str, str]]:
    """Return ``[(leaf_name, family_string), ...]`` from the ``fontFamily`` group."""
    out: list[tuple[str, str]] = []
    font_group = manifest.get("fontFamily") or {}
    if not isinstance(font_group, dict):
        return out
    for leaf_name, leaf in font_group.items():
        if not isinstance(leaf, dict):
            continue
        raw_value = leaf.get("$value")
        if isinstance(raw_value, str) and raw_value.strip():
            out.append((leaf_name, raw_value.strip()))
    return out


def _radius_from_dimension(manifest: DTCGManifest) -> float:
    """Return the radius (in rem) to use, falling back to shadcn default.

    Prefers ``radius-md`` -> ``radius-sm`` -> first ``radius-*`` leaf found.
    Accepts ``px`` and ``rem`` value strings; px is divided by 16.
    """
    dimension = manifest.get("dimension") or {}
    if not isinstance(dimension, dict):
        return SHADCN_DEFAULT_RADIUS_REM
    candidates_in_order = ("radius-md", "radius-sm", "radius-lg")
    for name in candidates_in_order:
        leaf = dimension.get(name)
        rem = _parse_radius_value(leaf)
        if rem is not None:
            return rem
    for name, leaf in dimension.items():
        if name.startswith("radius"):
            rem = _parse_radius_value(leaf)
            if rem is not None:
                return rem
    return SHADCN_DEFAULT_RADIUS_REM


def _parse_radius_value(leaf: Any) -> float | None:
    """Parse a single dimension leaf's ``$value`` into rem; ``None`` if unparseable."""
    if not isinstance(leaf, dict):
        return None
    raw = leaf.get("$value")
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    try:
        if text.endswith("rem"):
            return round(float(text[:-3].strip()), 3)
        if text.endswith("px"):
            return round(float(text[:-2].strip()) / 16.0, 3)
        return round(float(text), 3)
    except ValueError:
        return None


# ----------------------------------------------------------------------
# Palette -> slot mapping
# ----------------------------------------------------------------------

def _classify_palette(hex_colors: list[str]) -> dict[str, list[str]]:
    """Bucket palette entries by saturation and lightness.

    Buckets:
        - ``saturated``  : S > NEUTRAL_SATURATION_CEILING, ordered by S desc
        - ``neutral``    : S <= NEUTRAL_SATURATION_CEILING, ordered by L asc
        - ``light``      : L >= LIGHT_BACKGROUND_FLOOR (overlap with above)
        - ``dark``       : L <= (100 - LIGHT_BACKGROUND_FLOOR)
    """
    saturated: list[tuple[float, str]] = []
    neutral: list[tuple[float, str]] = []
    light: list[str] = []
    dark: list[str] = []
    for hex_color in hex_colors:
        triple = hex_to_hsl_triple(hex_color)
        _, s, l = _hsl_components(triple)
        if s > NEUTRAL_SATURATION_CEILING:
            saturated.append((s, triple))
        else:
            neutral.append((l, triple))
        if l >= LIGHT_BACKGROUND_FLOOR:
            light.append(triple)
        if l <= (100.0 - LIGHT_BACKGROUND_FLOOR):
            dark.append(triple)
    saturated.sort(key=lambda pair: pair[0], reverse=True)
    neutral.sort(key=lambda pair: pair[0])
    return {
        "saturated": [triple for _, triple in saturated],
        "neutral": [triple for _, triple in neutral],
        "light": light,
        "dark": dark,
    }


def _assign_slots_light(buckets: dict[str, list[str]]) -> dict[str, str]:
    """Map palette buckets to the shadcn light-mode slot table.

    Heuristic priority:
        - ``primary``       <- most saturated color
        - ``accent``        <- second most saturated, else fall back to primary
        - ``secondary``     <- a lighter neutral
        - ``muted``         <- the lightest neutral above the background floor,
                              else the lightest available
        - ``background``    <- lightest available, else white default
        - ``foreground``    <- darkest neutral, else default
        - ``border`` / ``input`` <- mid-light neutral
        - ``ring``          <- primary
        - ``chart-N``       <- saturated palette continued, padded from defaults
    """
    out = dict(SHADCN_DEFAULT_LIGHT)
    saturated = buckets["saturated"]
    neutral = buckets["neutral"]
    light = buckets["light"]
    dark = buckets["dark"]

    if saturated:
        out["primary"] = saturated[0]
        out["primary-foreground"] = _foreground_for(saturated[0])
        out["ring"] = saturated[0]
    if len(saturated) >= 2:
        out["accent"] = saturated[1]
        out["accent-foreground"] = _foreground_for(saturated[1])
    elif saturated:
        out["accent"] = saturated[0]
        out["accent-foreground"] = _foreground_for(saturated[0])

    if light:
        out["background"] = light[0]
        out["card"] = light[0]
        out["popover"] = light[0]
    if dark:
        out["foreground"] = dark[0]
        out["card-foreground"] = dark[0]
        out["popover-foreground"] = dark[0]

    if neutral:
        # lightest neutral -> muted/secondary; darkest neutral -> border/input
        lightest_neutral = neutral[-1] if neutral else None
        darkest_neutral = neutral[0] if neutral else None
        if lightest_neutral is not None:
            out["muted"] = lightest_neutral
            out["secondary"] = lightest_neutral
            out["muted-foreground"] = _foreground_for(lightest_neutral)
            out["secondary-foreground"] = _foreground_for(lightest_neutral)
        if darkest_neutral is not None and len(neutral) >= 2:
            out["border"] = darkest_neutral
            out["input"] = darkest_neutral

    chart_pool = saturated + buckets["neutral"]
    for i in range(1, 6):
        if i - 1 < len(chart_pool):
            out[f"chart-{i}"] = chart_pool[i - 1]
    return out


def _assign_slots_dark(light_assignments: dict[str, str], buckets: dict[str, list[str]]) -> dict[str, str]:
    """Produce a dark-mode slot table.

    If the manifest's palette already contains very dark colors (L <= 8),
    use them as ``background`` / ``card`` / ``popover``; otherwise mirror
    the light theme's lightness around 50% and reuse the same hue family
    (the auto-inversion heuristic).

    Primary/accent stay the same hue but are flipped to their light-on-dark
    foregrounds. This keeps brand identity recognizable across modes.
    """
    out = dict(SHADCN_DEFAULT_DARK)
    saturated = buckets["saturated"]
    very_dark = [t for t in buckets["dark"] if _hsl_components(t)[2] <= 8.0]

    # If the manifest contributed no palette at all, leave the shadcn dark
    # defaults untouched. The auto-inversion heuristic only kicks in when
    # there is at least one signal from the source.
    has_any_palette = bool(buckets["saturated"] or buckets["neutral"])
    if not has_any_palette:
        return out

    if very_dark:
        out["background"] = very_dark[0]
        out["card"] = very_dark[0]
        out["popover"] = very_dark[0]
    else:
        out["background"] = _invert_lightness(light_assignments["background"])
        out["card"] = _invert_lightness(light_assignments["card"])
        out["popover"] = _invert_lightness(light_assignments["popover"])

    out["foreground"] = _invert_lightness(light_assignments["foreground"])
    out["card-foreground"] = out["foreground"]
    out["popover-foreground"] = out["foreground"]

    if saturated:
        # Dark mode primary often raises lightness ~10 points for legibility.
        out["primary"] = _bump_lightness(saturated[0], delta=10.0)
        out["primary-foreground"] = _foreground_for(out["primary"])
        out["ring"] = out["primary"]
    if len(saturated) >= 2:
        out["accent"] = _bump_lightness(saturated[1], delta=10.0)
        out["accent-foreground"] = _foreground_for(out["accent"])
    elif saturated:
        out["accent"] = out["primary"]
        out["accent-foreground"] = out["primary-foreground"]

    # Muted/secondary in dark mode = a low-lightness neutral.
    muted_dark = _invert_lightness(light_assignments["muted"])
    out["muted"] = muted_dark
    out["secondary"] = muted_dark
    out["muted-foreground"] = _foreground_for(muted_dark)
    out["secondary-foreground"] = _foreground_for(muted_dark)
    out["border"] = muted_dark
    out["input"] = muted_dark

    chart_pool = saturated
    for i in range(1, 6):
        if i - 1 < len(chart_pool):
            out[f"chart-{i}"] = chart_pool[i - 1]
    return out


def _bump_lightness(hsl_triple: str, delta: float) -> str:
    """Add ``delta`` percentage points to the L component, clamped to 0-100."""
    h, s, l = _hsl_components(hsl_triple)
    new_l = round(max(0.0, min(100.0, l + delta)), 1)
    return f"{h} {s}% {new_l}%"


# ----------------------------------------------------------------------
# Font handling
# ----------------------------------------------------------------------

def _pick_fonts(families: list[tuple[str, str]]) -> tuple[str, str | None]:
    """Pick ``(--font-sans, --font-mono | None)`` from the extracted family list.

    Strategy:
        - Any family whose name OR value contains a monospace hint becomes
          ``font_mono``. Otherwise the first non-mono family becomes
          ``font_sans``. If all families are monospace, ``font_sans`` falls
          back to a CSS system stack.
        - System fallback chain is appended to whatever is picked so the
          rendered CSS is always usable even if the chosen face fails to load.
    """
    sans_fallback = (
        "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "
        '"Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
    )
    mono_fallback = (
        'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, '
        '"Liberation Mono", "Courier New", monospace'
    )

    sans_pick: str | None = None
    mono_pick: str | None = None
    for leaf_name, family in families:
        combined = f"{leaf_name} {family}".lower()
        is_mono = any(hint in combined for hint in MONO_FAMILY_HINTS)
        if is_mono and mono_pick is None:
            mono_pick = family
        elif not is_mono and sans_pick is None:
            sans_pick = family

    font_sans = f"{sans_pick}, {sans_fallback}" if sans_pick else sans_fallback
    font_mono = f"{mono_pick}, {mono_fallback}" if mono_pick else None
    # Always emit mono if we detected one; absent detection, leave None so
    # callers know it was not in the source manifest.
    return font_sans, font_mono


# ----------------------------------------------------------------------
# Public entry points
# ----------------------------------------------------------------------

def dtcg_to_shadcn(manifest: DTCGManifest, source_url: str | None = None) -> ShadcnTheme:
    """Convert a Resemblio DTCG manifest into a ``ShadcnTheme``.

    Args:
        manifest: A DTCG manifest dict as emitted by Resemblio's extractor
            (i.e. the ``dtcg_json`` field on an ``ExtractionResponse``). The
            top-level shape is ``{group: {leaf: {"$value": ..., "$type": ...}}}``
            with an optional ``schema_version`` int at the root.
        source_url: Optional source URL to stamp into ``ShadcnTheme.source_url``
            for provenance. Does not affect any output bytes.

    Returns:
        A frozen ``ShadcnTheme`` containing both light and dark color
        variable maps, the resolved font-sans (and font-mono if a
        monospace family was detected), the resolved radius in rem, and
        schema metadata for both the Resemblio source and this converter.

    Edge cases:
        - An empty or palette-less manifest returns the shadcn neutral
          defaults; no exception is raised. This is a deliberate degradation
          path so a partial Resemblio extraction still yields a usable
          theme file the developer can iterate from.
        - Non-hex color values (``rgb()``, named colors) are skipped during
          extraction; they do not contribute to slot assignment.
        - Output is deterministic and round-trip stable: calling twice on
          the same input returns bytewise-identical results.
    """
    color_leaves = list(_iter_color_leaves(manifest))
    hex_palette = [hex_color for _, hex_color in color_leaves]
    buckets = _classify_palette(hex_palette) if hex_palette else {
        "saturated": [],
        "neutral": [],
        "light": [],
        "dark": [],
    }
    light_map = _assign_slots_light(buckets)
    dark_map = _assign_slots_dark(light_map, buckets)

    font_sans, font_mono = _pick_fonts(_iter_font_families(manifest))
    radius_rem = _radius_from_dimension(manifest)
    resemblio_schema = manifest.get("schema_version") if isinstance(manifest.get("schema_version"), int) else None

    return ShadcnTheme(
        light=ShadcnColorVariables.model_validate(light_map),
        dark=ShadcnColorVariables.model_validate(dark_map),
        font_sans=font_sans,
        font_mono=font_mono,
        radius_rem=radius_rem,
        source_url=source_url,
        shadcn_schema_version=SHADCN_SCHEMA_VERSION,
        resemblio_schema_version=resemblio_schema,
    )


def render_globals_css(theme: ShadcnTheme) -> str:
    """Render the ``:root`` + ``.dark`` CSS block for ``app/globals.css``.

    Output is the exact text a shadcn project drops into its global stylesheet.
    Trailing newline included; deterministic and diff-stable.
    """
    light_lines = [f"    --{slot}: {value};" for slot, value in theme.light.as_ordered_pairs()]
    dark_lines = [f"    --{slot}: {value};" for slot, value in theme.dark.as_ordered_pairs()]
    font_lines = [f"    --font-sans: {theme.font_sans};"]
    if theme.font_mono:
        font_lines.append(f"    --font-mono: {theme.font_mono};")
    radius_line = f"    --radius: {theme.radius_rem}rem;"

    light_block = "\n".join([":root {", *light_lines, *font_lines, radius_line, "}"])
    dark_block = "\n".join([".dark {", *dark_lines, "}"])
    return f"{light_block}\n\n{dark_block}\n"


def render_tailwind_config(theme: ShadcnTheme) -> str:
    """Render a ``tailwind.config.js`` extension snippet.

    Emits the ``theme.extend.colors``, ``theme.extend.borderRadius``, and
    ``theme.extend.fontFamily`` blocks shadcn projects use. The snippet is
    a fragment - the consumer pastes it into their existing config. We do
    not emit a full Tailwind config because every project has additional
    plugins, content globs, and overrides we should not stomp on.
    """
    # Build the nested colors object the canonical shadcn way:
    #   - Slots like ``background`` / ``border`` / ``input`` / ``ring`` are flat.
    #   - Slots like ``card`` + ``card-foreground`` become a nested object with
    #     ``DEFAULT`` + ``foreground`` keys (Tailwind class -> bg-card / text-card-foreground).
    #   - ``chart-1`` ... ``chart-5`` become a nested ``chart`` object with
    #     numeric-string keys (Tailwind class -> bg-chart-1, etc.).
    nested: dict[str, dict[str, str] | str] = {}
    for slot in SHADCN_COLOR_SLOTS:
        ref = f"hsl(var(--{slot}))"
        if "-" not in slot:
            # Bare slot.
            existing = nested.get(slot)
            if isinstance(existing, dict):
                existing["DEFAULT"] = ref
            else:
                nested[slot] = ref
        else:
            parent, child = slot.split("-", 1)
            existing = nested.get(parent)
            if existing is None:
                nested[parent] = {child: ref}
            elif isinstance(existing, str):
                nested[parent] = {"DEFAULT": existing, child: ref}
            else:
                existing[child] = ref

    lines: list[str] = []
    lines.append("/** @type {import('tailwindcss').Config} */")
    lines.append("module.exports = {")
    lines.append("  darkMode: ['class'],")
    lines.append("  theme: {")
    lines.append("    extend: {")
    lines.append("      colors: {")
    for key in sorted(nested.keys()):
        value = nested[key]
        if isinstance(value, str):
            lines.append(f"        '{key}': '{value}',")
        else:
            lines.append(f"        '{key}': {{")
            for inner_key in sorted(value.keys()):
                lines.append(f"          '{inner_key}': '{value[inner_key]}',")
            lines.append("        },")
    lines.append("      },")
    lines.append("      borderRadius: {")
    lines.append("        lg: 'var(--radius)',")
    lines.append("        md: 'calc(var(--radius) - 2px)',")
    lines.append("        sm: 'calc(var(--radius) - 4px)',")
    lines.append("      },")
    lines.append("      fontFamily: {")
    lines.append("        sans: ['var(--font-sans)'],")
    if theme.font_mono:
        lines.append("        mono: ['var(--font-mono)'],")
    lines.append("      },")
    lines.append("    },")
    lines.append("  },")
    lines.append("};")
    lines.append("")  # trailing newline
    return "\n".join(lines)
