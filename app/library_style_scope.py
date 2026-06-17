"""Regex-based CSS selector scoping for library-page style blocks.

Vendored DRL templates emit CSS that assumes a full-document context:
``*, *::before, *::after { box-sizing: border-box }``, ``html, body``
preambles, and bare element/class selectors. When those blocks are
inlined into a per-class library article, the rules leak out of the
article's scope and repaint the surrounding Next.js page chrome.

This module rewrites bare selectors in a CSS block to be prefixed by a
wrapper selector (default ``.rs-library-page``) so the rules only match
inside the article. The implementation is deliberately regex-based, not
a full CSS parser: there is no ``cssutils`` dependency in this service's
``pyproject.toml`` and the input shape is the small, well-known subset
that DRL emits. The trade-off is documented: pathological CSS (deeply
nested ``@supports`` chains, escaped braces inside string values) is
out of scope. The function is idempotent: re-running on already-scoped
CSS does not double-prefix.

Public exports
--------------
``CssRule`` and ``iter_css_rules`` are the shared CSS-rule iterator used
by both this module (``scope_style_block``) and ``app.whole_mining``
(``css_rules_for_classes``). There is exactly ONE CSS rule parser in the
codebase; callers must not write a second one.

Edge cases preserved:
  - ``:root { ... }`` declarations (custom-property cascade is global)
  - ``@font-face``, ``@keyframes`` at-rules (no selectors to rewrite)
  - ``@media`` and ``@supports`` blocks (recurse into the inner CSS)
  - Selectors already prefixed by the wrapper (idempotent)
  - Pseudo-elements / pseudo-classes / attribute selectors on bare names

Edge cases rewritten:
  - ``html`` and ``body`` are stripped from positional selectors entirely
    (we are inside an article, not a document, so ``html { margin: 0 }``
    becomes ``.rs-library-page { margin: 0 }``)
  - ``*`` becomes ``<wrapper> *``
  - Bare element selectors (``p``, ``a``, ``button``) get prefixed
  - Bare class / attribute / id selectors get prefixed
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

# Default wrapper for library-page article fragments. Centralized so the
# caller in library_indexer.py and the test suite share one source of
# truth; downstream tools that compose articles under a different class
# can pass a custom wrapper_selector.
DEFAULT_WRAPPER_SELECTOR: Final[str] = ".rs-library-page"

# At-rules whose body is plain declarations (no selectors). We keep
# these blocks intact rather than recurse.
_DECLARATION_AT_RULES: Final[frozenset[str]] = frozenset(
    {"@font-face", "@page", "@counter-style", "@property"}
)

# At-rules whose body is plain declarations AND that target a name,
# not a selector list. ``@keyframes name { 0% { ... } 100% { ... } }``
# contains percentage "selectors" that must not be prefixed.
_NAMED_AT_RULES: Final[frozenset[str]] = frozenset({"@keyframes", "@-webkit-keyframes"})

# At-rules whose body is itself a CSS block that DOES contain real
# selectors and therefore must be recursed into.
_NESTED_AT_RULES: Final[frozenset[str]] = frozenset(
    {"@media", "@supports", "@container", "@layer", "@scope"}
)

# Selectors that should be stripped entirely (replaced by just the
# wrapper) because the rule targets the document root and we are
# inside an article.
_DOCUMENT_ROOT_SELECTORS: Final[frozenset[str]] = frozenset({"html", "body"})

# Matches a CSS comment so we can mask comments before tokenization.
_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)


# ---------------------------------------------------------------------------
# Shared CSS rule iterator (used by scope_style_block AND app.whole_mining)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CssRule:
    """One top-level CSS rule as yielded by ``iter_css_rules``.

    Fields
    ------
    prefix
        Whitespace and comments that appear BEFORE this rule in the source.
        Preserving this field lets callers reconstruct the full CSS text
        faithfully (round-trip safe).
    prelude
        Everything before the opening ``{``: a selector list for plain rules,
        or ``@media (...)`` / ``@keyframes name`` for at-rules. Empty string
        for the terminal sentinel (see below).
    body
        Everything between the opening ``{`` and its matching ``}``. Empty
        string for the terminal sentinel.
    at_name
        The lowercased at-rule keyword (e.g. ``'@media'``, ``'@keyframes'``),
        or ``''`` for plain selector rules and the terminal sentinel.

    Terminal sentinel
        When the CSS ends with trailing text that has no rule block (orphan
        comments, whitespace, malformed input), ``iter_css_rules`` emits one
        final ``CssRule`` with empty ``prelude`` and ``body`` and the
        remaining text in ``prefix``. Callers can detect this case with
        ``not rule.prelude``.
    """

    prefix: str
    prelude: str
    body: str
    at_name: str


def iter_css_rules(css: str) -> Iterator[CssRule]:
    """Iterate over top-level CSS rules in ``css``, yielding one ``CssRule`` each.

    Handles brace-balanced rule bodies correctly - nested ``@media`` /
    ``@supports`` blocks are NOT split on inner ``}`` characters. This is
    the single CSS rule walker for the codebase; ``scope_style_block`` and
    ``app.whole_mining.css_rules_for_classes`` both use it.

    The ``prefix`` field of each yielded ``CssRule`` carries whitespace and
    comments that precede the rule. Concatenating ``rule.prefix + rule.prelude
    + '{' + rule.body + '}'`` for every non-sentinel rule (plus the sentinel's
    ``prefix``) reconstructs the original text.

    Edge cases
    ----------
    - Trailing content (text after the last ``}`` with no new ``{``) is
      emitted as a sentinel with empty ``prelude`` / ``body`` and the
      remaining text as ``prefix``.
    - Unbalanced braces cause the remaining text to be emitted the same way
      (sentinel with the rest in ``prefix``).
    - Empty or whitespace-only input emits a single sentinel.

    Args:
        css: Raw CSS text. May contain comments, at-rules, and nested blocks.

    Yields:
        ``CssRule`` instances in source order; last item may be a sentinel.
    """
    i = 0
    n = len(css)

    while i < n:
        # -- accumulate leading whitespace and comments into prefix ----------
        prefix_parts: list[str] = []
        while i < n:
            ws = re.match(r"\s+", css[i:])
            if ws:
                prefix_parts.append(ws.group(0))
                i += ws.end()
                continue
            cm = _COMMENT_RE.match(css, i)
            if cm:
                prefix_parts.append(cm.group(0))
                i = cm.end()
                continue
            break  # non-whitespace, non-comment: start of rule prelude
        prefix = "".join(prefix_parts)

        if i >= n:
            # Only whitespace/comments left - emit as sentinel
            if prefix:
                yield CssRule(prefix=prefix, prelude="", body="", at_name="")
            return

        # -- find the opening brace of the next rule -------------------------
        brace_open = css.find("{", i)
        if brace_open == -1:
            # Trailing content with no block
            yield CssRule(prefix=prefix + css[i:], prelude="", body="", at_name="")
            return

        prelude = css[i:brace_open]
        brace_close = _find_matching_brace(css, brace_open)
        if brace_close == -1:
            # Unbalanced braces: emit the remainder as a sentinel
            yield CssRule(prefix=prefix + css[i:], prelude="", body="", at_name="")
            return

        body = css[brace_open + 1 : brace_close]
        prelude_stripped = prelude.strip()
        at_name = (
            _at_rule_name(prelude_stripped) if prelude_stripped.startswith("@") else ""
        )

        yield CssRule(prefix=prefix, prelude=prelude, body=body, at_name=at_name)
        i = brace_close + 1


def scope_style_block(
    css_text: str, wrapper_selector: str = DEFAULT_WRAPPER_SELECTOR
) -> str:
    """Rewrite bare selectors in ``css_text`` to be scoped under ``wrapper_selector``.

    See module docstring for the full edge-case table. Returns the rewritten
    CSS as a string. Idempotent: a second pass produces the same output as
    the first. Whitespace in declaration bodies is preserved as-is; only
    selector lists are tokenized and rewritten.

    Args:
        css_text: The CSS source. May contain comments, at-rules, and
            nested blocks. Pathological inputs (escaped braces inside
            strings, deeply nested ``@supports``) are out of scope.
        wrapper_selector: The selector to prefix bare selectors with.
            Defaults to ``.rs-library-page``.

    Returns:
        The rewritten CSS. Empty input returns empty output.
    """
    if not css_text or not css_text.strip():
        return css_text
    return _rewrite_block(css_text, wrapper_selector)


def _rewrite_block(css_text: str, wrapper: str) -> str:
    """Walk top-level rules in ``css_text`` and rewrite each one.

    Delegates rule iteration to ``iter_css_rules`` (the single CSS rule
    parser). For each rule, decides whether it is a plain rule (rewrite
    the selector list), a nested at-rule (recurse), or any other at-rule
    like @keyframes / @font-face (pass through unchanged).
    """
    out: list[str] = []
    for rule in iter_css_rules(css_text):
        out.append(rule.prefix)
        if not rule.prelude:
            # Sentinel: trailing/unbalanced text already emitted in prefix.
            continue
        if rule.at_name in _NESTED_AT_RULES:
            rewritten_body = _rewrite_block(rule.body, wrapper)
            out.append(f"{rule.prelude}{{{rewritten_body}}}")
        elif rule.at_name:
            # @font-face / @keyframes / @page / @property / unknown:
            # pass through unchanged (no real selectors to rewrite).
            _ = _DECLARATION_AT_RULES, _NAMED_AT_RULES  # documented intent
            out.append(f"{rule.prelude}{{{rule.body}}}")
        else:
            rewritten_prelude = _rewrite_selector_list(rule.prelude, wrapper)
            out.append(f"{rewritten_prelude}{{{rule.body}}}")
    return "".join(out)


def _find_matching_brace(text: str, open_idx: int) -> int:
    """Return the index of the '}' that matches the '{' at ``open_idx``.

    Returns -1 if no match is found. Aware of nested braces; not aware
    of braces inside string literals (CSS rarely contains them; the
    DRL emitter does not).
    """
    depth = 0
    for j in range(open_idx, len(text)):
        ch = text[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _at_rule_name(prelude: str) -> str:
    """Extract the at-rule name (e.g. '@media') from a prelude string."""
    match = re.match(r"@[-a-zA-Z]+", prelude)
    return match.group(0).lower() if match else ""


def _rewrite_selector_list(prelude: str, wrapper: str) -> str:
    """Rewrite a comma-separated selector list, preserving surrounding ws.

    Splits on commas that are not inside parentheses (e.g. ``:is(a, b)``).
    Each selector is fed through ``_rewrite_one_selector``.
    """
    leading_ws_match = re.match(r"\s*", prelude)
    trailing_ws_match = re.search(r"\s*$", prelude)
    leading_ws = leading_ws_match.group(0) if leading_ws_match else ""
    trailing_ws = trailing_ws_match.group(0) if trailing_ws_match else ""
    core = prelude[len(leading_ws) : len(prelude) - len(trailing_ws)]
    selectors = _split_selector_list(core)
    rewritten = [_rewrite_one_selector(s, wrapper) for s in selectors]
    # Filter out empties that came from html/body strip with no descendant
    rewritten = [s for s in rewritten if s.strip()]
    if not rewritten:
        # Degenerate: every selector was html/body alone; replace with wrapper
        rewritten = [wrapper]
    return f"{leading_ws}{', '.join(rewritten)} {trailing_ws.lstrip(' ')}".rstrip(" ") + (
        " " if trailing_ws and not trailing_ws.startswith("\n") else trailing_ws
    )


def _split_selector_list(selector_list: str) -> list[str]:
    """Split a selector list on top-level commas (not inside parens)."""
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


def _rewrite_one_selector(selector: str, wrapper: str) -> str:
    """Rewrite a single selector. See module docstring for the rule table."""
    stripped = selector.strip()
    if not stripped:
        return ""
    # :root cascades globally; never scope it.
    if stripped == ":root" or stripped.startswith(":root "):
        return stripped
    # Already prefixed: idempotent
    if stripped == wrapper or stripped.startswith(f"{wrapper} ") or stripped.startswith(
        f"{wrapper}:"
    ) or stripped.startswith(f"{wrapper}."):
        return stripped
    # Document-root selectors: strip and replace with wrapper.
    # ``html``, ``body``, ``html.foo``, ``body.dark`` all collapse.
    first_token = re.match(r"[a-zA-Z][-a-zA-Z0-9]*", stripped)
    if first_token and first_token.group(0).lower() in _DOCUMENT_ROOT_SELECTORS:
        rest = stripped[first_token.end() :]
        # If the rest is empty or starts with whitespace (descendant),
        # we want "<wrapper> <rest>". If the rest starts with a combinator
        # like ., #, :, [, also keep it on wrapper.
        rest_stripped = rest.lstrip()
        if not rest_stripped:
            return wrapper
        if rest.startswith((".", "#", ":", "[")):
            # html.dark -> .rs-library-page.dark
            return f"{wrapper}{rest}"
        # html body, html > div, html .foo
        return f"{wrapper} {rest_stripped}"
    # Universal selector
    if stripped == "*":
        return f"{wrapper} *"
    # Bare element / class / id / attribute / universal-with-pseudo: prefix
    return f"{wrapper} {stripped}"
