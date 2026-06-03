"""Unit tests for app.library_style_scope.

Each test docstring states the rule the assertion proves. Tests use only
synthetic fixtures - no network, no DRL template loading - per the
workspace quality floor.
"""

from __future__ import annotations

from app.library_style_scope import scope_style_block


def _normalize(s: str) -> str:
    """Collapse runs of whitespace so assertions are robust to formatting."""
    return " ".join(s.split())


def test_html_root_selector_is_stripped_and_replaced():
    """`html { ... }` collapses to the wrapper (we are inside an article)."""
    out = scope_style_block("html { margin: 0; }")
    assert _normalize(out) == ".rs-library-page { margin: 0; }"


def test_body_root_selector_is_stripped_and_replaced():
    """`body { ... }` collapses to the wrapper for the same reason as html."""
    out = scope_style_block("body { background: #fff; }")
    assert _normalize(out) == ".rs-library-page { background: #fff; }"


def test_universal_selector_is_scoped():
    """`*` becomes `<wrapper> *` so reset rules stay inside the article."""
    out = scope_style_block("* { box-sizing: border-box; }")
    assert _normalize(out) == ".rs-library-page * { box-sizing: border-box; }"


def test_universal_with_pseudo_elements_is_scoped():
    """`*, *::before, *::after` reset preamble (DRL pattern) all get scoped."""
    out = scope_style_block("*, *::before, *::after { box-sizing: border-box; }")
    norm = _normalize(out)
    assert ".rs-library-page *" in norm
    assert ".rs-library-page *::before" in norm
    assert ".rs-library-page *::after" in norm


def test_bare_class_selector_is_prefixed():
    """Bare class selector `.b-btn` gains the wrapper prefix."""
    out = scope_style_block(".b-btn { padding: 8px; }")
    assert _normalize(out) == ".rs-library-page .b-btn { padding: 8px; }"


def test_root_custom_properties_are_preserved():
    """`:root { ... }` must NOT be scoped; custom-property cascade is global."""
    out = scope_style_block(":root { --ds-bg: #fff; }")
    assert _normalize(out) == ":root { --ds-bg: #fff; }"


def test_media_query_inner_selectors_are_rewritten():
    """Selectors nested in @media blocks get rewritten via recursion."""
    src = "@media (min-width: 600px) { .b-btn { padding: 12px; } }"
    out = scope_style_block(src)
    norm = _normalize(out)
    assert "@media (min-width: 600px)" in norm
    assert ".rs-library-page .b-btn" in norm


def test_supports_block_recurses():
    """@supports nested selectors are also rewritten."""
    src = "@supports (display: grid) { .l-grid { display: grid; } }"
    out = scope_style_block(src)
    assert ".rs-library-page .l-grid" in _normalize(out)


def test_idempotent_on_already_scoped_css():
    """Running twice does not double-prefix already-scoped selectors."""
    src = ".rs-library-page .b-btn { padding: 8px; }"
    once = scope_style_block(src)
    twice = scope_style_block(once)
    assert _normalize(once) == _normalize(twice)
    assert ".rs-library-page .rs-library-page" not in twice


def test_idempotent_on_universal_reset():
    """The reset-preamble special case is also idempotent."""
    src = "* { box-sizing: border-box; }"
    once = scope_style_block(src)
    twice = scope_style_block(once)
    assert _normalize(once) == _normalize(twice)


def test_comma_separated_selectors_each_get_prefix():
    """Each selector in a comma list gets its own prefix."""
    out = scope_style_block(".b-btn, .b-card { color: red; }")
    norm = _normalize(out)
    assert ".rs-library-page .b-btn" in norm
    assert ".rs-library-page .b-card" in norm


def test_pseudo_class_on_class_selector_is_preserved():
    """`.b-btn:hover` keeps its pseudo-class after scoping."""
    out = scope_style_block(".b-btn:hover { color: red; }")
    assert _normalize(out) == ".rs-library-page .b-btn:hover { color: red; }"


def test_attribute_selector_is_prefixed():
    """Attribute selectors are bare selectors and get prefixed."""
    out = scope_style_block('[data-foo="bar"] { display: none; }')
    assert _normalize(out) == '.rs-library-page [data-foo="bar"] { display: none; }'


def test_comments_are_preserved():
    """CSS comments survive the rewrite intact."""
    src = "/* preamble */ .b-btn { color: red; }"
    out = scope_style_block(src)
    assert "/* preamble */" in out
    assert ".rs-library-page .b-btn" in _normalize(out)


def test_empty_rule_is_prefixed():
    """An empty rule still gets scoped (no declarations is fine)."""
    out = scope_style_block(".foo {}")
    assert _normalize(out) == ".rs-library-page .foo {}"


def test_keyframes_inner_percentages_are_not_rewritten():
    """@keyframes percentage keys must not be treated as selectors."""
    src = "@keyframes spin { 0% { opacity: 0; } 100% { opacity: 1; } }"
    out = scope_style_block(src)
    # The body is passed through verbatim; no wrapper prefix injected.
    assert ".rs-library-page" not in out
    assert "0% { opacity: 0; }" in _normalize(out)


def test_font_face_passes_through():
    """@font-face has no real selectors and must not be prefixed."""
    src = '@font-face { font-family: "Foo"; src: url("foo.woff2"); }'
    out = scope_style_block(src)
    assert ".rs-library-page" not in out


def test_element_selector_is_prefixed():
    """Bare element selectors like `p` get prefixed."""
    out = scope_style_block("p { margin: 0; }")
    assert _normalize(out) == ".rs-library-page p { margin: 0; }"


def test_custom_wrapper_is_honored():
    """Passing a non-default wrapper_selector uses that prefix instead."""
    out = scope_style_block(".b-btn { color: red; }", wrapper_selector=".x-wrap")
    assert _normalize(out) == ".x-wrap .b-btn { color: red; }"


def test_empty_input_returns_empty():
    """Empty / whitespace-only input round-trips unchanged."""
    assert scope_style_block("") == ""
    assert scope_style_block("   \n  ") == "   \n  "


def test_html_descendant_keeps_descendant_after_strip():
    """`html .foo` becomes `<wrapper> .foo`, not double-wrapped."""
    out = scope_style_block("html .foo { color: red; }")
    assert _normalize(out) == ".rs-library-page .foo { color: red; }"
