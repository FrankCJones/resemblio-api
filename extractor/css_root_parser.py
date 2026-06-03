"""Parse `:root` (and html/body) CSS custom-property declarations from <style> blocks.

The Resemblio extractor previously sent raw HTML to the LLM and asked it to
chase CSS-variable indirection like `background: var(--ink)` back to its
`:root { --ink: #0B0B0F; }` declaration. On the Susann pathology the LLM gave
up and returned a safe-default `#f5f5f5` that appears nowhere in the source.

This module is the deterministic pre-LLM pass that closes the
"missed `:root` custom-property declarations" diagnostic class from the R3.2
extraction-fidelity dispatch (`projects/Resemblio/_handoff/inbox/claude/2026-06-02-susann-extraction-fidelity-investigation.md`).
It scans every inline `<style>` block for `:root`, `html`, and `body` rule
bodies, captures every `--*: value` declaration, and surfaces them to the
extractor as ground-truth signal with HIGHER priority than computed-style
sampling: custom properties are the brand's stated INTENT, while computed
styles are the rendered artifact.

The module is pure-data: HTML string in, structured `RootCustomProperties`
out. No network. No DOM. Trivially unit-testable.

Throwaway: NO. Quality floor applies. Tests in tests/test_css_root_parser.py.
"""
from __future__ import annotations

import re
from typing import TypedDict

# Schema version bumped if the output shape changes.
SCHEMA_VERSION = 1

# Selectors whose rule bodies we mine for `--*` declarations. The list is
# intentionally short: only the document-root-equivalent selectors carry
# brand-token declarations on the modern sites we extract from. Adding
# selectors here means the extractor will see custom-property declarations
# on more elements, but it also widens the chance of capturing
# component-scoped variables that should not be promoted to brand tokens.
ROOT_LEVEL_SELECTORS: frozenset[str] = frozenset({
    ":root",
    "html",
    "body",
    "html, body",
    "body, html",
})
"""CSS selectors treated as document-root for custom-property capture."""

# Maximum number of custom properties to capture across all rule bodies in
# one HTML document. A pathological inlined design system could declare
# thousands of `--*` variables; we cap the surface to keep the prompt
# bounded. The cap is generous (covers any real brand site we have seen).
MAX_CAPTURED_PROPERTIES: int = 256
"""Hard cap on the number of `--*` declarations captured per HTML document."""

# Maximum length of a single captured value string. Long shadow chains or
# multi-stop gradients can blow up the prompt budget; truncate at this
# bound and append a marker so the LLM knows the value was clipped.
MAX_PROPERTY_VALUE_LENGTH: int = 400
"""Hard cap on a single captured value string (longer values are truncated)."""

# Marker appended to a truncated value so the LLM does not mistake the
# clipped tail for the real value.
_TRUNCATION_MARKER: str = "/*...*/"

# Pull every <style>...</style> block; same regex as font_link_parser uses.
_STYLE_BLOCK_RE = re.compile(
    r"<style\b[^>]*>(?P<body>.*?)</style>",
    re.IGNORECASE | re.DOTALL,
)

# Pull every CSS rule body. We deliberately do NOT try to fully parse CSS;
# we only need the selector list and the declaration block. The regex below
# captures `<selector list>{<body>}` pairs at the top level of the
# stylesheet text. Nested at-rules (@media, @supports) wrap a body that
# itself contains rules; the recursive structure is fine because our
# selector match still keys on the inner `:root`/`html`/`body` text.
_RULE_RE = re.compile(
    r"(?P<selectors>[^{}@]+)\{(?P<body>[^{}]*)\}",
    re.DOTALL,
)

# Match one `--name: value;` declaration inside a rule body. We require
# the leading `--` so we never capture vendor or normal properties; value
# ends at `;` or the closing brace. `!important` is preserved verbatim
# (the LLM should see it; downstream code can normalize).
_CUSTOM_PROPERTY_RE = re.compile(
    r"--(?P<name>[A-Za-z0-9_-]+)\s*:\s*(?P<value>[^;]+?)(?:;|$)",
    re.DOTALL,
)

# CSS comment stripper. Comments must go before declaration scanning so a
# commented-out `--ink: red;` does not leak into the captured set.
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


class CustomProperty(TypedDict):
    """One captured `--*` declaration with its source selector.

    Fields:
    - name: the property identifier without the leading `--` (e.g. "ink").
    - value: the declaration value as it appeared in the source, with
      leading/trailing whitespace stripped. Truncated to
      `MAX_PROPERTY_VALUE_LENGTH` characters with `_TRUNCATION_MARKER`
      appended if the source exceeded the cap.
    - selector: the selector text the declaration appeared under
      (`:root`, `html`, `body`, `html, body`, etc.). Preserved verbatim
      so downstream tooling can audit the capture decision.
    """

    name: str
    value: str
    selector: str


class RootCustomProperties(TypedDict):
    """Aggregate output of `parse_root_custom_properties`.

    Fields:
    - properties: every captured declaration in source order (de-duplicated
      by (selector, name); the LAST declaration wins when a property is
      declared more than once in the same selector, matching CSS cascade
      semantics within a single rule).
    - properties_by_name: convenience map of property name -> value. When
      the same name is declared under multiple selectors, the LAST
      declaration in source order wins (a reasonable approximation of the
      CSS cascade without computing specificity).
    - schema_version: bumped if the shape changes.
    """

    properties: list[CustomProperty]
    properties_by_name: dict[str, str]
    schema_version: int


