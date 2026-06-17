"""Whole-mining: extract embedded atoms (markup + CSS) from captured DRL wholes.

Schema: ``whole_mining_v1``

A "whole" is a captured DRL layout component (e.g., ``apple-cta-block-001``)
that was not designed as an atom but contains recognisable atom-class elements
embedded inside it. This module lifts those atoms out so the library indexer
can serve them on ``/library/<brand>/buttons`` (and other atom-class pages)
without requiring a separately-captured atom asset.

Public entry point
------------------
``mine_atom_from_whole(whole_html, atom_class) -> MinedAtom | None``

    Given the full text of a DRL ``asset.html`` whole and an atom class name
    (e.g. ``"buttons"``), returns a :class:`MinedAtom` carrying the extracted
    markup subtree and the matching CSS rules.  Returns ``None`` when no element
    of that class is present.

Shared utilities (re-exported for ``scripts/seed_from_drl.py``)
---------------------------------------------------------------
``strip_provenance_comments`` and ``derive_states_present`` were previously
defined in ``scripts/seed_from_drl.py``.  They now live here as the canonical
location.  ``seed_from_drl`` re-imports them so its callers are unaffected.

MinedAtom -> AssetComponentSpec mapping
--------------------------------------
``AssetComponentSpec`` (``app.asset_versions``) is the write-path shape for
``asset_components`` rows.  A ``MinedAtom`` maps onto it as follows:

    MinedAtom.component_html  -> AssetComponentSpec.component_html
    MinedAtom.component_css   -> AssetComponentSpec.component_css  (RAW, unscoped)
    MinedAtom.states_present  -> AssetComponentSpec.states_present
    MinedAtom.source_classes  -> (provenance / debug; no direct DB column)
    MinedAtom.atom_class      -> (used as fragment_key by the persistence layer)
    MinedAtom.schema_version  -> (not stored on AssetComponentSpec; tracked here)

Why component_css is left unscoped
-----------------------------------
The indexer (``library_indexer._compose_real_component``) calls
``scope_style_block`` on the stored ``component_css`` at render time.
Pre-scoping here would double-scope if the indexer ever changes its wrapper
selector, and would break the contract that the DB stores raw component code.

DRL read-only contract
----------------------
No function in this module writes to any path under the Design Reference
Library.  The ``mine_atom_from_whole`` entry point accepts the whole's HTML
text as a string so no DRL file I/O is required at extraction time.

Do this work at a level that would impress a senior developer.
Include documentation and code comments that make it easy for a future
developer to maintain this project.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final

from app.library_style_scope import CssRule, iter_css_rules


# ---------------------------------------------------------------------------
# Shared comment-stripping utilities (canonical; seed_from_drl re-imports)
# ---------------------------------------------------------------------------

_HTML_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->", re.DOTALL)
_CSS_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)

# Extracted CSS block from an asset.html <style> tag.
_STYLE_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"<style[^>]*>(.*?)</style>", re.DOTALL | re.IGNORECASE
)

# State-selector patterns used by derive_states_present.
_STATE_SELECTOR_MAP: Final[list[tuple[re.Pattern[str], str]]] = [
    (re.compile(r":hover"), "hover"),
    (re.compile(r":focus"), "focus"),
    (re.compile(r":active"), "active"),
    (re.compile(r':disabled|\[disabled\]|\[aria-disabled=["\']true["\']\]'), "disabled"),
]


def strip_provenance_comments(text: str) -> str:
    """Remove HTML (``<!-- -->``) and CSS (``/* */``) comments from ``text``.

    This is the primary defence against DRL provenance annotations reaching
    the bytes Resemblio serves.  DRL assets embed brand attribution exclusively
    inside comments; the rendered markup and class names are already brand-
    stripped.  Stripping both forms here ensures no comment-only attribution
    leaks into the DB.

    Args:
        text: Raw HTML or CSS string that may contain either comment form.

    Returns:
        The input with all comment blocks removed.  HTML comments are stripped
        first, then CSS comments; the order does not matter in practice because
        neither form nests inside the other.
    """
    text = _HTML_COMMENT_RE.sub("", text)
    text = _CSS_COMMENT_RE.sub("", text)
    return text


def derive_states_present(component_css: str) -> list[str]:
    """Derive the UI interaction states declared in ``component_css``.

    Scans for state selectors and returns a stable, sorted, deduplicated list
    of normalised state names.  ``'rest'`` is always included (it represents
    the default unstyled state and has no CSS selector of its own).

    State name mapping:
        - ``:hover``                               -> ``"hover"``
        - ``:focus`` or ``:focus-visible``         -> ``"focus"``
        - ``:active``                              -> ``"active"``
        - ``:disabled``, ``[disabled]``,
          ``[aria-disabled="true"]``               -> ``"disabled"``

    Args:
        component_css: CSS text (comment-stripped is fine but not required).

    Returns:
        Sorted list of state names, always including ``'rest'``.
    """
    states: set[str] = {"rest"}
    for pattern, state_name in _STATE_SELECTOR_MAP:
        if pattern.search(component_css):
            states.add(state_name)
    return sorted(states)


# ---------------------------------------------------------------------------
# Atom detection hints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AtomHint:
    """Detection hint for one atom class in whole markup.

    An element matches the hint when ANY of these is true:
      - ``element.tag`` is in ``tags``
      - ANY class token on the element contains ANY string in ``class_substrings``

    The class substring match is case-insensitive.  An empty set for ``tags``
    means "tag alone never triggers a match"; similarly for ``class_substrings``.
    """

    tags: frozenset[str]
    class_substrings: frozenset[str]


# Detection hints for the five atom classes supported by this implementation.
# Adding a new class: add an entry here with the appropriate tags and
# class_substrings.  The buttons slice is the proven one; cards/badges/links/
# inputs are seeded but not yet validated against a real corpus.
ATOM_DETECTION_HINTS: Final[dict[str, AtomHint]] = {
    "buttons": AtomHint(
        tags=frozenset({"button"}),
        class_substrings=frozenset({"btn", "button"}),
    ),
    "cards": AtomHint(
        tags=frozenset(),
        class_substrings=frozenset({"card"}),
    ),
    "badges": AtomHint(
        tags=frozenset(),
        class_substrings=frozenset({"badge", "pill", "tag", "chip"}),
    ),
    "links": AtomHint(
        tags=frozenset({"a"}),
        class_substrings=frozenset({"link"}),
    ),
    "inputs": AtomHint(
        tags=frozenset({"input", "textarea"}),
        class_substrings=frozenset({"input", "field"}),
    ),
}


# ---------------------------------------------------------------------------
# MinedAtom data shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MinedAtom:
    """A single atom extracted from a captured DRL whole.

    See module docstring for the MinedAtom -> AssetComponentSpec mapping and
    the reason ``component_css`` is kept raw/unscoped.

    Fields
    ------
    atom_class
        The Resemblio atom category this serves, e.g. ``"buttons"``.
    component_html
        Extracted markup subtree, comment-stripped, wrapped in a
        ``<div class="rs-mined-group" data-rs-mined-from="<atom_class>">``
        grouping element.
    component_css
        Matched CSS rules, raw (unscoped), comment-stripped.
    states_present
        Sorted list of interaction state names derived from ``component_css``.
    source_classes
        Sorted deduplicated list of class names found on matched elements.
        Used for provenance and debug; the persistence layer uses this to
        call ``css_rules_for_classes``.
    schema_version
        Always ``"whole_mining_v1"``.
    """

    atom_class: str
    component_html: str
    component_css: str
    states_present: list[str]
    source_classes: list[str]
    schema_version: str


# ---------------------------------------------------------------------------
# CSS selector helpers
# ---------------------------------------------------------------------------

_CLASS_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\.([a-zA-Z0-9_-]+)")

# Tokens that appear in CSS animation shorthand but are NOT the animation name.
_CSS_ANIM_NON_NAME_TOKENS: Final[frozenset[str]] = frozenset({
    # global keywords
    "initial", "inherit", "unset", "revert",
    # <single-animation-name>
    "none",
    # <easing-function>
    "ease", "ease-in", "ease-out", "ease-in-out", "linear",
    "step-start", "step-end",
    # <single-animation-direction>
    "normal", "reverse", "alternate", "alternate-reverse",
    # <single-animation-fill-mode>
    "forwards", "backwards", "both",
    # <single-animation-play-state>
    "paused", "running",
    # <single-animation-iteration-count>
    "infinite",
})
_CSS_TIMING_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d*\.?\d+(ms|s)$", re.IGNORECASE
)


def _split_comma_list(selector_list: str) -> list[str]:
    """Split a CSS selector list on top-level commas (not inside parentheses).

    Example: ``'.a, .b:not(.c, .d)'`` -> ``['.a', ' .b:not(.c, .d)']``
    """
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in selector_list:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _split_on_combinators(selector: str) -> list[str]:
    """Split a single CSS selector into simple-selector segments.

    Splits on descendant (whitespace), child (``>``), adjacent sibling
    (``+``), and general sibling (``~``) combinators at depth 0 (not
    inside parentheses).  Returns non-empty segments only.
    """
    segments: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in selector:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif depth == 0 and ch in " \t\n\r>~+":
            seg = "".join(buf).strip()
            if seg:
                segments.append(seg)
            buf = []
        else:
            buf.append(ch)
    seg = "".join(buf).strip()
    if seg:
        segments.append(seg)
    return segments


def _selector_subject_matches(selector: str, class_names: frozenset[str]) -> bool:
    """Return True when the rightmost simple selector references a class in ``class_names``.

    The "rightmost simple selector" is the subject element of the rule.
    Example: in ``.cta__actions .cta__btn:hover``, the subject is
    ``.cta__btn:hover`` whose class token is ``cta__btn``.

    Limitation: ``:not(.classname)`` and similar functional pseudo-classes
    may produce false positives.  DRL assets do not use them, so this is
    out-of-scope.

    Args:
        selector: One CSS selector (no commas).
        class_names: The set of class tokens mined from matched markup elements.

    Returns:
        True when the subject's class tokens intersect with ``class_names``.
    """
    segments = _split_on_combinators(selector.strip())
    if not segments:
        return False
    subject = segments[-1]
    tokens = frozenset(_CLASS_TOKEN_RE.findall(subject))
    return bool(tokens & class_names)


def _extract_keyframe_names(declarations: str) -> set[str]:
    """Extract ``@keyframes`` names referenced by ``animation`` or ``animation-name`` declarations.

    Handles both ``animation-name: name`` and the shorthand
    ``animation: name duration easing``.  Comma-separated animation lists
    are each parsed separately.

    Args:
        declarations: CSS declaration block body text (between ``{`` and ``}``).

    Returns:
        Set of animation-name identifiers found in the declarations.
    """
    names: set[str] = set()

    # animation-name: name1, name2
    for m in re.finditer(r"\banimation-name\s*:\s*([^;{}]+)", declarations, re.IGNORECASE):
        for token in m.group(1).split(","):
            name = token.strip().rstrip(";")
            if name and name.lower() != "none":
                names.add(name)

    # animation shorthand: name is the first identifier token that is not
    # a known timing/direction/fill/count keyword and not a <time> value.
    for m in re.finditer(r"\banimation\s*:\s*([^;{}]+)", declarations, re.IGNORECASE):
        for layer in m.group(1).split(","):
            for token in layer.split():
                tok = token.strip().rstrip(";")
                if not tok:
                    continue
                if tok.lower() in _CSS_ANIM_NON_NAME_TOKENS:
                    continue
                if _CSS_TIMING_VALUE_RE.match(tok):
                    continue
                if re.match(r"^\d+$", tok):
                    continue
                if tok.startswith(("cubic-bezier(", "steps(")):
                    continue
                # First non-excluded token is the name
                names.add(tok)
                break

    return names


# ---------------------------------------------------------------------------
# CSS class-based rule filter
# ---------------------------------------------------------------------------

# At-rules whose body is itself a CSS rule block and should be recursed into.
_NESTED_AT_RULE_NAMES: Final[frozenset[str]] = frozenset(
    {"@media", "@supports", "@container", "@layer", "@scope"}
)


def css_rules_for_classes(css: str, class_names: frozenset[str]) -> str:
    """Return the subset of ``css`` whose rules' subjects reference ``class_names``.

    Uses ``iter_css_rules`` (the single shared CSS rule walker) to iterate
    top-level rules.  Two-pass algorithm:

    Pass 1 - accumulate:
      - Plain rules: keep when any selector in the comma-separated list has its
        rightmost segment referencing a class in ``class_names``.
      - Nested at-rules (``@media``, ``@supports``, etc.): recurse; include
        the block only when the recursed body is non-empty.
      - ``@keyframes`` / ``@-webkit-keyframes``: defer to pass 2.
      - Other at-rules (``@font-face``, ``@page``, etc.): drop (not
        component-scoped).

    Pass 2 - @keyframes:
      - Scan kept rule bodies for ``animation`` / ``animation-name`` values.
      - Include ``@keyframes`` whose name is referenced by at least one kept rule.

    Args:
        css: Raw CSS text from the whole's ``<style>`` block.
        class_names: Frozen set of class tokens from the matched markup elements.

    Returns:
        Filtered CSS string, raw (unscoped), preserving leading whitespace of
        each kept rule.
    """
    kept_parts: list[str] = []  # pass 1 output (non-keyframe rules)
    kept_declarations: list[str] = []  # bodies of kept plain rules (for pass 2)
    deferred_keyframes: list[CssRule] = []  # @keyframes rules for pass 2

    for rule in iter_css_rules(css):
        if not rule.prelude:
            # Sentinel: trailing/unbalanced text. Drop silently.
            continue

        if not rule.at_name:
            # Plain rule: check selector list.
            selectors = _split_comma_list(rule.prelude)
            if any(_selector_subject_matches(s, class_names) for s in selectors):
                kept_parts.append(f"{rule.prefix}{rule.prelude}{{{rule.body}}}")
                kept_declarations.append(rule.body)

        elif rule.at_name in {"@keyframes", "@-webkit-keyframes"}:
            # Defer until we know which names are referenced.
            deferred_keyframes.append(rule)

        elif rule.at_name in _NESTED_AT_RULE_NAMES:
            # Recurse into @media / @supports / etc.
            inner = css_rules_for_classes(rule.body, class_names)
            if inner.strip():
                kept_parts.append(f"{rule.prefix}{rule.prelude}{{{inner}}}")

        # else: @font-face, @page, @property, unknown at-rules: drop.

    # Pass 2: include @keyframes whose name appears in any kept declaration body.
    referenced_names: set[str] = set()
    for body in kept_declarations:
        referenced_names |= _extract_keyframe_names(body)

    for rule in deferred_keyframes:
        # The keyframe name is the last word of the prelude after the at-keyword.
        # e.g. "@keyframes btn-fade" -> name is "btn-fade"
        prelude_parts = rule.prelude.strip().split()
        kf_name = prelude_parts[-1] if len(prelude_parts) >= 2 else ""
        if kf_name in referenced_names:
            kept_parts.append(f"{rule.prefix}{rule.prelude}{{{rule.body}}}")

    return "".join(kept_parts)


# ---------------------------------------------------------------------------
# HTML subtree extraction
# ---------------------------------------------------------------------------

# Known HTML5 void elements that never have a matching end tag.
# Tracking their depth separately prevents phantom depth inflation when they
# appear inside a captured subtree.
_HTML5_VOID_ELEMENTS: Final[frozenset[str]] = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


class _FragmentExtractor(HTMLParser):
    """SAX-style extractor that lifts matching atom elements from HTML.

    Accumulates ``self.completed_fragments`` as a list of
    ``(raw_html: str, classes: list[str])`` tuples - one per matched top-level
    element subtree.  Nested matching elements are NOT captured separately;
    only root-level matches are lifted.

    Position tracking
    -----------------
    ``html.parser`` returns ``getpos()`` as ``(line, col)`` where ``line`` is
    1-indexed and ``col`` is 0-indexed.  ``_pos_to_offset`` converts that pair
    to a byte offset in ``self._raw``.

    The raw HTML is normalised to ``\\n`` line endings before feeding the
    parser so that the line/col arithmetic is consistent on all platforms.

    Limitations
    -----------
    - End-of-tag detection uses ``raw.find('>', pos)`` which fails if a ``>``
      appears inside an attribute value.  DRL assets do not produce this case.
    - Depth counting for void elements relies on ``_HTML5_VOID_ELEMENTS``;
      non-standard void elements are not handled.
    """

    def __init__(self, hint: AtomHint, raw_html: str) -> None:
        super().__init__(convert_charrefs=False)
        self._hint = hint
        # Normalise to LF so getpos() col arithmetic is consistent.
        self._raw = raw_html.replace("\r\n", "\n").replace("\r", "\n")
        self._lines = self._raw.split("\n")
        self._depth = 0
        # Stack of active captures.  We only push when NOT already inside a
        # capture, so nested matching elements are not double-captured.
        self._capture_stack: list[dict[str, object]] = []
        self.completed_fragments: list[tuple[str, list[str]]] = []

    # ------------------------------------------------------------------
    # Position helpers
    # ------------------------------------------------------------------

    def _pos_to_offset(self) -> int:
        """Convert current ``getpos()`` (1-indexed line, 0-indexed col) to char offset."""
        line, col = self.getpos()
        return sum(len(self._lines[i]) + 1 for i in range(line - 1)) + col

    # ------------------------------------------------------------------
    # Element matching
    # ------------------------------------------------------------------

    def _element_matches(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> tuple[bool, list[str]]:
        """Return ``(matches_hint, class_list)`` for an element.

        An element matches when its tag is in ``hint.tags`` OR any of its
        class tokens contains any string in ``hint.class_substrings``
        (case-insensitive substring match).
        """
        attr_dict = dict(attrs)
        classes = (attr_dict.get("class") or "").split()
        tag_match = tag.lower() in self._hint.tags
        class_match = any(
            sub in cls.lower()
            for cls in classes
            for sub in self._hint.class_substrings
        )
        return (tag_match or class_match), classes

    # ------------------------------------------------------------------
    # HTMLParser event handlers
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        # Void elements do not nest, so their depth is not tracked.
        if tag_lower not in _HTML5_VOID_ELEMENTS:
            self._depth += 1

        matches, classes = self._element_matches(tag, attrs)
        if matches and not self._capture_stack:
            # Begin capturing this top-level matching element.
            self._capture_stack.append(
                {
                    "tag": tag_lower,
                    "depth": self._depth,
                    "start": self._pos_to_offset(),
                    "classes": classes,
                }
            )
        # If already inside a capture, we continue accumulating depth
        # but do not start a nested capture.

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if (
            self._capture_stack
            and tag_lower == self._capture_stack[-1]["tag"]
            and self._depth == self._capture_stack[-1]["depth"]
        ):
            cap = self._capture_stack.pop()
            offset = self._pos_to_offset()
            # offset is the '<' of '</tag>'; find the matching '>'.
            gt = self._raw.find(">", offset)
            end = (gt + 1) if gt != -1 else len(self._raw)
            fragment = self._raw[cap["start"] : end]  # type: ignore[index]
            self.completed_fragments.append((fragment, cap["classes"]))  # type: ignore[arg-type]
        if tag_lower not in _HTML5_VOID_ELEMENTS:
            self._depth -= 1

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Handle XHTML-style self-closing tags (``<input />``)."""
        matches, classes = self._element_matches(tag, attrs)
        if matches and not self._capture_stack:
            offset = self._pos_to_offset()
            gt = self._raw.find(">", offset)
            end = (gt + 1) if gt != -1 else len(self._raw)
            fragment = self._raw[offset:end]
            self.completed_fragments.append((fragment, classes))
        # Void/self-closing elements do not change _depth.


