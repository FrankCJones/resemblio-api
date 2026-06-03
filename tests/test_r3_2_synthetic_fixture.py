"""R3.2 synthetic-fixture end-to-end test.

Exercises the full pre-LLM signal chain on a synthetic Susann-shaped
HTML page that combines the three diagnostic conditions from the R3.2
dispatch:

1. Brand tokens declared via `:root { --ink: ...; --sun: ...; }`
2. Anton + Inter loaded via `<link rel="stylesheet">` to fonts.googleapis.com
3. A first-encountered `<button>` that is a generic nav-icon shell

The test asserts that the deterministic parsers recover the brand identity
(the LLM call itself is not exercised here; the LLM is wired to consume
these signals via `build_prompt` in production).

It also asserts the S20 rubric flags a Susann-shaped near-default extraction
output with `quality_score == 0` and the `near_default_extraction` penalty
flag.

Source dispatch: `projects/Resemblio/_handoff/inbox/claude/
2026-06-02-susann-extraction-fidelity-investigation.md`.
"""
from __future__ import annotations

from app.constants import NEAR_DEFAULT_EXTRACTION_FLAG
from app.quality_heuristics import apply_heuristic_penalties
from app.quality_scoring import compute_quality_score
from extractor.codex_extractor import build_prompt
from extractor.computed_styles import empty_report as empty_computed_report
from extractor.css_root_parser import parse_root_custom_properties
from extractor.font_link_parser import parse_loaded_fonts


# Synthetic HTML modeled on Susann's approved Headlights v2 concept.
# Combines the three R3.2 diagnostic conditions in one fixture.
SYNTHETIC_SUSANN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Synthetic Headlights fixture</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: dark;
      --ink: #0B0B0F;
      --ink-2: #14141A;
      --bone: #F5F2EA;
      --sun: #FBE71F;
      --warm-brown: #7A4A2E;
      --type-display: "Anton", "Bebas Neue", "Impact", system-ui, sans-serif;
      --type-body: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    html, body {
      background: var(--ink);
      color: var(--bone);
      font-family: var(--type-body);
    }
    a { color: var(--sun); }
    h1 { font-family: var(--type-display); }
  </style>
</head>
<body>
  <nav>
    <button aria-label="open menu" class="nav-icon">
      <span class="bar"></span>
      <span class="bar"></span>
    </button>
  </nav>
  <h1>Headlights</h1>
  <p>A writing life.</p>
  <a href="/work">Read more</a>
</body>
</html>
"""


def test_root_parser_captures_brand_tokens_from_synthetic_fixture() -> None:
    """The :root parser surfaces ink/bone/sun + type-body/type-display."""
    root_props = parse_root_custom_properties(SYNTHETIC_SUSANN_HTML)
    by_name = root_props["properties_by_name"]
    assert by_name["ink"] == "#0B0B0F"
    assert by_name["bone"] == "#F5F2EA"
    assert by_name["sun"] == "#FBE71F"
    assert "Anton" in by_name["type-display"]
    assert "Inter" in by_name["type-body"]


def test_font_link_parser_recovers_anton_and_inter_from_synthetic_fixture() -> None:
    """The <link> font parser extracts Anton + Inter from the Google Fonts URL."""
    loaded = parse_loaded_fonts(SYNTHETIC_SUSANN_HTML)
    assert "Anton" in loaded["families"]
    assert "Inter" in loaded["families"]


def test_build_prompt_includes_root_signal_block_first() -> None:
    """`build_prompt` renders the :root signal block FIRST in the signals chunk.

    Priority order matters: brand-declared `:root` properties (intent)
    outrank computed-style samples (artifact) outrank `<link>` font families.
    """
    root_props = parse_root_custom_properties(SYNTHETIC_SUSANN_HTML)
    loaded_fonts = parse_loaded_fonts(SYNTHETIC_SUSANN_HTML)
    computed = empty_computed_report("skipped", "test")
    prompt = build_prompt(
        url="https://example.invalid/",
        html=SYNTHETIC_SUSANN_HTML,
        loaded_fonts=loaded_fonts,
        computed_styles=computed,
        root_props=root_props,
    )
    # All three signals present:
    assert "Declared CSS custom properties" in prompt
    assert "--ink: #0B0B0F" in prompt
    assert "Anton" in prompt
    assert "Inter" in prompt
    # Root block ordered first within the signals section:
    root_index = prompt.index("Declared CSS custom properties")
    fonts_index = prompt.index("Detected web fonts")
    assert root_index < fonts_index


def test_s20_flags_synthetic_susann_shaped_extraction_output() -> None:
    """A Susann-shaped extraction OUTPUT (the wrong tokens) trips the R3.2 rule.

    Independent of whether the LLM call succeeds, the rubric must label
    Susann-shaped output with `quality_score == 0` and the
    `near_default_extraction` flag so the refund path triggers.
    """
    susann_shaped_output = {
        "bg": "#f5f5f5",
        "text": "#1a1a1a",
        "accent": "#4f46e5",
        "surface": "#ffffff",
        "font_body": "system-ui, -apple-system, sans-serif",
        "font_display": "Georgia, serif",
        "font_mono": "'Courier New', monospace",
        "text_base": "1rem",
        "text_lg": "1.125rem",
        "text_xl": "1.25rem",
        "text_2xl": "1.5rem",
        "space_2": "0.5rem",
        "space_4": "1rem",
        "space_6": "1.5rem",
    }
    base = compute_quality_score(susann_shaped_output)
    out = apply_heuristic_penalties(susann_shaped_output, base)
    assert NEAR_DEFAULT_EXTRACTION_FLAG in out.penalties_applied
    assert out.penalized_score == 0.0
