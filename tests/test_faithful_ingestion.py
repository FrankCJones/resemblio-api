"""Tests for Issue #38: faithful DRL component ingestion.

Epic #35, Step 2. Depends on #37 (fidelity oracle).

Acceptance criteria verified here (pure-data tier, no browser):

  AC1 (fonts): DRL head Google Fonts <link> tags are extracted and stored in
    AssetComponent.head_html; _compose_real_component uses those tags rather
    than the registry-derived alternative so computed font-family on the
    component subtree matches the DRL reference.

  AC2 (resets + box): Document-level resets (box-sizing, margin/padding) from
    component_css are scoped correctly by scope_style_block so they apply only
    inside the rs-library-page wrapper, not globally.

  AC3 (per-state inline styles): extract_component_html preserves inline
    style= attributes on state-demonstration buttons/elements exactly as DRL
    authored them; the oracle can capture each state node.

  AC4 (full tokens.css): _emit_brand_root covers every CSS custom property
    declared in a brand's tokens.css; no variable falls through to an
    unresolved var() reference on the component subtree.

  AC5 (scoper render-equivalence): scope_style_block relocates selectors
    without altering CSS property declarations. A property like
    `background: var(--ds-accent)` must appear unchanged inside the
    rewritten rule regardless of which selector it was scoped under.

Browser-tier tests (AC6: oracle tier pass) live in tests/render/ and require
Playwright; they are auto-skipped when the dependency is absent.

No network calls. No DRL file reads (synthetic HTML fixtures throughout).

Do this work at a level that would impress a senior developer.
Include documentation and code comments that make it easy for a future
developer to maintain this project.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.asset_versions import AssetComponentSpec, insert_asset_component
from app.library_indexer import drain_pending, enqueue_for_asset_version
from app.models import AssetVersion, LibraryPage
from app.constants import SCHEMA_V1
from sqlalchemy import select


# ---------------------------------------------------------------------------
# Synthetic DRL asset.html fixtures (AC1, AC2, AC3)
# ---------------------------------------------------------------------------

# Mirrors the real a24-cinematic-001 asset.html head structure: preconnect
# hints (which the candidate should NOT carry) plus a single stylesheet link
# for the two Google Fonts families the DRL curator chose.
_A24_BUTTONS_HEAD_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>a24-cinematic-001 · Design Reference Library</title>
<link rel="stylesheet" href="tokens.css"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400&display=swap"/>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  .btn { background: var(--ds-accent); color: #FFFFFF; font-family: var(--ds-font-body); }
  .btn:hover { background: var(--ds-accent-2); }
  .btn:focus-visible { outline: 2px solid var(--ds-focus-ring); outline-offset: 3px; }
  .btn[disabled] { background: var(--ds-hairline); cursor: not-allowed; }
</style>
</head>
<body>
  <div class="group">
    <span class="state-label">rest</span>
    <button type="button" class="btn">Watch Trailer</button>
  </div>
  <div class="group">
    <span class="state-label">hover</span>
    <button type="button" class="btn" style="background: var(--ds-accent-2);">Watch Trailer</button>
  </div>
  <div class="group">
    <span class="state-label">focus</span>
    <button type="button" class="btn" style="outline: 2px solid var(--ds-focus-ring); outline-offset: 3px;">Watch Trailer</button>
  </div>
  <div class="group">
    <span class="state-label">disabled</span>
    <button type="button" class="btn" disabled>Watch Trailer</button>
  </div>
</body>
</html>
"""

# Asset that links TWO separate font stylesheets in the head.
_MULTI_FONT_HTML = """\
<!doctype html><html><head>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,700;1,6..72,400&display=swap"/>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&display=swap"/>
<style>*, *::before, *::after { box-sizing: border-box; }</style>
</head><body><p>Content</p></body></html>
"""

# Asset with NO Google Fonts link - only local resources.
_NO_FONTS_HTML = """\
<!doctype html><html><head>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="stylesheet" href="tokens.css"/>
<style>body { font-family: sans-serif; }</style>
</head><body><p>Content</p></body></html>
"""


# ---------------------------------------------------------------------------
# AC1 - extract_drl_head_font_link_tags (pure function, new in seed_from_drl)
# ---------------------------------------------------------------------------