def parse_root_custom_properties(html: str) -> RootCustomProperties:
    """Scan inline `<style>` blocks for `:root`/`html`/`body` custom properties.

    The function is total: any unparseable input returns an empty result
    with `schema_version` set. Never raises.

    Behaviour:
    - Scan every `<style>...</style>` block in the document (not just the
      first one). Brand tokens sometimes live in a separate inline block
      from the rest of the page CSS.
    - Strip CSS comments before declaration scanning.
    - For each top-level rule body whose selector list contains any token
      in `ROOT_LEVEL_SELECTORS` (split on commas, trimmed), capture every
      `--name: value` declaration.
    - Deduplicate on `(selector, name)`; the last declaration wins.
    - Cap the result at `MAX_CAPTURED_PROPERTIES` entries. Excess entries
      are silently dropped from the tail; the cap is large enough that
      any real brand site fits.

    Edge cases handled:
    - Missing `<style>` block: returns empty result.
    - `:root` with no declarations: skipped.
    - Comments inside rule bodies (`/* --fake: red; */`): ignored.
    - `!important` suffix: preserved verbatim in the captured value.
    - Same property declared twice in one rule: last value wins.
    """
    if not isinstance(html, str) or not html:
        return RootCustomProperties(
            properties=[], properties_by_name={}, schema_version=SCHEMA_VERSION
        )

    captured: list[CustomProperty] = []
    # Dedup key is (selector_canonical, name). We canonicalize the selector
    # by lowercasing and stripping internal whitespace so "html, body" and
    # "HTML,BODY" land in the same bucket.
    seen: dict[tuple[str, str], int] = {}

    for style_match in _STYLE_BLOCK_RE.finditer(html):
        block = style_match.group("body") or ""
        block = _COMMENT_RE.sub("", block)
        for rule_match in _RULE_RE.finditer(block):
            selector_raw = rule_match.group("selectors").strip()
            if not _selector_is_root_level(selector_raw):
                continue
            body = rule_match.group("body") or ""
            selector_canonical = _canonical_selector(selector_raw)
            for prop_match in _CUSTOM_PROPERTY_RE.finditer(body):
                if len(captured) >= MAX_CAPTURED_PROPERTIES and (selector_canonical, prop_match.group("name")) not in seen:
                    # Capacity reached for a NEW property; drop the rest.
                    # Existing entries can still be updated (last-wins).
                    continue
                name = prop_match.group("name").strip()
                value_raw = prop_match.group("value").strip()
                if not name or not value_raw:
                    continue
                value = _truncate_value(value_raw)
                entry = CustomProperty(
                    name=name, value=value, selector=selector_raw
                )
                key = (selector_canonical, name)
                if key in seen:
                    captured[seen[key]] = entry
                else:
                    seen[key] = len(captured)
                    captured.append(entry)

    # Build the by-name view. CSS cascade within a single document: later
    # declarations override earlier ones (we approximate without computing
    # selector specificity, which is fine for the root-level selectors
    # we scan here, all of which carry equivalent specificity in practice).
    properties_by_name: dict[str, str] = {}
    for entry in captured:
        properties_by_name[entry["name"]] = entry["value"]

    return RootCustomProperties(
        properties=captured,
        properties_by_name=properties_by_name,
        schema_version=SCHEMA_VERSION,
    )


def _selector_is_root_level(selector_text: str) -> bool:
    """Return True if any comma-separated token matches a root-level selector.

    Selector text like `html, body` or `:root` or `body.dark` is split on
    commas, each segment trimmed, and the FIRST class/attribute/pseudo
    suffix stripped so that `body.dark` still matches `body`. This is a
    deliberate widening: a `body.dark { --ink: ... }` declaration on a
    dark-mode brand site is exactly the signal we want to capture.
    """
    canonical = _canonical_selector(selector_text)
    if canonical in ROOT_LEVEL_SELECTORS:
        return True
    # Per-segment check (handles e.g. "body.dark", "html[data-theme=dark]").
    for segment in selector_text.split(","):
        bare = segment.strip()
        if not bare:
            continue
        # Strip the first non-identifier-character suffix so "body.dark"
        # becomes "body" and "html[data-theme]" becomes "html".
        head = re.split(r"[.#:\[]", bare, maxsplit=1)[0].strip().lower()
        if head in {":root", "html", "body"}:
            return True
    return False


def _canonical_selector(selector_text: str) -> str:
    """Return a lower-cased, whitespace-normalized selector for dedup keys."""
    return re.sub(r"\s+", " ", selector_text.strip().lower())


def _truncate_value(value: str) -> str:
    """Clip a captured value to `MAX_PROPERTY_VALUE_LENGTH` with a marker."""
    if len(value) <= MAX_PROPERTY_VALUE_LENGTH:
        return value
    return value[: MAX_PROPERTY_VALUE_LENGTH - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def render_for_prompt(root_props: RootCustomProperties) -> str:
    """Render a RootCustomProperties result as a Markdown block for the LLM.

    Empty input returns an empty string so the caller omits the section
    entirely. Properties are listed in capture order so the LLM sees brand
    tokens (typically declared first in `:root`) before component overrides.

    The header explicitly tells the LLM these values OUTRANK any other
    signal in the prompt: custom-property declarations are the brand's
    stated INTENT, while computed-style samples are the rendered artifact
    that may have been overridden by a stray inline style somewhere on
    the page.
    """
    if not root_props["properties"]:
        return ""
    lines = [
        "Declared CSS custom properties (root-level INTENT - prefer these over computed styles):",
    ]
    for entry in root_props["properties"]:
        lines.append(f"- {entry['selector']} --{entry['name']}: {entry['value']}")
    return "\n".join(lines)
