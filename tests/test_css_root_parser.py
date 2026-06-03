"""Unit tests for extractor.css_root_parser.

The parser is the R3.2 deterministic pre-LLM signal that closes the
"missed :root custom-property declarations" diagnostic from the Susann
extraction-fidelity investigation. These tests exercise the pure-data
shape: HTML in, structured RootCustomProperties out, no network.

Source dispatch: projects/Resemblio/_handoff/inbox/claude/
2026-06-02-susann-extraction-fidelity-investigation.md
"""
from __future__ import annotations

from extractor.css_root_parser import (
    MAX_CAPTURED_PROPERTIES,
    MAX_PROPERTY_VALUE_LENGTH,
    SCHEMA_VERSION,
    parse_root_custom_properties,
    render_for_prompt,
)


def test_empty_or_invalid_input_returns_well_formed_empty_result() -> None:
    """Empty or non-string inputs produce a well-formed empty report."""
    for value in ("", None, "<html></html>", "<style></style>", "not html"):
        result = parse_root_custom_properties(value)  # type: ignore[arg-type]
        assert result["properties"] == []
        assert result["properties_by_name"] == {}
        assert result["schema_version"] == SCHEMA_VERSION


def test_susann_style_root_block_captures_brand_tokens() -> None:
    """Susann's :root brand tokens (ink/bone/sun + Anton/Inter) are captured.

    This is the flagship R3.2 fixture: the exact CSS-variable indirection
    pattern that defeated the pre-R3.2 LLM-only extractor.
    """
    html = """
    <html><head><style>
      :root {
        color-scheme: dark;
        --ink: #0B0B0F;
        --bone: #F5F2EA;
        --sun: #FBE71F;
        --type-display: "Anton", "Bebas Neue", sans-serif;
        --type-body: "Inter", -apple-system, sans-serif;
      }
      html, body { background: var(--ink); color: var(--bone); }
    </style></head><body></body></html>
    """
    result = parse_root_custom_properties(html)
    by_name = result["properties_by_name"]
    assert by_name["ink"] == "#0B0B0F"
    assert by_name["bone"] == "#F5F2EA"
    assert by_name["sun"] == "#FBE71F"
    assert "Anton" in by_name["type-display"]
    assert "Inter" in by_name["type-body"]


def test_html_body_selector_is_root_level() -> None:
    """`html, body { --x: ...; }` is treated as a root-level declaration."""
    html = "<style>html, body { --primary: #FF00FF; }</style>"
    result = parse_root_custom_properties(html)
    assert result["properties_by_name"]["primary"] == "#FF00FF"


def test_body_dot_dark_selector_is_root_level() -> None:
    """Class-suffixed root-level selectors (e.g. `body.dark`) still capture.

    Dark-mode brand sites commonly declare overrides under `body.dark`.
    The selector-canonical check strips the first non-identifier suffix
    so the segment `body` matches the root-level whitelist.
    """
    html = "<style>body.dark { --bg: #000000; }</style>"
    result = parse_root_custom_properties(html)
    assert result["properties_by_name"]["bg"] == "#000000"


def test_component_scoped_custom_property_is_ignored() -> None:
    """Custom properties declared on a component selector are NOT captured.

    `.btn-primary { --hover-bg: red; }` is not a brand token; only
    declarations on `:root`, `html`, `body`, or their suffix-stripped
    variants count.
    """
    html = "<style>.btn-primary { --hover-bg: red; }</style>"
    result = parse_root_custom_properties(html)
    assert result["properties"] == []


def test_commented_out_declaration_is_ignored() -> None:
    """A commented-out --x is stripped before declaration scanning."""
    html = """
    <style>:root {
      /* --fake: tomato; */
      --real: #112233;
    }</style>
    """
    result = parse_root_custom_properties(html)
    assert "fake" not in result["properties_by_name"]
    assert result["properties_by_name"]["real"] == "#112233"


def test_last_declaration_wins_within_same_selector() -> None:
    """Re-declared property within the same rule: last value wins."""
    html = "<style>:root { --x: red; --x: blue; }</style>"
    result = parse_root_custom_properties(html)
    assert result["properties_by_name"]["x"] == "blue"


def test_long_value_is_truncated_with_marker() -> None:
    """Values longer than MAX_PROPERTY_VALUE_LENGTH are truncated."""
    long_value = "rgba(0,0,0,0.5) " * 60  # ~960 chars
    html = f"<style>:root {{ --shadow: {long_value}; }}</style>"
    result = parse_root_custom_properties(html)
    captured = result["properties_by_name"]["shadow"]
    assert len(captured) <= MAX_PROPERTY_VALUE_LENGTH
    assert captured.endswith("/*...*/")


def test_render_for_prompt_lists_every_capture() -> None:
    """The prompt renderer lists every captured declaration in source order."""
    html = """
    <style>:root {
      --ink: #0B0B0F;
      --sun: #FBE71F;
    }</style>
    """
    result = parse_root_custom_properties(html)
    rendered = render_for_prompt(result)
    assert "Declared CSS custom properties" in rendered
    assert "--ink: #0B0B0F" in rendered
    assert "--sun: #FBE71F" in rendered


def test_render_for_prompt_on_empty_returns_empty_string() -> None:
    """Empty input renders nothing so the caller can omit the section."""
    result = parse_root_custom_properties("<html></html>")
    assert render_for_prompt(result) == ""


def test_multiple_style_blocks_are_all_scanned() -> None:
    """Brand tokens sometimes live in a separate inline block from page CSS."""
    html = """
    <style>:root { --ink: #0B0B0F; }</style>
    <style>:root { --sun: #FBE71F; }</style>
    """
    result = parse_root_custom_properties(html)
    assert result["properties_by_name"]["ink"] == "#0B0B0F"
    assert result["properties_by_name"]["sun"] == "#FBE71F"


def test_cap_on_captured_properties_does_not_break() -> None:
    """Pathological inputs with many declarations are bounded, not crashed."""
    declarations = "\n".join(
        f"--p{i}: #{i:06x};" for i in range(MAX_CAPTURED_PROPERTIES + 25)
    )
    html = f"<style>:root {{ {declarations} }}</style>"
    result = parse_root_custom_properties(html)
    assert len(result["properties"]) <= MAX_CAPTURED_PROPERTIES
