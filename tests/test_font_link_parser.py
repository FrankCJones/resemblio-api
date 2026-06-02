"""Unit tests for extractor.font_link_parser.

The parser is the deterministic pre-LLM signal that closes the "missed
<head> font-link declarations" diagnostic from R3.1 Phase A. These tests
exercise the pure-data shape: HTML in, structured LoadedFonts out, no
network.

Source mission: projects/OptSus Team/missions/resemblio-r3.1-extractor-surgery-v1.md
"""
from __future__ import annotations

import pytest

from extractor.font_link_parser import (
    SCHEMA_VERSION,
    parse_loaded_fonts,
    render_for_prompt,
)


def test_empty_input_returns_empty_well_formed_result() -> None:
    """Empty or non-string inputs produce a well-formed empty report."""
    for value in ("", None, "<html></html>"):
        result = parse_loaded_fonts(value)  # type: ignore[arg-type]
        assert result["families"] == []
        assert result["entries"] == []
        assert result["schema_version"] == SCHEMA_VERSION


def test_google_fonts_css2_single_family() -> None:
    """Single-family Google Fonts CSS2 URL yields one entry."""
    html = """
    <html><head>
    <link href="https://fonts.googleapis.com/css2?family=Anton&display=swap" rel="stylesheet">
    </head></html>
    """
    result = parse_loaded_fonts(html)
    assert result["families"] == ["Anton"]
    assert result["entries"][0]["source"] == "google"


def test_google_fonts_css2_multi_family_with_weights() -> None:
    """Multiple `family=` params with weight specs all extract cleanly."""
    html = """
    <head>
    <link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;700&display=swap" rel="stylesheet">
    </head>
    """
    result = parse_loaded_fonts(html)
    assert result["families"] == ["Anton", "Inter"]


def test_google_fonts_plus_decoded_to_space() -> None:
    """Google Fonts encodes spaces as "+"; we restore them."""
    html = """
    <head>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Open+Sans" rel="stylesheet">
    </head>
    """
    result = parse_loaded_fonts(html)
    assert result["families"] == ["Playfair Display", "Open Sans"]


def test_css1_pipe_separated_families() -> None:
    """Legacy CSS1 pipe-separated families all extract."""
    html = """
    <head>
    <link href="https://fonts.googleapis.com/css?family=Roboto|Lato|Open+Sans" rel="stylesheet">
    </head>
    """
    result = parse_loaded_fonts(html)
    assert result["families"] == ["Roboto", "Lato", "Open Sans"]


def test_bunny_fonts_treated_as_google_compatible() -> None:
    """fonts.bunny.net uses the same query syntax; we accept it."""
    html = """
    <head>
    <link href="https://fonts.bunny.net/css?family=manrope:400,600" rel="stylesheet">
    </head>
    """
    result = parse_loaded_fonts(html)
    assert result["families"] == ["manrope"]
    assert result["entries"][0]["source"] == "bunny"


def test_fontshare_query_parsed() -> None:
    """Fontshare's f= param is parsed; weight suffix stripped."""
    html = """
    <head>
    <link href="https://api.fontshare.com/v2/css?f=Satoshi@400,700&f=Clash+Display" rel="stylesheet">
    </head>
    """
    result = parse_loaded_fonts(html)
    assert "Satoshi" in result["families"]
    assert "Clash Display" in result["families"]


def test_typekit_kit_id_recorded_without_inventing_family() -> None:
    """Typekit URLs do not expose families; we mark the kit, not a name."""
    html = """
    <head>
    <link href="https://use.typekit.net/abc1234.css" rel="stylesheet">
    </head>
    """
    result = parse_loaded_fonts(html)
    assert result["families"] == ["typekit:abc1234"]
    assert result["entries"][0]["source"] == "typekit"


def test_preconnect_and_preload_links_are_ignored() -> None:
    """Only rel=stylesheet links contribute; preconnect/preload do not."""
    html = """
    <head>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preload" as="style" href="https://fonts.googleapis.com/css2?family=Roboto">
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">
    </head>
    """
    result = parse_loaded_fonts(html)
    assert result["families"] == ["Inter"]


def test_at_font_face_in_style_block_detected() -> None:
    """Inline @font-face declarations are caught."""
    html = """
    <head>
    <style>
      @font-face { font-family: "Custom Sans"; src: url(/c.woff2); }
      body { font-family: "Custom Sans", sans-serif; }
    </style>
    </head>
    """
    result = parse_loaded_fonts(html)
    assert "Custom Sans" in result["families"]
    assert any(e["source"] == "font-face" for e in result["entries"])


def test_susann_pathology_fixture_recovers_anton_and_inter() -> None:
    """The exact Susann <head> recovers both Anton and Inter."""
    html = """
    <html lang="en"><head>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;700&display=swap" rel="stylesheet">
    <style>:root { --type-display: "Anton", system-ui; }</style>
    </head><body></body></html>
    """
    result = parse_loaded_fonts(html)
    assert "Anton" in result["families"]
    assert "Inter" in result["families"]


def test_deduplication_preserves_first_seen_order() -> None:
    """Same family from two sources is recorded once, in first-seen order."""
    html = """
    <head>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto">
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@700">
    </head>
    """
    result = parse_loaded_fonts(html)
    assert result["families"] == ["Inter", "Roboto"]


def test_link_without_href_is_skipped() -> None:
    """Malformed <link rel=stylesheet> with no href is ignored, not raised."""
    html = '<head><link rel="stylesheet"></head>'
    result = parse_loaded_fonts(html)
    assert result["families"] == []


def test_falls_back_to_full_document_when_no_head_tag() -> None:
    """Single-page HTML with no <head> still gets scanned."""
    html = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Lato">'
    result = parse_loaded_fonts(html)
    assert result["families"] == ["Lato"]


def test_render_for_prompt_empty_returns_empty_string() -> None:
    """No fonts means no Markdown block; caller omits the section."""
    result = parse_loaded_fonts("<html></html>")
    assert render_for_prompt(result) == ""


def test_render_for_prompt_includes_families_and_sources() -> None:
    """Rendered Markdown lists each detection with its source."""
    html = '<head><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Anton"></head>'
    result = parse_loaded_fonts(html)
    rendered = render_for_prompt(result)
    assert "Anton" in rendered
    assert "google" in rendered


@pytest.mark.parametrize("href", [
    "https://fonts.googleapis.com/css2",  # no family= param
    "https://example.com/style.css",       # unrelated CDN
    "data:text/css,body{}",                 # data URL
])
def test_non_font_or_familyless_links_return_empty(href: str) -> None:
    """Unrelated or familyless stylesheet URLs contribute nothing."""
    html = f'<head><link rel="stylesheet" href="{href}"></head>'
    result = parse_loaded_fonts(html)
    assert result["families"] == []