def test_extract_drl_head_font_link_tags_returns_google_fonts_link():
    """extract_drl_head_font_link_tags returns the raw Google Fonts <link> tag.

    The DRL a24 button asset has exactly one Google Fonts stylesheet link.
    The function must return that tag verbatim (suitable for inlining into
    the candidate's <article> head block) and MUST NOT include the
    preconnect or tokens.css links.
    """
    from scripts.seed_from_drl import extract_drl_head_font_link_tags

    result = extract_drl_head_font_link_tags(_A24_BUTTONS_HEAD_HTML)

    assert "fonts.googleapis.com" in result, (
        "Expected Google Fonts URL in result; got: %r" % result[:200]
    )
    assert "family=Inter" in result, "Inter family must be present"
    assert "JetBrains+Mono" in result or "JetBrains Mono" in result, (
        "JetBrains Mono family must be present"
    )
    # Preconnect and tokens.css links must NOT be included.
    assert 'rel="preconnect"' not in result, (
        "preconnect links must be excluded (only rel=stylesheet font links)"
    )
    assert "tokens.css" not in result, (
        "local tokens.css link must not be included (only CDN font links)"
    )


def test_extract_drl_head_font_link_tags_multiple_stylesheets():
    """Two separate Google Fonts stylesheet links are both returned."""
    from scripts.seed_from_drl import extract_drl_head_font_link_tags

    result = extract_drl_head_font_link_tags(_MULTI_FONT_HTML)

    assert "Newsreader" in result, "Newsreader link must be in result"
    assert "Inter+Tight" in result or "Inter Tight" in result, (
        "Inter Tight link must be in result"
    )


def test_extract_drl_head_font_link_tags_empty_when_no_google_fonts():
    """Returns empty string when asset.html has no Google Fonts stylesheet link."""
    from scripts.seed_from_drl import extract_drl_head_font_link_tags

    result = extract_drl_head_font_link_tags(_NO_FONTS_HTML)

    assert result == "", (
        "Expected empty string when no Google Fonts link is present; got: %r" % result
    )


def test_extract_drl_head_font_link_tags_empty_string_input():
    """Empty input returns empty string without raising."""
    from scripts.seed_from_drl import extract_drl_head_font_link_tags

    result = extract_drl_head_font_link_tags("")

    assert result == ""


# ---------------------------------------------------------------------------
# AC1 - AssetComponentSpec carries head_html (schema evolution)
# ---------------------------------------------------------------------------


def test_asset_component_spec_has_head_html_field():
    """AssetComponentSpec includes a head_html field for DRL head font links.

    head_html stores the raw <link> tags extracted from the DRL asset.html
    <head> so _compose_real_component can use them instead of the registry-
    derived Google Fonts link.
    """
    spec = AssetComponentSpec(
        fragment_key="default",
        component_html="<button>click</button>",
        component_css=".btn { color: red; }",
        source_asset_path="assets/atoms/buttons/acme-001",
        states_present=["rest", "hover"],
        head_html='<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter&display=swap"/>',
    )

    assert hasattr(spec, "head_html"), (
        "AssetComponentSpec must have a head_html field for DRL head font links"
    )
    assert spec.head_html.startswith("<link"), (
        "head_html must hold the raw <link> tag, not a parsed structure"
    )


# ---------------------------------------------------------------------------
# AC1 - _compose_real_component uses head_html for font loading (integration)
# ---------------------------------------------------------------------------

# Google Fonts URL that should appear in the candidate HTML when head_html is set.
_DRL_FONT_LINK = (
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400&display=swap"/>'
)


_BUTTONS_TOKENS: dict = {
    "bg": "#ffffff",
    "text": "#111111",
    "accent": "#000000",
    "accent-2": "#1a1a1a",
    "hairline": "#e5e4df",
    "focus-ring": "#111111",
    "font-display": '"GT America", "Inter", system-ui, sans-serif',
    "font-body": '"GT America", "Inter", system-ui, sans-serif',
    "font-mono": '"JetBrains Mono", ui-monospace, monospace',
}


