"""Deterministic brand-signal digest for the Resemblio extractor.

Combines the CSS custom-property resolver and the brand cascade scanner into a
single "VERIFIED STYLE DIGEST" block that the LLM extraction prompt uses to
anchor its output to declared brand intent rather than raw var() indirection.

This module is ADDITIVE to the existing signal pipeline. It does NOT replace
`css_root_parser` (which renders all `:root` vars) or `font_link_parser` (which
parses web-font `<link>` tags). The digest adds:

  1. ``resolve_var``:  pure ``var(--x, fallback)`` resolver against a name map.
  2. ``extract_brand_cascade``:  scans key CSS rule bodies (html/body, headings,
     links/buttons) and resolves ``background``, ``color``, ``font-family``
     through the var map into typed SlotValue items.
  3. ``build_style_digest``:  orchestrates the two passes above (plus a font-link
     fallback) into a schema-versioned ``StyleDigest``.
  4. ``render_digest_block``:  renders the digest as a labelled prompt block.

Motivation (R3.1):  the Susann Headlights site stores every brand color as a
``:root`` custom property and consumes it via ``var()``. The extractor previously
saw ``background: var(--ink)`` and guessed ``#f5f5f5`` because it did not resolve
the indirection. The digest makes the mapping EXPLICIT before the LLM call:
``bg: #0B0B0F  [source: html, body { background: var(--ink) }]``.

Quality floor: pure-data (no I/O, no network), TypedDict shapes, docstrings
with intent + edge cases, named constants, unit tests in
``tests/test_style_digest.py``.

Throwaway: NO. Senior-developer bar applies.
"""
from __future__ import annotations

import re
from typing import TypedDict

from extractor.css_root_parser import RootCustomProperties, parse_root_custom_properties
from extractor.font_link_parser import LoadedFonts, parse_loaded_fonts

# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

SCHEMA_VERSION: str = "style_digest_v1"
"""Bumped when the shape of StyleDigest or SlotValue changes."""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VAR_MAX_RESOLVE_DEPTH: int = 5
"""Maximum recursion depth for var() resolution.

Prevents infinite loops in self-referential or circular var chains. Five levels
covers real-world cases (design tokens occasionally chain two or three levels).
"""

# CSS block parsers (shared with css_root_parser; duplicated here to keep this
# module independent and avoid circular import risk from shared utility module).
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_STYLE_BLOCK_RE = re.compile(
    r"<style\b[^>]*>(?P<body>.*?)</style>",
    re.IGNORECASE | re.DOTALL,
)
_RULE_RE = re.compile(
    r"(?P<selectors>[^{}@]+)\{(?P<body>[^{}]*)\}",
    re.DOTALL,
)
# Declaration inside a rule body: `property-name: value;`
# The prop pattern deliberately excludes leading hyphens (CSS custom properties
# start with `--`; `_` prefix is also excluded) so vendor-prefixed props and
# custom properties in cascade rule bodies don't pollute the output.
_DECL_RE = re.compile(
    r"(?P<prop>[a-z][a-z0-9-]*)[ \t]*:[ \t]*(?P<value>[^;]+?)[ \t]*(?:;|$)",
    re.DOTALL | re.IGNORECASE,
)
# Match a var() expression; may appear inline inside a larger value string.
_VAR_RE = re.compile(
    r"var\(--(?P<name>[A-Za-z0-9_-]+)(?:[ \t]*,[ \t]*(?P<fallback>[^)]+))?\)",
    re.IGNORECASE,
)

# Canonical forms of body-level selectors.
_BODY_SELECTORS: frozenset[str] = frozenset({
    "html",
    "body",
    "html,body",
    "html, body",
    "body,html",
    "body, html",
    "html body",
})
"""Selectors whose rule bodies carry base background / text / font-family."""

# Heading selector pattern: matches any rule whose selector contains h1..h6.
_HEADING_RE = re.compile(r"\bh[1-6]\b", re.IGNORECASE)

# CSS properties that map to the ``bg`` token slot.
_BG_PROPS: frozenset[str] = frozenset({"background", "background-color"})

# CSS properties that map to the ``text`` token slot.
_TEXT_PROPS: frozenset[str] = frozenset({"color"})

