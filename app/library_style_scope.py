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

    Splits on matched braces (not naive ``}`` splits, which would corrupt
    nested at-rules). For each rule, decides whether it is a plain rule
    (rewrite the selector list), a nested at-rule (recurse), a declaration
    at-rule (pass through), or a named at-rule like @keyframes (pass through).
    """
    out: list[str] = []
    i = 0
    n = len(css_text)
    while i < n:
        # Preserve leading whitespace / comments verbatim
        ws_match = re.match(r"\s+", css_text[i:])
        if ws_match:
            out.append(ws_match.group(0))
            i += ws_match.end()
            continue
        comment_match = _COMMENT_RE.match(css_text, i)
        if comment_match:
            out.append(comment_match.group(0))
            i = comment_match.end()
            continue
        # Find the next '{' that opens a block
        brace_open = css_text.find("{", i)
        if brace_open == -1:
            # Trailing content with no block; keep verbatim
            out.append(css_text[i:])
            break
        prelude = css_text[i:brace_open]
        body_start = brace_open + 1
        body_end = _find_matching_brace(css_text, brace_open)
        if body_end == -1:
            # Unbalanced; bail out and keep remainder verbatim
            out.append(css_text[i:])
            break
        body = css_text[body_start:body_end]
        prelude_stripped = prelude.strip()
        if prelude_stripped.startswith("@"):
            at_name = _at_rule_name(prelude_stripped)
            if at_name in _NESTED_AT_RULES:
                rewritten_body = _rewrite_block(body, wrapper)
                out.append(f"{prelude}{{{rewritten_body}}}")
            else:
                # @font-face / @keyframes / @page / @property / unknown:
                # pass through unchanged (no real selectors to rewrite).
                _ = _DECLARATION_AT_RULES, _NAMED_AT_RULES  # documented intent
                out.append(f"{prelude}{{{body}}}")
        else:
            rewritten_prelude = _rewrite_selector_list(prelude, wrapper)
            out.append(f"{rewritten_prelude}{{{body}}}")
        i = body_end + 1
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