def find_atom_fragments(
    whole_html: str, atom_class: str
) -> tuple[str, list[str]]:
    """Extract and group atom elements from ``whole_html`` matching ``atom_class``.

    Uses ``html.parser`` (not regex) to find matching element subtrees and
    lifts them out of the whole's layout context.  All matched elements are
    grouped under a ``<div class="rs-mined-group" data-rs-mined-from="...">``
    wrapper so the caller receives a self-contained fragment.

    Args:
        whole_html: Full text of the DRL ``asset.html`` for the whole.
        atom_class: Resemblio atom class name, e.g. ``"buttons"``.  Must be a
            key in ``ATOM_DETECTION_HINTS``; unknown classes return ``('', [])``.

    Returns:
        ``(fragment_html, source_classes)`` where ``fragment_html`` is the
        grouped markup (comment-stripped) or an empty string when no match,
        and ``source_classes`` is the sorted deduplicated list of class tokens
        found on all matched elements.
    """
    hint = ATOM_DETECTION_HINTS.get(atom_class)
    if hint is None:
        return "", []

    extractor = _FragmentExtractor(hint, whole_html)
    extractor.feed(extractor._raw)  # feed the normalised text

    if not extractor.completed_fragments:
        return "", []

    # Collect source classes from all matched elements (dedup + sort).
    all_classes: set[str] = set()
    raw_fragments: list[str] = []
    for frag_html, classes in extractor.completed_fragments:
        all_classes.update(classes)
        raw_fragments.append(strip_provenance_comments(frag_html))

    source_classes = sorted(all_classes)
    inner = "\n  ".join(raw_fragments)
    fragment_html = (
        f'<div class="rs-mined-group" data-rs-mined-from="{atom_class}">\n'
        f"  {inner}\n"
        f"</div>"
    )
    return fragment_html, source_classes


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def mine_atom_from_whole(whole_html: str, atom_class: str) -> MinedAtom | None:
    """Extract the embedded ``atom_class`` atom from a captured whole's ``asset.html``.

    Orchestrates the four extraction steps:
    1. Extract the ``<style>`` block CSS from the whole.
    2. Find and group matching markup elements via ``find_atom_fragments``.
    3. Filter CSS to only the rules whose subject references the matched classes.
    4. Derive interaction states from the filtered CSS.

    Returns ``None`` when no element of the requested atom class is present in
    the whole.  The caller (persistence layer, issue #28) renders the honest
    gap in that case.

    DRL read-only: this function accepts the whole HTML as a string; it never
    opens or writes any file under the Design Reference Library.

    Args:
        whole_html: Full text of a DRL ``asset.html`` whole.
        atom_class: Resemblio atom class name, e.g. ``"buttons"``.

    Returns:
        ``MinedAtom`` carrying extracted markup + CSS + metadata, or ``None``.
    """
    # Step 1: extract raw CSS from <style> blocks (comment-stripped).
    style_blocks = _STYLE_BLOCK_RE.findall(whole_html)
    raw_css = strip_provenance_comments("\n".join(style_blocks))

    # Step 2: find matching markup elements and their class set.
    fragment_html, source_classes = find_atom_fragments(whole_html, atom_class)
    if not fragment_html:
        return None

    # Step 3: filter CSS to rules whose subject references the matched classes.
    class_names = frozenset(source_classes)
    component_css = css_rules_for_classes(raw_css, class_names)

    # Step 4: derive interaction states from the filtered (already stripped) CSS.
    states_present = derive_states_present(component_css)

    return MinedAtom(
        atom_class=atom_class,
        component_html=fragment_html,
        component_css=component_css,
        states_present=states_present,
        source_classes=source_classes,
        schema_version="whole_mining_v1",
    )