# CSS properties that map to the font slots.
_FONT_PROPS: frozenset[str] = frozenset({"font-family"})


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


class SlotValue(TypedDict):
    """One resolved brand-role slot value with its provenance.

    Fields:
    - slot:   the TokenSet key (``bg``, ``text``, ``accent``, ``font_body``,
              ``font_display``).
    - value:  the resolved CSS value (literal hex, rgb(), or font-family stack).
    - source: human-readable provenance string for the prompt block, naming the
              CSS rule that carried the declaration.
    """

    slot: str
    value: str
    source: str


class StyleDigest(TypedDict):
    """Structured output of ``build_style_digest``.

    Fields:
    - schema_version:   bumped when the shape changes. Consumers tolerate
                        unknown keys (future fields are additive).
    - resolved_slots:   list of SlotValues, one per resolved brand role.
                        Empty when no slots could be resolved (plain HTML,
                        no ``:root`` vars, or all values unresolvable).
    """

    schema_version: str
    resolved_slots: list[SlotValue]


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def resolve_var(value: str, var_map: dict[str, str], *, _depth: int = 0) -> str:
    """Resolve CSS ``var()`` expressions in a value string using ``var_map``.

    Pure function: no side effects, no I/O, deterministic.

    The function handles the full common-case set of CSS ``var()`` syntax:
    - ``var(--x)``             -> ``var_map["x"]`` when present, else unchanged.
    - ``var(--x, fallback)``   -> ``var_map["x"]`` when present, else ``fallback``.
    - Nested chains            -> resolved recursively up to VAR_MAX_RESOLVE_DEPTH.
    - Partial values           -> ``"0 1px var(--shadow)"`` has the var() portion
                                   replaced; surrounding text is preserved.
    - Literal values           -> returned unchanged (fast path, no regex).

    Edge cases:
    - ``var_map`` key is the name WITHOUT the leading ``--`` (e.g. ``"ink"`` for
      ``--ink``). This matches the output of ``css_root_parser.properties_by_name``.
    - Self-referential cycle (``--a: var(--a)``): at max depth the function
      returns the unresolved ``var(--a)`` token rather than looping.
    - Fallback strings are themselves resolved (``var(--x, var(--y))``).

    Args:
        value:   the raw CSS value string, e.g. ``"var(--ink)"`` or ``"17px"``.
        var_map: name-to-value mapping from ``parse_root_custom_properties``.
        _depth:  recursion depth counter; callers should not set this.

    Returns:
        The resolved value string, or the original value if no resolution applied.
    """
    if not value or "var(--" not in value:
        return value
    if _depth >= VAR_MAX_RESOLVE_DEPTH:
        return value

    def _replace(match: re.Match[str]) -> str:
        name = match.group("name")
        fallback_raw = match.group("fallback")
        if name in var_map:
            resolved = var_map[name].strip()
            return resolve_var(resolved, var_map, _depth=_depth + 1)
        if fallback_raw:
            return resolve_var(fallback_raw.strip(), var_map, _depth=_depth + 1)
        return match.group(0)  # unresolvable; preserve the original token

    return _VAR_RE.sub(_replace, value)