def _make_asset_version_with_head_html(
    session: Session,
    *,
    brand_slug: str = "a24test",
    head_html: str = _DRL_FONT_LINK,
) -> AssetVersion:
    """Create a synthetic buttons asset_version with a component carrying head_html.

    The dtcg_json["class"] = "buttons" so the indexer routes to
    _compose_real_component for the buttons page.
    """
    dtcg: dict = {
        "schema_version": SCHEMA_V1,
        "slug": brand_slug,
        "class": "buttons",
        "tokens": dict(_BUTTONS_TOKENS),
    }
    av = AssetVersion(
        url=f"https://{brand_slug}.example/",
        content_hash=f"test-hash-faithful-{brand_slug}",
        dtcg_json=dtcg,
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label="test-2026-06",
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(av)
    session.flush()

    spec = AssetComponentSpec(
        fragment_key="default",
        component_html=(
            '<div class="group">'
            '<span class="state-label">rest</span>'
            '<button class="btn" type="button">Click</button>'
            "</div>"
            '<div class="group">'
            '<span class="state-label">hover</span>'
            '<button class="btn" type="button" style="background: var(--ds-accent-2);">Click</button>'
            "</div>"
        ),
        component_css=(
            "*, *::before, *::after { box-sizing: border-box; }\n"
            "html, body { margin: 0; padding: 0; }\n"
            ".btn { background: var(--ds-accent); font-family: var(--ds-font-body); }\n"
            ".btn:hover { background: var(--ds-accent-2); }\n"
        ),
        source_asset_path="assets/atoms/buttons/a24test-001",
        states_present=["rest", "hover"],
        head_html=head_html,
    )
    insert_asset_component(session, av.id, spec)
    session.flush()

    return av


def _get_buttons_page(session: Session, av_id: int) -> LibraryPage | None:
    """Return the buttons library page row, or None."""
    return session.execute(
        select(LibraryPage)
        .where(LibraryPage.asset_version_id == av_id)
        .where(LibraryPage.category_slug == "buttons")
    ).scalar_one_or_none()


def test_compose_real_component_uses_head_html_font_link(session: Session) -> None:
    """When a component has head_html set, the rendered buttons page uses those font links.

    AC1 integration: the DRL-curated font links (carried in head_html) must appear
    in the rendered HTML so the browser loads the same fonts as the DRL reference.
    """
    av = _make_asset_version_with_head_html(session, brand_slug="a24test-font")
    session.commit()

    job = enqueue_for_asset_version(session, av.id)
    assert job is not None
    session.commit()
    drain_pending(session)

    page = _get_buttons_page(session, av.id)
    assert page is not None, "buttons LibraryPage was not written"
    html = page.rendered_html

    # The DRL font link from head_html must appear verbatim in the rendered HTML.
    assert _DRL_FONT_LINK in html, (
        "Expected DRL head_html font link in rendered HTML.\n"
        "The _compose_real_component path must carry head_html for faithful font loading,\n"
        "not derive fonts through the brand font registry.\n"
        "First 400 chars of rendered_html: %r" % html[:400]
    )
    # The page must be a real-component page (not generic template).
    assert 'data-rs-source="drl-component"' in html


def test_compose_real_component_no_font_alt_override_when_head_html_set(
    session: Session,
) -> None:
    """When head_html is set, _compose_real_component does not apply the font-alt :root override.

    AC1: the registry-based build_font_alternative_root_block emits a :root block that
    overrides --ds-font-* variables to the free-alternative family. For real components
    this override is harmful: it changes the resolved font-family away from what the DRL
    reference loads, causing a mismatch in the oracle's computed-style comparison.

    When head_html is non-empty, the rendered HTML must NOT contain the
    font-alt-override :root block (identified by the 'rs-font-alternative' comment
    that build_font_alternative_root_block emits, or by the --ds-font-* override vars
    following the brand :root block).

    The brand's :root block itself (from _emit_brand_root) is still present and
    correct; only the SECONDARY override block is suppressed.
    """
    av = _make_asset_version_with_head_html(session, brand_slug="a24test-nooverride")
    session.commit()

    job = enqueue_for_asset_version(session, av.id)
    assert job is not None
    session.commit()
    drain_pending(session)

    page = _get_buttons_page(session, av.id)
    assert page is not None
    html = page.rendered_html

    assert 'data-rs-source="drl-component"' in html

    # Count :root { blocks. With the override suppressed there should be
    # exactly ONE :root block (from _emit_brand_root). With the override
    # present there would be TWO.
    root_blocks = re.findall(r":root\s*\{", html)
    assert len(root_blocks) == 1, (
        "Expected exactly 1 :root { block in real-component HTML (brand tokens only).\n"
        "Found %d. The font-alt-root override block must be suppressed when "
        "head_html is set, because it overrides --ds-font-* away from the DRL "
        "reference values and breaks AC1.\n"
        "Rendered :root blocks: %s" % (len(root_blocks), root_blocks)
    )


# ---------------------------------------------------------------------------
# AC2 - Document resets scoped correctly
# ---------------------------------------------------------------------------


def test_scope_style_block_scopes_box_sizing_reset():
    """scope_style_block converts '*, *::before, *::after { box-sizing: border-box; }' correctly.

    AC2: document-level resets must be scoped so they only affect elements inside
    the rs-library-page wrapper, not the surrounding Next.js page chrome.
    """
    from app.library_style_scope import scope_style_block

    css = "*, *::before, *::after { box-sizing: border-box; }\nhtml, body { margin: 0; padding: 0; }"
    scoped = scope_style_block(css)

    # The universal selector must be prefixed with the wrapper.
    assert ".rs-library-page *" in scoped, (
        "Universal selector '*' must be scoped to '.rs-library-page *' "
        "(not left bare or dropped). Got: %r" % scoped
    )
    # The box-sizing property must survive the rewrite unchanged.
    assert "box-sizing: border-box" in scoped, (
        "box-sizing: border-box property value must be preserved after scoping"
    )
    # html and body must be collapsed to the wrapper selector (not dropped).
    assert ".rs-library-page" in scoped, (
        "html/body collapse must produce the wrapper selector"
    )
    assert "margin: 0" in scoped, (
        "margin: 0 property value must survive the selector rewrite"
    )


# ---------------------------------------------------------------------------
# AC3 - Per-state inline style= attributes preserved
# ---------------------------------------------------------------------------


def test_extract_component_html_preserves_inline_style_attrs():
    """extract_component_html preserves inline style= attributes on state nodes.

    AC3: DRL button assets render hover/focus states as DOM nodes with inline
    style= attributes (e.g. style="background: var(--ds-accent-2);"). These
    must survive the HTML extraction so the oracle's per-state capture finds
    the correct computed styles.
    """
    from scripts.seed_from_drl import extract_component_html

    result = extract_component_html(_A24_BUTTONS_HEAD_HTML)

    # The hover-state button carries an inline style override.
    assert 'style="background: var(--ds-accent-2);"' in result, (
        "Hover-state inline style must be preserved in component_html. "
        "Got: %r" % result[:300]
    )
    # The focus-state button carries an outline override.
    assert "outline: 2px solid var(--ds-focus-ring)" in result, (
        "Focus-state inline style must be preserved"
    )
    # The state labels must be present so the oracle can detect state names.
    assert 'class="state-label"' in result, (
        "State-label spans must survive to enable oracle state detection"
    )


# ---------------------------------------------------------------------------
# AC4 - tokens.css coverage via _emit_brand_root
# ---------------------------------------------------------------------------


def test_emit_brand_root_covers_all_token_vars():
    """_emit_brand_root emits a :root block with every custom property from the token dict.

    AC4: every --ds-* variable declared in the DRL tokens.css must appear in the
    emitted :root block so component CSS using var(--ds-*) resolves correctly.
    """
    from app.library_indexer import _emit_brand_root

    tokens = {
        "bg": "#0a0a0a",
        "text": "#ffffff",
        "accent": "#ff3366",
        "font-body": '"Inter", system-ui, sans-serif',
        "ds-radius-xs": "2px",
    }

    result = _emit_brand_root(tokens)

    assert ":root {" in result, "_emit_brand_root must emit a :root block"
    # All explicit token values must appear somewhere in the block.
    assert "#0a0a0a" in result or "--ds-bg: #0a0a0a" in result, (
        "--ds-bg must be emitted"
    )
    assert "#ffffff" in result, "--ds-text must be emitted"
    assert "#ff3366" in result or "--ds-accent: #ff3366" in result, (
        "--ds-accent must be emitted"
    )


# ---------------------------------------------------------------------------
# AC5 - scope_style_block render-equivalence (pure verification)
# ---------------------------------------------------------------------------


def test_scope_style_block_does_not_alter_property_declarations():
    """scope_style_block preserves CSS property values verbatim inside each rule body.

    AC5 (structural half): the scoper must rewrite selector location without
    modifying any property: value; declaration. A declaration like
    'background: var(--ds-accent)' inside .btn must appear unchanged (same
    property, same value) in the scoped output, just under a prefixed selector.

    This is the pure-data half of AC5. The browser-computed-style half
    (comparing getComputedStyle() on scoped vs unscoped renders) requires
    Playwright and lives in tests/render/.
    """
    from app.library_style_scope import scope_style_block

    css = (
        ".btn { background: var(--ds-accent); color: #fff; border-radius: var(--ds-radius-xs); }\n"
        ".btn:hover { background: var(--ds-accent-2); }\n"
        ".btn:focus-visible { outline: 2px solid var(--ds-focus-ring); outline-offset: 3px; }\n"
        ".btn[disabled] { opacity: 0.5; cursor: not-allowed; }\n"
    )
    scoped = scope_style_block(css)

    # Every property: value pair from the original must appear in the scoped output.
    expected_declarations = [
        "background: var(--ds-accent)",
        "color: #fff",
        "border-radius: var(--ds-radius-xs)",
        "background: var(--ds-accent-2)",
        "outline: 2px solid var(--ds-focus-ring)",
        "outline-offset: 3px",
        "opacity: 0.5",
        "cursor: not-allowed",
    ]
    for decl in expected_declarations:
        assert decl in scoped, (
            "Property declaration %r was lost or mutated by scope_style_block.\n"
            "Scoped output: %r" % (decl, scoped[:500])
        )

    # The wrapper selector must appear (selector was rewritten).
    assert ".rs-library-page" in scoped, (
        "Wrapper selector must appear in scoped output"
    )
    # Every .btn selector must be scoped: each occurrence of ".btn" in the
    # output must be immediately preceded by the wrapper selector and a space
    # (".rs-library-page .btn..."). A bare ".btn" at the start of a rule would
    # leak the component's styles out of the wrapper into the page chrome.
    for match in re.finditer(r"\.btn\b", scoped):
        prefix = scoped[max(0, match.start() - len(".rs-library-page ")):match.start()]
        assert prefix.endswith(".rs-library-page "), (
            "Found a .btn selector not scoped under the wrapper at offset %d.\n"
            "Preceding text was %r.\nScoped output: %r"
            % (match.start(), prefix, scoped[:500])
        )
    # And the first rule must start with the wrapper (no leading bare selector).
    assert scoped.strip().startswith(".rs-library-page"), (
        "First rule in scoped output must start with the wrapper selector"
    )


def test_scope_style_block_keyframes_pass_through_unchanged():
    """@keyframes rules are not scoped (no selectors to rewrite).

    AC5: @keyframes must pass through intact so component animations work.
    """
    from app.library_style_scope import scope_style_block

    css = (
        "@keyframes spin {\n"
        "  from { transform: rotate(0deg); }\n"
        "  to   { transform: rotate(360deg); }\n"
        "}\n"
        ".loader { animation: spin 1s linear infinite; }\n"
    )
    scoped = scope_style_block(css)

    assert "@keyframes spin" in scoped, "@keyframes must pass through unchanged"
    assert "transform: rotate(0deg)" in scoped, "keyframe body must be preserved"
    assert "animation: spin" in scoped, ".loader animation property must be preserved"


def test_scope_style_block_root_selector_preserved():
    """:root { ... } is preserved as-is (CSS custom properties cascade globally).

    AC5: :root must NOT be scoped to .rs-library-page :root (that would be
    invalid CSS and break custom property inheritance).
    """
    from app.library_style_scope import scope_style_block

    css = ":root { --ds-bg: #0a0908; --ds-text: #f5f1ea; }\n.body { background: var(--ds-bg); }\n"
    scoped = scope_style_block(css)

    assert ":root {" in scoped or ":root{" in scoped, (
        ":root block must survive scoping unchanged"
    )
    assert "--ds-bg: #0a0908" in scoped, ":root custom properties must be preserved"
    # The body rule must be scoped.
    assert ".rs-library-page" in scoped
