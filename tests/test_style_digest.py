"""Unit tests for extractor.style_digest.

TDD anchor for the R3.1 Phase 1-4 digest pipeline.

Tests cover every public function:
  - resolve_var: literal passthrough, simple lookup, fallback, nested, cycles, partial
  - extract_brand_cascade: body/heading/link slots; var resolution; no-styles edge case
  - build_style_digest: Susann fixture; empty HTML; font-link fallback
  - render_digest_block: empty digest; populated digest

Plus the D3 boundary test: build_prompt for Susann HTML contains the resolved
brand values (proves the model is handed the right inputs without calling the LLM).

Run:
    RESEMBLIO_DISABLE_BROWSER_PASS=1 python -m pytest tests/test_style_digest.py -v
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from extractor.style_digest import (
    StyleDigest,
    build_style_digest,
    extract_brand_cascade,
    render_digest_block,
    resolve_var,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_SUSANN_LIKE_HTML = """\
<!doctype html>
<html lang="en">
<head>
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;700&display=swap" rel="stylesheet">
<style>
:root {
  --ink: #0B0B0F;
  --bone: #F5F2EA;
  --sun: #FBE71F;
  --type-display: "Anton", system-ui, sans-serif;
  --type-body: "Inter", -apple-system, sans-serif;
}
html, body {
  background: var(--ink);
  color: var(--bone);
  font-family: var(--type-body);
  font-size: 17px;
}
a { color: var(--sun); text-decoration: none; }
h1, h2, h3 { font-family: var(--type-display); }
</style>
</head>
<body><h1>Test</h1></body>
</html>"""

_PLAIN_HTML = """<!doctype html><html><body><p>Hello</p></body></html>"""

_VAR_MAP_SUSANN: dict[str, str] = {
    "ink": "#0B0B0F",
    "bone": "#F5F2EA",
    "sun": "#FBE71F",
    "type-display": '"Anton", system-ui, sans-serif',
    "type-body": '"Inter", -apple-system, sans-serif',
}

_FIXTURE_SUSANN = (
    Path(__file__).parent
    / "fixtures"
    / "extraction"
    / "001_susann_headlights"
    / "source.html"
)


# ---------------------------------------------------------------------------
# resolve_var
# ---------------------------------------------------------------------------


class TestResolveVar:
    """resolve_var: pure CSS var() resolver against a property map."""

    def test_literal_passes_through(self) -> None:
        """Literal values with no var() are returned unchanged."""
        assert resolve_var("#0B0B0F", {}) == "#0B0B0F"
        assert resolve_var("Inter, sans-serif", {}) == "Inter, sans-serif"
        assert resolve_var("17px", {}) == "17px"

    def test_empty_string_passes_through(self) -> None:
        """Empty string returns empty string."""
        assert resolve_var("", {}) == ""

    def test_simple_lookup(self) -> None:
        """var(--ink) resolves to the mapped value."""
        assert resolve_var("var(--ink)", _VAR_MAP_SUSANN) == "#0B0B0F"

    def test_fallback_used_when_missing(self) -> None:
        """var(--missing, #999) returns the fallback when name not in map."""
        assert resolve_var("var(--missing, #999)", {}) == "#999"

    def test_fallback_not_used_when_present(self) -> None:
        """var(--ink, #999) returns the map value, not the fallback."""
        assert resolve_var("var(--ink, #999)", _VAR_MAP_SUSANN) == "#0B0B0F"

    def test_nested_resolution(self) -> None:
        """--a: var(--b); --b: #red resolves two levels deep."""
        var_map = {"a": "var(--b)", "b": "#ff0000"}
        assert resolve_var("var(--a)", var_map) == "#ff0000"

    def test_cycle_safe(self) -> None:
        """A self-referential var(--a: var(--a)) does not loop; returns the token."""
        var_map = {"a": "var(--a)"}
        result = resolve_var("var(--a)", var_map)
        # The resolved value should be the var token itself (cycle detected at max depth).
        assert "var(--a)" in result

    def test_partial_var_in_value(self) -> None:
        """A value containing both literal text and var() resolves the var() portion."""
        # e.g. "0 1px 0 var(--border)" - the var() is replaced inline.
        result = resolve_var("0 1px 0 var(--ink)", _VAR_MAP_SUSANN)
        assert "#0B0B0F" in result

    def test_hyphenated_var_name(self) -> None:
        """Var names with hyphens (--type-body) resolve correctly."""
        result = resolve_var("var(--type-body)", _VAR_MAP_SUSANN)
        assert "Inter" in result

    def test_unresolvable_no_fallback_returns_original(self) -> None:
        """var(--x) with no entry and no fallback returns the original var() token."""
        result = resolve_var("var(--nonexistent)", {})
        assert result == "var(--nonexistent)"


# ---------------------------------------------------------------------------
# extract_brand_cascade
# ---------------------------------------------------------------------------


class TestExtractBrandCascade:
    """extract_brand_cascade: scan CSS rules and resolve brand slots."""

    def test_no_styles_returns_empty(self) -> None:
        """Plain HTML with no <style> block returns an empty list."""
        result = extract_brand_cascade(_PLAIN_HTML, {})
        assert result == []

    def test_susann_bg_resolved(self) -> None:
        """html,body { background: var(--ink) } resolves to bg=#0B0B0F."""
        slots = extract_brand_cascade(_SUSANN_LIKE_HTML, _VAR_MAP_SUSANN)
        bg_slots = [s for s in slots if s["slot"] == "bg"]
        assert bg_slots, "bg slot not extracted"
        assert bg_slots[-1]["value"] == "#0B0B0F"

    def test_susann_text_resolved(self) -> None:
        """html,body { color: var(--bone) } resolves to text=#F5F2EA."""
        slots = extract_brand_cascade(_SUSANN_LIKE_HTML, _VAR_MAP_SUSANN)
        text_slots = [s for s in slots if s["slot"] == "text"]
        assert text_slots, "text slot not extracted"
        assert text_slots[-1]["value"] == "#F5F2EA"

    def test_susann_accent_resolved(self) -> None:
        """a { color: var(--sun) } resolves to accent=#FBE71F."""
        slots = extract_brand_cascade(_SUSANN_LIKE_HTML, _VAR_MAP_SUSANN)
        accent_slots = [s for s in slots if s["slot"] == "accent"]
        assert accent_slots, "accent slot not extracted"
        assert "#FBE71F" in accent_slots[0]["value"]

    def test_susann_font_body_resolved(self) -> None:
        """html,body { font-family: var(--type-body) } resolves to font_body containing Inter."""
        slots = extract_brand_cascade(_SUSANN_LIKE_HTML, _VAR_MAP_SUSANN)
        font_body = [s for s in slots if s["slot"] == "font_body"]
        assert font_body, "font_body slot not extracted"
        assert "Inter" in font_body[-1]["value"]

    def test_susann_font_display_resolved(self) -> None:
        """h1,h2,h3 { font-family: var(--type-display) } resolves to font_display containing Anton."""
        slots = extract_brand_cascade(_SUSANN_LIKE_HTML, _VAR_MAP_SUSANN)
        font_display = [s for s in slots if s["slot"] == "font_display"]
        assert font_display, "font_display slot not extracted"
        assert "Anton" in font_display[0]["value"]

    def test_unresolvable_var_excluded(self) -> None:
        """A value containing unresolvable var() is excluded from output."""
        html = """\
<html><head><style>
html, body { background: var(--unknown); }
</style></head></html>"""
        slots = extract_brand_cascade(html, {})
        # The var(--unknown) can't resolve; bg slot should not appear.
        bg_slots = [s for s in slots if s["slot"] == "bg"]
        assert bg_slots == [], "unresolvable var should not produce a slot entry"

    def test_literal_value_without_var(self) -> None:
        """A literal value (no var()) is captured directly."""
        html = """\
<html><head><style>
html, body { background: #1a1a1a; color: #ffffff; }
</style></head></html>"""
        slots = extract_brand_cascade(html, {})
        bg_slots = [s for s in slots if s["slot"] == "bg"]
        assert bg_slots, "literal bg not extracted"
        assert bg_slots[-1]["value"] == "#1a1a1a"

    def test_provenance_source_label(self) -> None:
        """Each SlotValue carries a non-empty source string describing the origin rule."""
        slots = extract_brand_cascade(_SUSANN_LIKE_HTML, _VAR_MAP_SUSANN)
        for sv in slots:
            assert sv["source"], f"slot {sv['slot']} missing source"


# ---------------------------------------------------------------------------
# build_style_digest
# ---------------------------------------------------------------------------


class TestBuildStyleDigest:
    """build_style_digest: orchestrator combining root props + cascade + font links."""

    def test_empty_html_returns_empty_digest(self) -> None:
        """Empty string returns a schema-versioned digest with no resolved slots."""
        digest = build_style_digest("")
        assert digest["schema_version"]
        assert digest["resolved_slots"] == []

    def test_plain_html_returns_empty_digest(self) -> None:
        """HTML with no CSS returns an empty resolved_slots list."""
        digest = build_style_digest(_PLAIN_HTML)
        assert digest["resolved_slots"] == []

    def test_susann_like_resolves_ink_as_bg(self) -> None:
        """Susann-like HTML: bg slot resolves to #0B0B0F."""
        digest = build_style_digest(_SUSANN_LIKE_HTML)
        bg = next((s for s in digest["resolved_slots"] if s["slot"] == "bg"), None)
        assert bg is not None, "bg slot missing from digest"
        assert bg["value"] == "#0B0B0F"

    def test_susann_like_resolves_sun_as_accent(self) -> None:
        """Susann-like HTML: accent slot resolves to #FBE71F."""
        digest = build_style_digest(_SUSANN_LIKE_HTML)
        accent = next((s for s in digest["resolved_slots"] if s["slot"] == "accent"), None)
        assert accent is not None, "accent slot missing from digest"
        assert "#FBE71F" in accent["value"]

    def test_susann_like_resolves_anton_as_display_font(self) -> None:
        """Susann-like HTML: font_display slot resolves to a value containing Anton."""
        digest = build_style_digest(_SUSANN_LIKE_HTML)
        fd = next((s for s in digest["resolved_slots"] if s["slot"] == "font_display"), None)
        assert fd is not None, "font_display slot missing from digest"
        assert "Anton" in fd["value"]

    def test_susann_like_resolves_inter_as_body_font(self) -> None:
        """Susann-like HTML: font_body slot resolves to a value containing Inter."""
        digest = build_style_digest(_SUSANN_LIKE_HTML)
        fb = next((s for s in digest["resolved_slots"] if s["slot"] == "font_body"), None)
        assert fb is not None, "font_body slot missing from digest"
        assert "Inter" in fb["value"]

    def test_schema_version_present(self) -> None:
        """Digest always carries schema_version."""
        digest = build_style_digest(_SUSANN_LIKE_HTML)
        assert "schema_version" in digest
        assert digest["schema_version"]

    def test_font_link_fallback_when_no_cascade_font(self) -> None:
        """When font-family not in cascade, font_body comes from <link> tag."""
        html = """\
<html><head>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display&display=swap" rel="stylesheet">
<style>:root { --clr: #112233; } html,body { color: var(--clr); }</style>
</head><body><p>hi</p></body></html>"""
        digest = build_style_digest(html)
        fb = next((s for s in digest["resolved_slots"] if s["slot"] == "font_body"), None)
        assert fb is not None, "font_body not populated from font-link fallback"
        assert "Playfair Display" in fb["value"]

    @pytest.mark.skipif(not _FIXTURE_SUSANN.exists(), reason="Susann fixture not present")
    def test_real_susann_fixture_resolves_correctly(self) -> None:
        """Integration: real Susann source.html resolves ink/bone/sun + Anton/Inter."""
        html = _FIXTURE_SUSANN.read_text(encoding="utf-8")
        digest = build_style_digest(html)
        by_slot: dict[str, str] = {s["slot"]: s["value"] for s in digest["resolved_slots"]}
        assert "#0B0B0F" in by_slot.get("bg", ""), f"bg wrong: {by_slot.get('bg')}"
        assert "#FBE71F" in by_slot.get("accent", ""), f"accent wrong: {by_slot.get('accent')}"
        assert "Anton" in by_slot.get("font_display", ""), f"font_display wrong: {by_slot.get('font_display')}"
        assert "Inter" in by_slot.get("font_body", ""), f"font_body wrong: {by_slot.get('font_body')}"


# ---------------------------------------------------------------------------
# render_digest_block
# ---------------------------------------------------------------------------


class TestRenderDigestBlock:
    """render_digest_block: prompt text rendering."""

    def test_empty_digest_returns_empty_string(self) -> None:
        """An empty digest produces an empty string (caller omits the block)."""
        digest = StyleDigest(schema_version="test", resolved_slots=[])
        assert render_digest_block(digest) == ""

    def test_nonempty_digest_has_header(self) -> None:
        """A populated digest starts with the VERIFIED STYLE DIGEST header."""
        digest = build_style_digest(_SUSANN_LIKE_HTML)
        block = render_digest_block(digest)
        assert "VERIFIED STYLE DIGEST" in block

    def test_nonempty_digest_contains_resolved_values(self) -> None:
        """The rendered block contains the actual resolved color and font values."""
        digest = build_style_digest(_SUSANN_LIKE_HTML)
        block = render_digest_block(digest)
        assert "#0B0B0F" in block
        assert "#FBE71F" in block
        assert "Anton" in block
        assert "Inter" in block

    def test_each_slot_on_its_own_line(self) -> None:
        """Each slot entry appears on a separate line prefixed with '- '."""
        digest = build_style_digest(_SUSANN_LIKE_HTML)
        block = render_digest_block(digest)
        slot_lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
        assert len(slot_lines) == len(digest["resolved_slots"])


# ---------------------------------------------------------------------------
# D3 boundary test: build_prompt carries resolved brand values
# ---------------------------------------------------------------------------


class TestBuildPromptBoundary:
    """Prove resolved values reach build_prompt without calling the live LLM."""

    def test_build_prompt_contains_susann_resolved_values(self) -> None:
        """build_prompt for Susann-like HTML contains #0B0B0F, #FBE71F, Anton, Inter."""
        from extractor.codex_extractor import build_prompt
        from extractor.css_root_parser import parse_root_custom_properties
        from extractor.font_link_parser import parse_loaded_fonts
        from extractor.style_digest import build_style_digest

        root_props = parse_root_custom_properties(_SUSANN_LIKE_HTML)
        loaded_fonts = parse_loaded_fonts(_SUSANN_LIKE_HTML)
        style_digest = build_style_digest(_SUSANN_LIKE_HTML, root_props=root_props)
        prompt = build_prompt(
            "https://example.com",
            _SUSANN_LIKE_HTML,
            loaded_fonts=loaded_fonts,
            root_props=root_props,
            style_digest=style_digest,
        )
        assert "#0B0B0F" in prompt, "bg color not in prompt"
        assert "#FBE71F" in prompt, "accent color not in prompt"
        assert "Anton" in prompt, "display font not in prompt"
        assert "Inter" in prompt, "body font not in prompt"