def extract_brand_cascade(
    html: str,
    var_map: dict[str, str],
) -> list[SlotValue]:
    """Scan inline CSS rules for key brand slots and resolve ``var()`` references.

    Targets:
    - ``html``/``body``/``html, body`` rules:
        ``background``/``background-color`` -> ``bg`` slot (last declaration wins).
        ``color``                           -> ``text`` slot (last declaration wins).
        ``font-family``                     -> ``font_body`` slot (last wins).
    - ``h1``-``h6`` rules:
        ``font-family`` -> ``font_display`` slot (FIRST match wins; heading font is
        generally consistent across heading levels, so the first rule is authoritative).
    - Accent-bearing rules (bare ``a``, ``.btn``, ``.button``, ``.cta``, ``.link``,
      selectors containing "accent"):
        ``color`` -> ``accent`` slot (FIRST match wins; later rules are often overrides
        for hover/focus states that would give the wrong primary value).

    Edge cases:
    - No ``<style>`` blocks: returns empty list.
    - Unresolvable ``var()`` (still contains ``var(--`` after resolution attempt):
      the slot is silently excluded rather than propagating a wrong value.
    - CSS comments are stripped before parsing.
    - Nested at-rules (``@media``) are not recursed; declarations inside them are
      excluded by the flat-rule-body regex.
    - The function implements a best-effort approximation of the CSS cascade.
      Specificity, ``!important``, and external stylesheets are out of scope.

    Args:
        html:    the full HTML document string (not truncated).
        var_map: ``properties_by_name`` from ``parse_root_custom_properties``.

    Returns:
        List of SlotValue items. Multiple entries per slot are possible (each
        declaration is an entry). Callers use the last value per slot for
        body-level properties (CSS last-wins) or the first value for display
        font / accent (see above).
    """
    last_bg: SlotValue | None = None
    last_text: SlotValue | None = None
    last_font_body: SlotValue | None = None
    first_font_display: SlotValue | None = None
    first_accent: SlotValue | None = None

    for style_match in _STYLE_BLOCK_RE.finditer(html):
        block = _COMMENT_RE.sub("", style_match.group("body") or "")
        for rule_match in _RULE_RE.finditer(block):
            selector_raw = rule_match.group("selectors").strip()
            # Canonicalise for matching: lowercase + collapse internal spaces.
            selector_key = re.sub(r"[ \t]+", " ", selector_raw.lower().strip())
            body = rule_match.group("body") or ""

            is_body = selector_key in _BODY_SELECTORS
            is_heading = bool(_HEADING_RE.search(selector_key))
            is_accent = _is_accent_selector(selector_key)

            if not (is_body or is_heading or is_accent):
                continue

            for decl_match in _DECL_RE.finditer(body):
                prop = decl_match.group("prop").strip().lower()
                raw_value = decl_match.group("value").strip()
                resolved = resolve_var(raw_value, var_map)

                # Exclude any slot whose value is still an unresolved var() -
                # propagating a var() token as a CSS value would mislead the LLM.
                if "var(--" in resolved:
                    continue

                source = f"{selector_raw} {{ {prop}: {raw_value} }}"

                if is_body:
                    if prop in _BG_PROPS:
                        last_bg = SlotValue(slot="bg", value=resolved, source=source)
                    elif prop in _TEXT_PROPS:
                        last_text = SlotValue(slot="text", value=resolved, source=source)
                    elif prop in _FONT_PROPS:
                        last_font_body = SlotValue(
                            slot="font_body", value=resolved, source=source
                        )

                if is_heading and first_font_display is None and prop in _FONT_PROPS:
                    first_font_display = SlotValue(
                        slot="font_display", value=resolved, source=source
                    )

                if is_accent and first_accent is None and prop in _TEXT_PROPS:
                    first_accent = SlotValue(
                        slot="accent", value=resolved, source=source
                    )

    results: list[SlotValue] = []
    for sv in (last_bg, last_text, last_font_body, first_font_display, first_accent):
        if sv is not None:
            results.append(sv)
    return results


def _is_accent_selector(selector_key: str) -> bool:
    """Return True when the selector likely carries an accent/link color.

    Targets the set of selectors that real sites use to declare their primary
    interactive color: bare ``a``, ``a`` with pseudo-class, and common class
    names for buttons and CTAs.

    Intentionally narrow: ``nav a``, ``.card-title a``, ``.footer a`` are
    excluded to avoid absorbing component-level overrides that would give the
    wrong primary accent value.

    Args:
        selector_key: lowercased, whitespace-normalised selector string.

    Returns:
        True if any comma-segment of the selector is a bare link or button/CTA
        class.
    """
    for segment in selector_key.split(","):
        seg = segment.strip()
        # Bare anchor or with a single pseudo-class only.
        if seg in ("a", "a:link", "a:visited", "a:hover", "a:focus", "a:active"):
            return True
        # Class-name check: strip all non-alpha/hyphen chars, compare to known CTA names.
        bare_class = re.sub(r"[^a-z-]", "", seg)
        if bare_class in ("btn", "button", "cta", "link", "accent", "primary"):
            return True
    return False


def build_style_digest(
    html: str,
    root_props: RootCustomProperties | None = None,
) -> StyleDigest:
    """Build a StyleDigest from HTML, optionally reusing already-parsed root props.

    Orchestration:
    1. Parse ``:root`` CSS custom properties (or reuse ``root_props`` when
       provided to avoid double-parsing in the main extraction path).
    2. Call ``extract_brand_cascade`` to resolve bg / text / font-body /
       font-display / accent slots through the var map.
    3. Fall back to ``font_link_parser`` for font slots not resolved by the
       cascade (covers the case where fonts are loaded from a ``<link>`` tag
       but no inline ``font-family`` rule exists in the body CSS).

    Edge cases:
    - Empty or non-string html: returns an empty digest immediately.
    - No ``:root`` vars: ``var_map`` is empty; cascade resolution will not
      resolve any ``var()`` expressions and will only capture literal values.
    - Font-link fallback: only fires when the cascade found no ``font_body``
      and/or ``font_display`` slot. The first family from the detected-web-fonts
      list fills ``font_body``; the second (if present) fills ``font_display``.
    - Never raises: all called functions are total.

    Args:
        html:       the full HTML document string (not truncated).
        root_props: optional pre-parsed ``RootCustomProperties`` from
                    ``parse_root_custom_properties``. When provided, skips a
                    second parse pass (useful when the caller already parsed
                    root props for the signals pipeline).

    Returns:
        A ``StyleDigest`` with ``schema_version`` and ``resolved_slots``.
    """
    if not isinstance(html, str) or not html:
        return StyleDigest(schema_version=SCHEMA_VERSION, resolved_slots=[])

    if root_props is None:
        root_props = parse_root_custom_properties(html)
    var_map: dict[str, str] = root_props.get("properties_by_name", {})

    cascade_slots = extract_brand_cascade(html, var_map)
    slots_by_type = {sv["slot"] for sv in cascade_slots}

    # Font-link fallback: when the cascade found no font-family declaration in
    # the body rules, try the web-font <link> tags. This covers sites that load
    # a Google Fonts family but set it via a CSS custom property (the cascade
    # scanner already resolved that path), or sites where the font-family is
    # only declared in an external stylesheet (out of reach without Playwright).
    if "font_body" not in slots_by_type or "font_display" not in slots_by_type:
        loaded_fonts: LoadedFonts = parse_loaded_fonts(html)
        families: list[str] = loaded_fonts.get("families", [])
        source_label = "detected web-font <link> tag (font-family not in inline cascade)"
        if families and "font_body" not in slots_by_type:
            cascade_slots.append(SlotValue(
                slot="font_body",
                value=f"{families[0]}, sans-serif",
                source=source_label,
            ))
        if len(families) >= 2 and "font_display" not in slots_by_type:
            cascade_slots.append(SlotValue(
                slot="font_display",
                value=f"{families[1]}, sans-serif",
                source=source_label,
            ))

    return StyleDigest(schema_version=SCHEMA_VERSION, resolved_slots=cascade_slots)


def render_digest_block(digest: StyleDigest) -> str:
    """Render a StyleDigest as a prompt block for the LLM.

    Returns an empty string when the digest has no resolved slots so the
    caller can use a simple ``if rendered:`` guard before appending to the
    signals list.

    The block header instructs the LLM to prefer these values over raw HTML
    scanning because they are fully RESOLVED: var() indirection has been traced
    back to literal values, and the source rule is named for auditability.

    Format::

        VERIFIED STYLE DIGEST (resolved var() -> literal values; prefer over raw HTML):
        - bg: #0B0B0F  [source: html, body { background: var(--ink) }]
        - text: #F5F2EA  [source: html, body { color: var(--bone) }]
        ...

    Args:
        digest: the StyleDigest from ``build_style_digest``.

    Returns:
        Multi-line string for inclusion in the extraction prompt, or ``""`` when
        the digest is empty.
    """
    slots = digest.get("resolved_slots", [])
    if not slots:
        return ""
    lines = [
        "VERIFIED STYLE DIGEST (resolved var() -> literal values; prefer over raw HTML):",
    ]
    for sv in slots:
        lines.append(f"- {sv['slot']}: {sv['value']}  [source: {sv['source']}]")
    return "\n".join(lines)
