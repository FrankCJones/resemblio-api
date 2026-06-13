"""HTML templates for every asset class.

One template per class. Each template is a Python format-string with
named placeholders that compose.py fills from an ExtractionRecord +
TokenSet. Templates carry the TOKEN_CONTRACT discipline; every visual
decision is sourced from a contract variable, never inline.

## Why inline templates (not .tmpl files)

Single source of truth. Editing a template doesn't require finding the
matching .tmpl file. The Python format-string mini-language is enough
for our needs (no Jinja loops or conditionals beyond what we can express
in compose.py).

## The contract

Each `*_TEMPLATE` is a Python string with `{placeholder}` markers. The
matching `*_PLACEHOLDERS` tuple lists every placeholder name; tests
verify the two stay in sync.

## Adding a new class template

1. Add `<CLASS>_BODY = "..."` and `<CLASS>_STYLES = "..."` and `<CLASS>_PLACEHOLDERS = (...)`.
2. Add the class to `TEMPLATES_BY_CLASS` mapping at the bottom.
3. Add a fixture in `test_templates.py` that round-trips through it.
4. Add a one-line entry to `_templates/README.md` if it exists.
"""
from __future__ import annotations

from typing import TypedDict

# ----------------------------------------------------------------------
# Shared HTML skeleton — every template extends this.
# ----------------------------------------------------------------------

HTML_SKELETON = """\
<!--
  Asset: {slug}
  Class: {class_folder}
  Pattern: {pattern_tags}
  Inspired by: {inspired_by_summary}
  Notes:
{notes_block}
-->
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{slug} · Design Reference Library</title>
<link rel="stylesheet" href="tokens.css"/>
<style>
{styles}
</style>
</head>
<body>
{body}
</body>
</html>
"""

# ----------------------------------------------------------------------
# Tokens.css template — emits the full --ds-* contract from a TokenSet.
# ----------------------------------------------------------------------

TOKENS_CSS_TEMPLATE = """\
/*
 * {slug} tokens
 *
 * Native --ds-* contract values for {system_name}.
 * Captured: {captured} via {extraction_method} at score {provenance_score}.
 * Source: {primary_url}
 *
 * Per-site libraries override these by writing their own tokens.css
 * against the same slot names. See TOKEN_CONTRACT.md.
 */
:root {{
{tokens_body}
}}
"""

# ----------------------------------------------------------------------
# Per-class HTML body templates.
# Each yields the {body} insert for HTML_SKELETON.
# ----------------------------------------------------------------------

ALPHABET_BODY = """\
<main class="a-page">
  <header class="a-header">
    <span class="a-kicker">{kicker}</span>
    <h1 class="a-display-1">{display_headline}</h1>
    <p class="a-dek">{dek}</p>
  </header>

  <section class="a-row"><span class="a-slot">Display 1</span><h2 class="a-display-1">{display_sample}</h2></section>
  <section class="a-row"><span class="a-slot">Display 2</span><h2 class="a-display-2">{display_sample_2}</h2></section>
  <section class="a-row"><span class="a-slot">H2</span><h3 class="a-h2">{h2_sample}</h3></section>
  <section class="a-row"><span class="a-slot">H3</span><h4 class="a-h3">{h3_sample}</h4></section>
  <section class="a-row"><span class="a-slot">Lead</span><p class="a-lead">{lead_sample}</p></section>
  <section class="a-row"><span class="a-slot">Body</span><p class="a-body">{body_sample}</p></section>
  <section class="a-row"><span class="a-slot">Dek</span><p class="a-dek">{dek_sample}</p></section>
  <section class="a-row"><span class="a-slot">Small</span><p class="a-small">{small_sample}</p></section>
  <section class="a-row"><span class="a-slot">Footnote</span><p class="a-footnote">{footnote_sample}</p></section>
  <section class="a-row"><span class="a-slot">Kicker</span><span class="a-kicker">{kicker_sample}</span></section>
  <section class="a-row"><span class="a-slot">Nav link</span><a class="a-nav" href="#">{nav_link_sample}</a></section>
  <section class="a-row"><span class="a-slot">Button</span><button class="a-btn">{button_sample}</button></section>
  <section class="a-row"><span class="a-slot">Mono</span><span class="a-mono">{mono_sample}</span></section>
  <section class="a-row"><span class="a-slot">Wordmark</span><span class="a-wordmark">{wordmark_sample}</span></section>
</main>
"""

ALPHABET_STYLES = """\
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--ds-bg);
  color: var(--ds-text);
  font-family: var(--ds-font-body);
  font-size: var(--ds-text-base);
  line-height: var(--ds-leading-normal, 1.55);
  -webkit-font-smoothing: antialiased;
}
.a-page { max-width: var(--ds-page-max-default, 880px); margin: 0 auto; padding: var(--ds-page-pad-y, 96px) var(--ds-page-pad-x, 32px) 160px; }
.a-header { margin-bottom: 56px; }
.a-row { display: grid; grid-template-columns: 200px 1fr; gap: 32px;
         padding: 28px 0; border-top: var(--ds-section-divider-width, 1px) solid var(--ds-hairline); }
.a-row:first-of-type { border-top: 0; }
.a-slot { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
          letter-spacing: var(--ds-tracking-wide, 0.06em); text-transform: uppercase;
          color: var(--ds-text-muted); padding-top: 6px; }
.a-display-1 { font-family: var(--ds-font-display); font-size: var(--ds-text-6xl, 88px);
               line-height: var(--ds-leading-tight, 1.05); letter-spacing: var(--ds-tracking-tight, -0.02em);
               font-weight: var(--ds-font-weight-display, 600); margin: 0; }
.a-display-2 { font-family: var(--ds-font-display); font-size: var(--ds-text-5xl, 72px);
               line-height: 1.05; letter-spacing: var(--ds-tracking-snug, -0.018em);
               font-weight: var(--ds-font-weight-display, 600); margin: 0; }
.a-h2 { font-family: var(--ds-font-display); font-size: var(--ds-text-3xl);
        line-height: 1.15; letter-spacing: -0.012em; font-weight: var(--ds-font-weight-display, 600);
        color: var(--ds-text); margin: 0; }
.a-h3 { font-family: var(--ds-font-display); font-size: var(--ds-text-2xl);
        line-height: 1.2; font-weight: var(--ds-font-weight-display, 600);
        color: var(--ds-text); margin: 0; }
.a-lead { font-family: var(--ds-font-body); font-size: var(--ds-text-xl);
          line-height: 1.45; margin: 0; }
.a-body { font-family: var(--ds-font-body); font-size: var(--ds-text-base);
          line-height: var(--ds-leading-normal, 1.55); margin: 0; max-width: 56ch; }
.a-dek { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
         line-height: 1.5; color: var(--ds-text-muted); margin: 0; max-width: 52ch; }
.a-small { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
           line-height: 1.5; color: var(--ds-text-muted); margin: 0; }
.a-footnote { font-family: var(--ds-font-body); font-size: var(--ds-text-xs);
              line-height: 1.45; color: var(--ds-text-muted); margin: 0; }
.a-kicker { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
            font-weight: var(--ds-font-weight-medium, 500); color: var(--ds-text-muted); letter-spacing: 0.01em; }
.a-nav { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
         font-weight: var(--ds-font-weight-medium, 500); color: var(--ds-text); text-decoration: none; }
.a-btn { display: inline-flex; align-items: center; gap: 8px;
         font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
         font-weight: var(--ds-font-weight-medium, 500); padding: 8px 14px;
         border-radius: var(--ds-radius-sm, 4px); border: 0;
         background: var(--ds-accent); color: var(--ds-bg); cursor: pointer; }
.a-mono { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
          color: var(--ds-text-muted); }
.a-wordmark { font-family: var(--ds-font-display); font-size: var(--ds-text-2xl);
              font-weight: var(--ds-font-weight-display, 600); letter-spacing: -0.01em; }
"""

# ----------------------------------------------------------------------
# Whole templates. Each is a self-contained section with its own
# stylesheet block. Section sequence for layouts is built by concatenating
# whole-body templates with the layout's section_sequence ordering.
# ----------------------------------------------------------------------

HERO_BODY = """\
<section class="h-hero">
  <div class="h-hero__inner">
    <span class="h-hero__kicker">{kicker}</span>
    <h1 class="h-hero__title">{headline}</h1>
    <p class="h-hero__dek">{dek}</p>
    <div class="h-hero__actions">
      <a class="h-btn h-btn--primary" href="#">{cta_primary}</a>
      <a class="h-btn h-btn--ghost" href="#">{cta_secondary}</a>
    </div>
  </div>
</section>
"""

HERO_STYLES = """\
.h-hero { padding: var(--ds-section-padding-y, 96px) var(--ds-section-padding-x, 32px); background: var(--ds-bg); color: var(--ds-text); }
.h-hero__inner { max-width: var(--ds-page-max-wide, 1100px); margin: 0 auto; }
.h-hero__kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
                  text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
                  color: var(--ds-text-muted); }
.h-hero__title { font-family: var(--ds-font-display); font-size: var(--ds-text-5xl);
                 line-height: var(--ds-leading-tight, 1.05); letter-spacing: var(--ds-tracking-tight, -0.02em);
                 font-weight: var(--ds-font-weight-display, 600); margin: 24px 0 16px; max-width: 18ch; }
.h-hero__dek { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
               line-height: 1.45; color: var(--ds-text-muted);
               margin: 0 0 32px; max-width: 56ch; }
.h-hero__actions { display: flex; gap: 12px; }
.h-btn { display: inline-flex; align-items: center; padding: var(--ds-button-padding-y, 12px) var(--ds-button-padding-x, 20px);
         font-family: var(--ds-button-font-family, var(--ds-font-body)); font-size: var(--ds-button-font-size, var(--ds-text-sm));
         font-weight: var(--ds-button-font-weight, 500); border-radius: var(--ds-button-radius, var(--ds-radius-sm, 6px));
         text-decoration: none; }
.h-btn--primary { background: var(--ds-accent); color: var(--ds-bg); }
.h-btn--ghost { background: transparent; color: var(--ds-text);
                border: var(--ds-button-border-width, 1px) solid var(--ds-border); }
"""

NAV_BODY = """\
<header class="n-nav">
  <div class="n-nav__inner">
    <a class="n-nav__brand" href="#">{wordmark}</a>
    <nav class="n-nav__links">
      <a href="#">{link_1}</a>
      <a href="#">{link_2}</a>
      <a href="#">{link_3}</a>
      <a href="#">{link_4}</a>
    </nav>
    <div class="n-nav__actions">
      <a class="n-nav__signin" href="#">{signin}</a>
      <a class="n-btn n-btn--primary" href="#">{signup}</a>
    </div>
  </div>
</header>
"""

NAV_STYLES = """\
.n-nav { background: var(--ds-bg); border-bottom: 1px solid var(--ds-hairline); }
.n-nav__inner { max-width: var(--ds-page-max-full, 1200px); margin: 0 auto; padding: 16px var(--ds-page-pad-x, 32px);
                display: flex; align-items: center; gap: 32px; }
.n-nav__brand { font-family: var(--ds-font-display); font-weight: var(--ds-font-weight-display, 600);
                font-size: var(--ds-text-lg); color: var(--ds-text);
                text-decoration: none; }
.n-nav__links { display: flex; gap: 24px; flex: 1; }
.n-nav__links a { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
                  font-weight: var(--ds-font-weight-medium, 500); color: var(--ds-text); text-decoration: none; }
.n-nav__actions { display: flex; align-items: center; gap: 16px; }
.n-nav__signin { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
                 font-weight: var(--ds-font-weight-medium, 500); color: var(--ds-text); text-decoration: none; }
.n-btn { display: inline-flex; padding: 8px 14px; border-radius: var(--ds-button-radius, var(--ds-radius-sm, 6px));
         font-family: var(--ds-button-font-family, var(--ds-font-body)); font-size: var(--ds-button-font-size, var(--ds-text-sm));
         font-weight: var(--ds-button-font-weight, 500); text-decoration: none; }
.n-btn--primary { background: var(--ds-accent); color: var(--ds-bg); }
"""

FOOTER_BODY = """\
<footer class="f-footer">
  <div class="f-footer__inner">
    <div class="f-footer__brand">
      <span class="f-footer__wordmark">{wordmark}</span>
      <p class="f-footer__tagline">{tagline}</p>
    </div>
    <div class="f-footer__cols">
      <div class="f-footer__col">
        <h4>{col_1_title}</h4>
        <a href="#">{col_1_link_1}</a><a href="#">{col_1_link_2}</a><a href="#">{col_1_link_3}</a>
      </div>
      <div class="f-footer__col">
        <h4>{col_2_title}</h4>
        <a href="#">{col_2_link_1}</a><a href="#">{col_2_link_2}</a><a href="#">{col_2_link_3}</a>
      </div>
      <div class="f-footer__col">
        <h4>{col_3_title}</h4>
        <a href="#">{col_3_link_1}</a><a href="#">{col_3_link_2}</a><a href="#">{col_3_link_3}</a>
      </div>
    </div>
  </div>
  <div class="f-footer__legal">
    <span>{copyright_line}</span>
  </div>
</footer>
"""

FOOTER_STYLES = """\
.f-footer { background: var(--ds-bg); color: var(--ds-text);
            border-top: 1px solid var(--ds-hairline); padding: 64px var(--ds-page-pad-x, 32px) 24px; }
.f-footer__inner { max-width: var(--ds-page-max-full, 1200px); margin: 0 auto;
                   display: grid; grid-template-columns: 1fr 2fr; gap: 48px; }
.f-footer__wordmark { font-family: var(--ds-font-display); font-weight: var(--ds-font-weight-display, 600);
                      font-size: var(--ds-text-lg); }
.f-footer__tagline { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
                     color: var(--ds-text-muted); margin: 8px 0 0; max-width: 32ch; }
.f-footer__cols { display: grid; grid-template-columns: repeat(3, 1fr); gap: 32px; }
.f-footer__col h4 { font-family: var(--ds-font-body); font-size: var(--ds-text-xs);
                    text-transform: uppercase; letter-spacing: var(--ds-tracking-wide, 0.06em);
                    color: var(--ds-text-muted); margin: 0 0 12px; font-weight: var(--ds-font-weight-medium, 500); }
.f-footer__col a { display: block; font-family: var(--ds-font-body);
                   font-size: var(--ds-text-sm); color: var(--ds-text);
                   text-decoration: none; padding: 4px 0; }
.f-footer__legal { max-width: var(--ds-page-max-full, 1200px); margin: 48px auto 0;
                   padding-top: 24px; border-top: 1px solid var(--ds-hairline);
                   font-family: var(--ds-font-body); font-size: var(--ds-text-xs);
                   color: var(--ds-text-muted); }
"""

FEATURE_GRID_BODY = """\
<section class="fg-grid">
  <div class="fg-grid__inner">
    <header class="fg-grid__head">
      <span class="fg-grid__kicker">{kicker}</span>
      <h2 class="fg-grid__title">{title}</h2>
      <p class="fg-grid__dek">{dek}</p>
    </header>
    <div class="fg-grid__tiles">
      <article class="fg-tile"><h3>{tile_1_title}</h3><p>{tile_1_dek}</p><a href="#">{tile_1_link}</a></article>
      <article class="fg-tile"><h3>{tile_2_title}</h3><p>{tile_2_dek}</p><a href="#">{tile_2_link}</a></article>
      <article class="fg-tile"><h3>{tile_3_title}</h3><p>{tile_3_dek}</p><a href="#">{tile_3_link}</a></article>
      <article class="fg-tile"><h3>{tile_4_title}</h3><p>{tile_4_dek}</p><a href="#">{tile_4_link}</a></article>
      <article class="fg-tile"><h3>{tile_5_title}</h3><p>{tile_5_dek}</p><a href="#">{tile_5_link}</a></article>
      <article class="fg-tile"><h3>{tile_6_title}</h3><p>{tile_6_dek}</p><a href="#">{tile_6_link}</a></article>
    </div>
  </div>
</section>
"""

FEATURE_GRID_STYLES = """\
.fg-grid { background: var(--ds-bg); color: var(--ds-text); padding: var(--ds-section-padding-y, 96px) var(--ds-section-padding-x, 32px); }
.fg-grid__inner { max-width: var(--ds-page-max-full, 1200px); margin: 0 auto; }
.fg-grid__head { max-width: var(--ds-page-max-narrow, 720px); margin-bottom: 48px; }
.fg-grid__kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
                   text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
                   color: var(--ds-text-muted); }
.fg-grid__title { font-family: var(--ds-font-display); font-size: var(--ds-text-3xl);
                  line-height: 1.15; margin: 16px 0 12px; }
.fg-grid__dek { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
                color: var(--ds-text-muted); margin: 0; }
.fg-grid__tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--ds-card-grid-gap, 24px); }
.fg-tile { padding: var(--ds-card-padding, 24px); border: var(--ds-card-border-width, 1px) solid var(--ds-border);
           border-radius: var(--ds-card-radius, var(--ds-radius-md, 8px)); background: var(--ds-surface); }
.fg-tile h3 { font-family: var(--ds-font-display); font-size: var(--ds-text-xl);
              margin: 0 0 8px; }
.fg-tile p { font-family: var(--ds-font-body); font-size: var(--ds-text-base);
             color: var(--ds-text-muted); line-height: 1.5;
             margin: 0 0 16px; }
.fg-tile a { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
             font-weight: var(--ds-font-weight-medium, 500); color: var(--ds-accent); text-decoration: none; }
"""

CTA_BLOCK_BODY = """\
<section class="cta">
  <div class="cta__inner">
    <span class="cta__kicker">{kicker}</span>
    <h2 class="cta__title">{title}</h2>
    <p class="cta__dek">{dek}</p>
    <div class="cta__actions">
      <a class="cta__btn cta__btn--primary" href="#">{cta_primary}</a>
      <a class="cta__btn cta__btn--ghost" href="#">{cta_secondary}</a>
    </div>
  </div>
</section>
"""

CTA_BLOCK_STYLES = """\
.cta { background: var(--ds-surface); color: var(--ds-text);
       padding: var(--ds-section-padding-y, 96px) var(--ds-section-padding-x, 32px); border-top: var(--ds-section-divider-width, 1px) solid var(--ds-hairline); }
.cta__inner { max-width: var(--ds-page-max-narrow, 720px); margin: 0 auto; text-align: center; }
.cta__kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
               text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
               color: var(--ds-text-muted); }
.cta__title { font-family: var(--ds-font-display); font-size: var(--ds-text-4xl);
              line-height: 1.1; margin: 16px 0 12px; letter-spacing: -0.015em; }
.cta__dek { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
            color: var(--ds-text-muted); margin: 0 0 32px; }
.cta__actions { display: inline-flex; gap: 12px; }
.cta__btn { padding: var(--ds-button-padding-y, 12px) var(--ds-button-padding-x, 20px); border-radius: var(--ds-button-radius, var(--ds-radius-sm, 6px));
            font-family: var(--ds-button-font-family, var(--ds-font-body)); font-size: var(--ds-button-font-size, var(--ds-text-sm));
            font-weight: var(--ds-button-font-weight, 500); text-decoration: none; }
.cta__btn--primary { background: var(--ds-accent); color: var(--ds-bg); }
.cta__btn--ghost { background: transparent; color: var(--ds-text);
                   border: var(--ds-button-border-width, 1px) solid var(--ds-border); }
"""

PRICING_TABLE_BODY = """\
<section class="pt">
  <div class="pt__inner">
    <header class="pt__head">
      <span class="pt__kicker">{kicker}</span>
      <h2 class="pt__title">{title}</h2>
    </header>
    <div class="pt__tiers">
      <article class="pt__tier"><h3>{tier_1_name}</h3><p class="pt__price">{tier_1_price}</p><p class="pt__dek">{tier_1_dek}</p><a href="#" class="pt__btn">{tier_1_cta}</a></article>
      <article class="pt__tier pt__tier--recommended"><span class="pt__badge">{recommended_label}</span><h3>{tier_2_name}</h3><p class="pt__price">{tier_2_price}</p><p class="pt__dek">{tier_2_dek}</p><a href="#" class="pt__btn pt__btn--primary">{tier_2_cta}</a></article>
      <article class="pt__tier"><h3>{tier_3_name}</h3><p class="pt__price">{tier_3_price}</p><p class="pt__dek">{tier_3_dek}</p><a href="#" class="pt__btn">{tier_3_cta}</a></article>
    </div>
  </div>
</section>
"""

PRICING_TABLE_STYLES = """\
.pt { background: var(--ds-bg); padding: var(--ds-section-padding-y, 96px) var(--ds-section-padding-x, 32px); }
.pt__inner { max-width: var(--ds-page-max-wide, 1100px); margin: 0 auto; }
.pt__head { text-align: center; margin-bottom: 48px; }
.pt__kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
              text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
              color: var(--ds-text-muted); }
.pt__title { font-family: var(--ds-font-display); font-size: var(--ds-text-3xl);
             margin: 16px 0 0; line-height: 1.15; }
.pt__tiers { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--ds-card-grid-gap, 24px); }
.pt__tier { position: relative; padding: 32px var(--ds-card-padding-x, 24px); border: var(--ds-card-border-width, 1px) solid var(--ds-border);
            border-radius: var(--ds-card-radius, var(--ds-radius-md, 8px)); background: var(--ds-surface); }
.pt__tier--recommended { border-color: var(--ds-accent); border-width: 2px; }
.pt__badge { position: absolute; top: -12px; left: 50%; transform: translateX(-50%);
             padding: var(--ds-badge-padding-y, 4px) var(--ds-badge-padding-x, 12px); background: var(--ds-accent); color: var(--ds-bg);
             font-family: var(--ds-font-body); font-size: var(--ds-badge-font-size, var(--ds-text-xs));
             font-weight: var(--ds-badge-font-weight, 500); border-radius: var(--ds-badge-radius, var(--ds-radius-full, 9999px));
             text-transform: uppercase; letter-spacing: var(--ds-tracking-wide, 0.06em); }
.pt__tier h3 { font-family: var(--ds-font-display); font-size: var(--ds-text-xl);
               margin: 0 0 8px; }
.pt__price { font-family: var(--ds-font-display); font-size: var(--ds-text-3xl);
             margin: 16px 0; font-weight: var(--ds-font-weight-display, 600); }
.pt__dek { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
           color: var(--ds-text-muted); line-height: 1.5; margin: 0 0 24px;
           min-height: 3em; }
.pt__btn { display: inline-block; padding: var(--ds-button-padding-y, 10px) var(--ds-button-padding-x, 18px); width: 100%; text-align: center;
           border-radius: var(--ds-button-radius, var(--ds-radius-sm, 6px)); border: var(--ds-button-border-width, 1px) solid var(--ds-border);
           font-family: var(--ds-button-font-family, var(--ds-font-body)); font-size: var(--ds-button-font-size, var(--ds-text-sm));
           font-weight: var(--ds-button-font-weight, 500); color: var(--ds-text); text-decoration: none; box-sizing: border-box; }
.pt__btn--primary { background: var(--ds-accent); color: var(--ds-bg);
                    border-color: var(--ds-accent); }
"""

TESTIMONIALS_BODY = """\
<section class="ts">
  <div class="ts__inner">
    <header class="ts__head">
      <span class="ts__kicker">{kicker}</span>
      <h2 class="ts__title">{title}</h2>
    </header>
    <div class="ts__cards">
      <article class="ts__card"><blockquote>{quote_1}</blockquote><footer><strong>{author_1}</strong><span>{role_1}</span></footer></article>
      <article class="ts__card"><blockquote>{quote_2}</blockquote><footer><strong>{author_2}</strong><span>{role_2}</span></footer></article>
      <article class="ts__card"><blockquote>{quote_3}</blockquote><footer><strong>{author_3}</strong><span>{role_3}</span></footer></article>
    </div>
  </div>
</section>
"""

TESTIMONIALS_STYLES = """\
.ts { background: var(--ds-surface); padding: var(--ds-section-padding-y, 96px) var(--ds-section-padding-x, 32px); }
.ts__inner { max-width: var(--ds-page-max-full, 1200px); margin: 0 auto; }
.ts__head { text-align: center; margin-bottom: 48px; }
.ts__kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
              text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
              color: var(--ds-text-muted); }
.ts__title { font-family: var(--ds-font-display); font-size: var(--ds-text-3xl);
             margin: 16px 0 0; line-height: 1.15; }
.ts__cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--ds-card-grid-gap, 24px); }
.ts__card { padding: 32px var(--ds-card-padding-x, 24px); border: var(--ds-card-border-width, 1px) solid var(--ds-border);
            border-radius: var(--ds-card-radius, var(--ds-radius-md, 8px)); background: var(--ds-bg); }
.ts__card blockquote { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
                       line-height: 1.5; margin: 0 0 24px; color: var(--ds-text); }
.ts__card footer { font-family: var(--ds-font-body); font-size: var(--ds-text-sm); }
.ts__card footer strong { display: block; color: var(--ds-text); font-weight: var(--ds-font-weight-display, 600); }
.ts__card footer span { color: var(--ds-text-muted); }
"""

PROCESS_STEPS_BODY = """\
<section class="ps">
  <div class="ps__inner">
    <header class="ps__head">
      <span class="ps__kicker">{kicker}</span>
      <h2 class="ps__title">{title}</h2>
    </header>
    <ol class="ps__steps">
      <li><span class="ps__num">01</span><h3>{step_1_title}</h3><p>{step_1_dek}</p></li>
      <li><span class="ps__num">02</span><h3>{step_2_title}</h3><p>{step_2_dek}</p></li>
      <li><span class="ps__num">03</span><h3>{step_3_title}</h3><p>{step_3_dek}</p></li>
      <li><span class="ps__num">04</span><h3>{step_4_title}</h3><p>{step_4_dek}</p></li>
    </ol>
  </div>
</section>
"""

PROCESS_STEPS_STYLES = """\
.ps { background: var(--ds-bg); padding: var(--ds-section-padding-y, 96px) var(--ds-section-padding-x, 32px); }
.ps__inner { max-width: var(--ds-page-max-wide, 1100px); margin: 0 auto; }
.ps__head { max-width: var(--ds-page-max-narrow, 720px); margin-bottom: 48px; }
.ps__kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
              text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
              color: var(--ds-text-muted); }
.ps__title { font-family: var(--ds-font-display); font-size: var(--ds-text-3xl);
             margin: 16px 0 0; line-height: 1.15; }
.ps__steps { display: grid; grid-template-columns: repeat(4, 1fr); gap: 32px;
             padding: 0; list-style: none; }
.ps__steps li { padding-top: 32px; border-top: 2px solid var(--ds-accent); }
.ps__num { font-family: var(--ds-font-mono); font-size: var(--ds-text-sm);
           color: var(--ds-accent); font-weight: var(--ds-font-weight-medium, 500); }
.ps__steps li h3 { font-family: var(--ds-font-display); font-size: var(--ds-text-xl);
                   margin: 16px 0 8px; }
.ps__steps li p { font-family: var(--ds-font-body); font-size: var(--ds-text-base);
                  line-height: 1.5; color: var(--ds-text-muted); margin: 0; }
"""

ARTICLE_LAYOUT_BODY = """\
<article class="al">
  <header class="al__head">
    <span class="al__kicker">{kicker}</span>
    <h1 class="al__title">{title}</h1>
    <p class="al__dek">{dek}</p>
  </header>
  <div class="al__body">
    <p>{lead}</p>
    <h2>{section_2_title}</h2>
    <p>{section_2_body}</p>
    <blockquote class="al__pull">{pull_quote}</blockquote>
    <h2>{section_3_title}</h2>
    <p>{section_3_body}</p>
  </div>
</article>
"""

ARTICLE_LAYOUT_STYLES = """\
.al { max-width: var(--ds-page-max-narrow, 720px); margin: 0 auto; padding: 64px var(--ds-page-pad-x, 32px) 96px;
      background: var(--ds-bg); color: var(--ds-text); }
.al__head { margin-bottom: 48px; padding-bottom: 32px;
            border-bottom: 1px solid var(--ds-hairline); }
.al__kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
              text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
              color: var(--ds-text-muted); }
.al__title { font-family: var(--ds-font-display); font-size: var(--ds-text-4xl);
             line-height: 1.1; margin: 16px 0 16px; letter-spacing: var(--ds-tracking-snug, -0.018em); }
.al__dek { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
           line-height: 1.5; color: var(--ds-text-muted); margin: 0 0 24px; }
.al__byline { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
              color: var(--ds-text-muted); display: flex; gap: 8px; }
.al__body { font-family: var(--ds-font-body); font-size: var(--ds-text-base);
            line-height: var(--ds-leading-relaxed, 1.7); }
.al__body p { margin: 0 0 24px; }
.al__body h2 { font-family: var(--ds-font-display); font-size: var(--ds-text-2xl);
               margin: 48px 0 16px; line-height: 1.2; }
.al__pull { margin: 48px 0; padding-left: 24px;
            border-left: 3px solid var(--ds-accent);
            font-family: var(--ds-font-display); font-size: var(--ds-text-xl);
            font-style: italic; color: var(--ds-text); }
"""

ABOUT_TEAM_BODY = """\
<section class="at">
  <div class="at__inner">
    <header class="at__head">
      <span class="at__kicker">{kicker}</span>
      <h2 class="at__title">{title}</h2>
      <p class="at__dek">{dek}</p>
    </header>
    <div class="at__grid">
      <article class="at__member"><div class="at__avatar"></div><h3>{member_1_name}</h3><p>{member_1_role}</p></article>
      <article class="at__member"><div class="at__avatar"></div><h3>{member_2_name}</h3><p>{member_2_role}</p></article>
      <article class="at__member"><div class="at__avatar"></div><h3>{member_3_name}</h3><p>{member_3_role}</p></article>
      <article class="at__member"><div class="at__avatar"></div><h3>{member_4_name}</h3><p>{member_4_role}</p></article>
    </div>
  </div>
</section>
"""

ABOUT_TEAM_STYLES = """\
.at { background: var(--ds-bg); padding: var(--ds-section-padding-y, 96px) var(--ds-section-padding-x, 32px); }
.at__inner { max-width: var(--ds-page-max-wide, 1100px); margin: 0 auto; }
.at__head { max-width: var(--ds-page-max-narrow, 720px); margin-bottom: 48px; }
.at__kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
              text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
              color: var(--ds-text-muted); }
.at__title { font-family: var(--ds-font-display); font-size: var(--ds-text-3xl);
             margin: 16px 0 12px; line-height: 1.15; }
.at__dek { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
           color: var(--ds-text-muted); margin: 0; }
.at__grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--ds-card-grid-gap, 24px); }
.at__member { text-align: center; }
.at__avatar { width: 96px; height: 96px; border-radius: var(--ds-radius-full, 9999px);
              background: var(--ds-surface-2, var(--ds-surface));
              margin: 0 auto 16px; }
.at__member h3 { font-family: var(--ds-font-display); font-size: var(--ds-text-base);
                 margin: 0 0 4px; }
.at__member p { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
                color: var(--ds-text-muted); margin: 0; }
"""

NEWS_LIST_BODY = """\
<section class="nl">
  <div class="nl__inner">
    <header class="nl__head">
      <span class="nl__kicker">{kicker}</span>
      <h2 class="nl__title">{title}</h2>
    </header>
    <ul class="nl__items">
      <li><article><span class="nl__date">{item_1_date}</span><h3>{item_1_title}</h3><p>{item_1_dek}</p></article></li>
      <li><article><span class="nl__date">{item_2_date}</span><h3>{item_2_title}</h3><p>{item_2_dek}</p></article></li>
      <li><article><span class="nl__date">{item_3_date}</span><h3>{item_3_title}</h3><p>{item_3_dek}</p></article></li>
      <li><article><span class="nl__date">{item_4_date}</span><h3>{item_4_title}</h3><p>{item_4_dek}</p></article></li>
    </ul>
  </div>
</section>
"""

NEWS_LIST_STYLES = """\
.nl { background: var(--ds-bg); padding: var(--ds-section-padding-y, 96px) var(--ds-section-padding-x, 32px); }
.nl__inner { max-width: var(--ds-page-max-default, 880px); margin: 0 auto; }
.nl__head { margin-bottom: 32px; }
.nl__kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
              text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
              color: var(--ds-text-muted); }
.nl__title { font-family: var(--ds-font-display); font-size: var(--ds-text-3xl);
             margin: 16px 0 0; }
.nl__items { padding: 0; list-style: none; }
.nl__items li { padding: 24px 0; border-bottom: 1px solid var(--ds-hairline); }
.nl__date { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
            color: var(--ds-text-muted); text-transform: uppercase;
            letter-spacing: var(--ds-tracking-wide, 0.06em); }
.nl__items h3 { font-family: var(--ds-font-display); font-size: var(--ds-text-xl);
                margin: 8px 0; line-height: 1.2; }
.nl__items p { font-family: var(--ds-font-body); font-size: var(--ds-text-base);
               color: var(--ds-text-muted); line-height: 1.5; margin: 0; }
"""

# ----------------------------------------------------------------------
# Library template — composes 6 atoms inline into a single specimen page.
# ----------------------------------------------------------------------

LIBRARY_BODY = """\
<main class="l-page">
  <header class="l-header">
    <span class="l-kicker">{kicker}</span>
    <h1 class="l-title">{title}</h1>
    <p class="l-dek">{dek}</p>
  </header>

  <section class="l-section">
    <h2 class="l-section__title">Buttons</h2>
    <div class="l-row">
      <button class="l-btn l-btn--primary">{button_primary}</button>
      <button class="l-btn l-btn--ghost">{button_ghost}</button>
    </div>
  </section>

  <section class="l-section">
    <h2 class="l-section__title">Card</h2>
    <article class="l-card">
      <h3>{card_title}</h3>
      <p>{card_body}</p>
      <a href="#">{card_link}</a>
    </article>
  </section>

  <section class="l-section">
    <h2 class="l-section__title">Nav link &amp; wordmark</h2>
    <div class="l-row">
      <a class="l-navlink" href="#">{nav_link}</a>
      <span class="l-wordmark">{wordmark}</span>
    </div>
  </section>

  <section class="l-section">
    <h2 class="l-section__title">Pricing tier (recommended)</h2>
    <article class="l-tier">
      <span class="l-tier__badge">{recommended_label}</span>
      <h3>{tier_name}</h3>
      <p class="l-tier__price">{tier_price}</p>
      <p class="l-tier__dek">{tier_dek}</p>
    </article>
  </section>

  <section class="l-section">
    <h2 class="l-section__title">Testimonial</h2>
    <article class="l-testimonial">
      <blockquote>{testimonial_quote}</blockquote>
      <footer><strong>{testimonial_author}</strong> · <span>{testimonial_role}</span></footer>
    </article>
  </section>

  <section class="l-section">
    <h2 class="l-section__title">Hairline divider &amp; kicker</h2>
    <hr class="l-hairline"/>
    <span class="l-kicker">{kicker_sample}</span>
  </section>
</main>
"""

LIBRARY_STYLES = """\
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { background: var(--ds-bg); color: var(--ds-text);
       font-family: var(--ds-font-body); font-size: var(--ds-text-base);
       line-height: 1.55; }
.l-page { max-width: var(--ds-page-max-default, 880px); margin: 0 auto; padding: var(--ds-page-pad-y, 96px) var(--ds-page-pad-x, 32px) 160px; }
.l-header { margin-bottom: 64px; }
.l-kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
            text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
            color: var(--ds-text-muted); }
.l-title { font-family: var(--ds-font-display); font-size: var(--ds-text-4xl);
           line-height: 1.1; margin: 16px 0 12px; letter-spacing: var(--ds-tracking-snug, -0.018em); }
.l-dek { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
         color: var(--ds-text-muted); margin: 0; }
.l-section { padding: 48px 0; border-top: var(--ds-section-divider-width, 1px) solid var(--ds-hairline); }
.l-section__title { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
                    text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
                    color: var(--ds-text-muted); margin: 0 0 16px; }
.l-row { display: flex; gap: 12px; align-items: center; }
.l-btn { padding: var(--ds-button-padding-y, 10px) var(--ds-button-padding-x, 16px); border-radius: var(--ds-button-radius, var(--ds-radius-sm, 6px));
         font-family: var(--ds-button-font-family, var(--ds-font-body)); font-size: var(--ds-button-font-size, var(--ds-text-sm));
         font-weight: var(--ds-button-font-weight, 500); border: 0; cursor: pointer; }
.l-btn--primary { background: var(--ds-accent); color: var(--ds-bg); }
.l-btn--ghost { background: transparent; color: var(--ds-text);
                border: var(--ds-button-border-width, 1px) solid var(--ds-border); }
.l-card { padding: var(--ds-card-padding, 24px); border: var(--ds-card-border-width, 1px) solid var(--ds-border);
          border-radius: var(--ds-card-radius, var(--ds-radius-md, 8px)); background: var(--ds-surface); }
.l-card h3 { font-family: var(--ds-font-display); font-size: var(--ds-text-xl);
             margin: 0 0 8px; }
.l-card p { font-family: var(--ds-font-body); font-size: var(--ds-text-base);
            color: var(--ds-text-muted); margin: 0 0 12px; }
.l-card a { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
            color: var(--ds-accent); text-decoration: none; font-weight: var(--ds-font-weight-medium, 500); }
.l-navlink { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
             color: var(--ds-text); text-decoration: none; font-weight: var(--ds-font-weight-medium, 500); }
.l-wordmark { font-family: var(--ds-font-display); font-weight: var(--ds-font-weight-display, 600);
              font-size: var(--ds-text-lg); }
.l-tier { position: relative; padding: var(--ds-card-padding, 24px); border: 2px solid var(--ds-accent);
          border-radius: var(--ds-card-radius, var(--ds-radius-md, 8px)); background: var(--ds-surface);
          max-width: 320px; }
.l-tier__badge { position: absolute; top: -10px; left: 16px; padding: var(--ds-badge-padding-y, 4px) var(--ds-badge-padding-x, 10px);
                 background: var(--ds-accent); color: var(--ds-bg);
                 font-family: var(--ds-font-body); font-size: var(--ds-badge-font-size, var(--ds-text-xs));
                 font-weight: var(--ds-badge-font-weight, 500); border-radius: var(--ds-badge-radius, var(--ds-radius-full, 9999px));
                 text-transform: uppercase; letter-spacing: var(--ds-tracking-wide, 0.06em); }
.l-tier h3 { font-family: var(--ds-font-display); font-size: var(--ds-text-xl);
             margin: 0 0 8px; }
.l-tier__price { font-family: var(--ds-font-display); font-size: var(--ds-text-2xl);
                 margin: 8px 0; font-weight: var(--ds-font-weight-display, 600); }
.l-tier__dek { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
               color: var(--ds-text-muted); margin: 0; }
.l-testimonial blockquote { font-family: var(--ds-font-body);
                            font-size: var(--ds-text-lg); line-height: 1.5;
                            margin: 0 0 12px; color: var(--ds-text); }
.l-testimonial footer { font-family: var(--ds-font-body); font-size: var(--ds-text-sm); }
.l-testimonial footer strong { font-weight: var(--ds-font-weight-display, 600); }
.l-testimonial footer span { color: var(--ds-text-muted); }
.l-hairline { border: 0; border-top: 1px solid var(--ds-hairline); margin: 16px 0; }
"""

# ----------------------------------------------------------------------
# Component-library templates (added for Resemblio Library v1.1 per
# mission `projects/OptSus Team/missions/resemblio-library-v1.1.md` D3).
# Each renders ONE component category with 3-7 variants so the
# composed page reads as a category showcase rather than a single example.
# Naming: short prefix `b-` (buttons), `ff-` (form-fields), `i-` (inputs),
# `bd-` (badges), `cd-` (cards). All visual decisions bound to --ds-*.
# ----------------------------------------------------------------------

BUTTONS_BODY = """\
<main class="b-page">
  <header class="b-header">
    <span class="b-kicker">{kicker}</span>
    <h1 class="b-title">{title}</h1>
    <p class="b-dek">{dek}</p>
  </header>

  <section class="b-section">
    <h2 class="b-section__title">Variants</h2>
    <div class="b-row">
      <button class="b-btn b-btn--primary" type="button">{label_primary}</button>
      <button class="b-btn b-btn--secondary" type="button">{label_secondary}</button>
      <button class="b-btn b-btn--outline" type="button">{label_outline}</button>
      <button class="b-btn b-btn--ghost" type="button">{label_ghost}</button>
      <button class="b-btn b-btn--destructive" type="button">{label_destructive}</button>
    </div>
  </section>

  <section class="b-section">
    <h2 class="b-section__title">Sizes</h2>
    <div class="b-row">
      <button class="b-btn b-btn--primary b-btn--sm" type="button">{label_sm}</button>
      <button class="b-btn b-btn--primary b-btn--md" type="button">{label_md}</button>
      <button class="b-btn b-btn--primary b-btn--lg" type="button">{label_lg}</button>
    </div>
  </section>

  <section class="b-section">
    <h2 class="b-section__title">With icon</h2>
    <div class="b-row">
      <button class="b-btn b-btn--primary" type="button">
        <span class="b-btn__icon" aria-hidden="true">+</span>
        <span>{label_icon_leading}</span>
      </button>
      <button class="b-btn b-btn--outline" type="button">
        <span>{label_icon_trailing}</span>
        <span class="b-btn__icon" aria-hidden="true">&rarr;</span>
      </button>
    </div>
  </section>

  <section class="b-section">
    <h2 class="b-section__title">Disabled</h2>
    <div class="b-row">
      <button class="b-btn b-btn--primary" type="button" disabled aria-disabled="true">{label_disabled}</button>
      <button class="b-btn b-btn--outline" type="button" disabled aria-disabled="true">{label_disabled_outline}</button>
    </div>
  </section>
</main>
"""

BUTTONS_STYLES = """\
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { background: var(--ds-bg); color: var(--ds-text);
       font-family: var(--ds-font-body); font-size: var(--ds-text-base);
       line-height: var(--ds-leading-normal, 1.55); }
.b-page { max-width: var(--ds-page-max-default, 880px); margin: 0 auto; padding: var(--ds-page-pad-y, 96px) var(--ds-page-pad-x, 32px) 160px; }
.b-header { margin-bottom: 56px; }
.b-kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
            text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
            color: var(--ds-text-muted); }
.b-title { font-family: var(--ds-font-display); font-size: var(--ds-text-4xl);
           line-height: 1.1; margin: 16px 0 12px; letter-spacing: var(--ds-tracking-snug, -0.018em); }
.b-dek { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
         color: var(--ds-text-muted); margin: 0; }
.b-section { padding: 32px 0; border-top: var(--ds-section-divider-width, 1px) solid var(--ds-hairline); }
.b-section__title { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
                    text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
                    color: var(--ds-text-muted); margin: 0 0 16px; }
.b-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.b-btn { display: inline-flex; align-items: center; gap: 8px;
         padding: var(--ds-button-padding-y, 10px) var(--ds-button-padding-x, 16px); font-family: var(--ds-button-font-family, var(--ds-font-body));
         font-size: var(--ds-button-font-size, var(--ds-text-sm)); font-weight: var(--ds-button-font-weight, 500);
         border-radius: var(--ds-button-radius, var(--ds-radius-sm, 6px)); border: var(--ds-button-border-width, 1px) solid transparent;
         cursor: pointer; line-height: 1.2;
         transition: background var(--ds-duration-fast, 150ms) var(--ds-ease-standard, ease); }
.b-btn:focus-visible { outline: 2px solid var(--ds-focus-ring, var(--ds-accent)); outline-offset: 2px; }
.b-btn[disabled] { opacity: 0.5; cursor: not-allowed; }
.b-btn--primary { background: var(--ds-accent); color: var(--ds-bg); border-color: var(--ds-accent); }
.b-btn--secondary { background: var(--ds-accent-2, var(--ds-surface-2, var(--ds-surface)));
                    color: var(--ds-text); border-color: var(--ds-accent-2, var(--ds-border)); }
.b-btn--outline { background: transparent; color: var(--ds-text);
                  border-color: var(--ds-border); }
.b-btn--ghost { background: transparent; color: var(--ds-text); border-color: transparent; }
.b-btn--destructive { background: var(--ds-error); color: var(--ds-bg);
                      border-color: var(--ds-error); }
.b-btn--sm { padding: var(--ds-button-sm-padding-y, 6px) var(--ds-button-sm-padding-x, 12px); font-size: var(--ds-text-xs); }
.b-btn--md { padding: var(--ds-button-padding-y, 10px) var(--ds-button-padding-x, 16px); font-size: var(--ds-button-font-size, var(--ds-text-sm)); }
.b-btn--lg { padding: var(--ds-button-lg-padding-y, 14px) var(--ds-button-lg-padding-x, 22px); font-size: var(--ds-text-base); }
.b-btn__icon { font-family: var(--ds-font-mono); font-size: 0.95em;
               display: inline-flex; align-items: center; }
"""

FORM_FIELDS_BODY = """\
<main class="ff-page">
  <header class="ff-header">
    <span class="ff-kicker">{kicker}</span>
    <h1 class="ff-title">{title}</h1>
    <p class="ff-dek">{dek}</p>
  </header>

  <form class="ff-form" onsubmit="return false">
    <fieldset class="ff-set">
      <legend class="ff-legend">{legend_text}</legend>

      <div class="ff-field">
        <label class="ff-label" for="ff-text">{label_text}</label>
        <input class="ff-input" id="ff-text" type="text" placeholder="{placeholder_text}" />
        <small class="ff-help">{help_text}</small>
      </div>

      <div class="ff-field">
        <label class="ff-label" for="ff-textarea">{label_textarea}</label>
        <textarea class="ff-input ff-input--multiline" id="ff-textarea" rows="3"
                  placeholder="{placeholder_textarea}"></textarea>
      </div>

      <div class="ff-field">
        <label class="ff-label" for="ff-select">{label_select}</label>
        <select class="ff-input" id="ff-select">
          <option>{option_1}</option>
          <option>{option_2}</option>
          <option>{option_3}</option>
        </select>
      </div>

      <div class="ff-field ff-field--inline">
        <input class="ff-check" id="ff-checkbox" type="checkbox" />
        <label class="ff-label ff-label--inline" for="ff-checkbox">{label_checkbox}</label>
      </div>

      <div class="ff-field ff-field--group" role="radiogroup" aria-label="{radio_group_label}">
        <span class="ff-label">{radio_group_label}</span>
        <div class="ff-radio-row">
          <label class="ff-radio"><input type="radio" name="ff-radio"/> <span>{radio_1}</span></label>
          <label class="ff-radio"><input type="radio" name="ff-radio"/> <span>{radio_2}</span></label>
          <label class="ff-radio"><input type="radio" name="ff-radio"/> <span>{radio_3}</span></label>
        </div>
      </div>

      <div class="ff-field">
        <label class="ff-label" for="ff-date">{label_date}</label>
        <input class="ff-input" id="ff-date" type="date" />
      </div>

      <div class="ff-field">
        <label class="ff-label" for="ff-file">{label_file}</label>
        <input class="ff-input ff-input--file" id="ff-file" type="file" />
      </div>

      <div class="ff-field ff-field--error">
        <label class="ff-label" for="ff-error">{label_error}</label>
        <input class="ff-input ff-input--invalid" id="ff-error" type="email"
               value="not-an-email" aria-invalid="true" aria-describedby="ff-error-msg"/>
        <small class="ff-error-msg" id="ff-error-msg">{error_text}</small>
      </div>
    </fieldset>
  </form>
</main>
"""

FORM_FIELDS_STYLES = """\
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { background: var(--ds-bg); color: var(--ds-text);
       font-family: var(--ds-font-body); font-size: var(--ds-text-base);
       line-height: var(--ds-leading-normal, 1.55); }
.ff-page { max-width: var(--ds-page-max-narrow, 720px); margin: 0 auto; padding: var(--ds-page-pad-y, 96px) var(--ds-page-pad-x, 32px) 160px; }
.ff-header { margin-bottom: 48px; }
.ff-kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
             text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
             color: var(--ds-text-muted); }
.ff-title { font-family: var(--ds-font-display); font-size: var(--ds-text-4xl);
            line-height: 1.1; margin: 16px 0 12px; letter-spacing: var(--ds-tracking-snug, -0.018em); }
.ff-dek { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
          color: var(--ds-text-muted); margin: 0; }
.ff-form { display: block; }
.ff-set { border: 0; padding: 0; margin: 0; }
.ff-legend { font-family: var(--ds-font-display); font-size: var(--ds-text-xl);
             margin: 0 0 24px; padding: 0; font-weight: var(--ds-font-weight-display, 600); }
.ff-field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 20px; }
.ff-field--inline { flex-direction: row; align-items: center; gap: 10px; }
.ff-label { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
            font-weight: var(--ds-font-weight-medium, 500); color: var(--ds-text); }
.ff-label--inline { font-weight: var(--ds-font-weight-body, 400); }
.ff-input { font-family: var(--ds-input-font-family, var(--ds-font-body)); font-size: var(--ds-input-font-size, var(--ds-text-base));
            padding: var(--ds-input-padding-y, 10px) var(--ds-input-padding-x, 12px); background: var(--ds-surface);
            color: var(--ds-text); border: var(--ds-input-border-width, 1px) solid var(--ds-border);
            border-radius: var(--ds-input-radius, var(--ds-radius-sm, 6px)); width: 100%; line-height: var(--ds-input-line-height, 1.4); }
.ff-input:focus-visible { outline: 2px solid var(--ds-focus-ring, var(--ds-accent));
                          outline-offset: 1px; border-color: var(--ds-accent); }
.ff-input--multiline { font-family: var(--ds-font-body); resize: vertical; min-height: 96px; }
.ff-input--file { padding: 8px; background: var(--ds-surface-2, var(--ds-surface)); }
.ff-input--invalid { border-color: var(--ds-error); }
.ff-help { font-family: var(--ds-font-body); font-size: var(--ds-text-xs);
           color: var(--ds-text-muted); }
.ff-error-msg { font-family: var(--ds-font-body); font-size: var(--ds-text-xs);
                color: var(--ds-error); }
.ff-check { width: 16px; height: 16px; accent-color: var(--ds-accent); }
.ff-radio-row { display: flex; gap: 16px; flex-wrap: wrap; }
.ff-radio { display: inline-flex; align-items: center; gap: 6px;
            font-family: var(--ds-font-body); font-size: var(--ds-text-sm); }
.ff-radio input { accent-color: var(--ds-accent); }
"""

INPUTS_BODY = """\
<main class="i-page">
  <header class="i-header">
    <span class="i-kicker">{kicker}</span>
    <h1 class="i-title">{title}</h1>
    <p class="i-dek">{dek}</p>
  </header>

  <section class="i-section">
    <h2 class="i-section__title">Search</h2>
    <label class="i-search" aria-label="{search_label}">
      <span class="i-search__icon" aria-hidden="true">&#x2315;</span>
      <input class="i-search__input" type="search" placeholder="{search_placeholder}" />
    </label>
  </section>

  <section class="i-section">
    <h2 class="i-section__title">Tag input</h2>
    <div class="i-tags" role="group" aria-label="{tags_label}">
      <span class="i-tag">{tag_1}<button class="i-tag__x" type="button" aria-label="Remove">&times;</button></span>
      <span class="i-tag">{tag_2}<button class="i-tag__x" type="button" aria-label="Remove">&times;</button></span>
      <span class="i-tag">{tag_3}<button class="i-tag__x" type="button" aria-label="Remove">&times;</button></span>
      <input class="i-tags__input" type="text" placeholder="{tags_placeholder}" />
    </div>
  </section>

  <section class="i-section">
    <h2 class="i-section__title">Segmented control</h2>
    <div class="i-seg" role="tablist" aria-label="{segmented_label}">
      <button class="i-seg__btn i-seg__btn--active" role="tab" aria-selected="true">{seg_1}</button>
      <button class="i-seg__btn" role="tab" aria-selected="false">{seg_2}</button>
      <button class="i-seg__btn" role="tab" aria-selected="false">{seg_3}</button>
    </div>
  </section>

  <section class="i-section">
    <h2 class="i-section__title">Toggle</h2>
    <label class="i-toggle">
      <input type="checkbox" class="i-toggle__input" checked />
      <span class="i-toggle__track" aria-hidden="true"><span class="i-toggle__thumb"></span></span>
      <span class="i-toggle__label">{toggle_label}</span>
    </label>
  </section>

  <section class="i-section">
    <h2 class="i-section__title">Stepper</h2>
    <div class="i-step" role="group" aria-label="{stepper_label}">
      <button class="i-step__btn" type="button" aria-label="Decrement">&minus;</button>
      <span class="i-step__value">{stepper_value}</span>
      <button class="i-step__btn" type="button" aria-label="Increment">+</button>
    </div>
  </section>
</main>
"""

INPUTS_STYLES = """\
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { background: var(--ds-bg); color: var(--ds-text);
       font-family: var(--ds-font-body); font-size: var(--ds-text-base);
       line-height: var(--ds-leading-normal, 1.55); }
.i-page { max-width: var(--ds-page-max-narrow, 720px); margin: 0 auto; padding: var(--ds-page-pad-y, 96px) var(--ds-page-pad-x, 32px) 160px; }
.i-header { margin-bottom: 48px; }
.i-kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
            text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
            color: var(--ds-text-muted); }
.i-title { font-family: var(--ds-font-display); font-size: var(--ds-text-4xl);
           line-height: 1.1; margin: 16px 0 12px; letter-spacing: var(--ds-tracking-snug, -0.018em); }
.i-dek { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
         color: var(--ds-text-muted); margin: 0; }
.i-section { padding: 24px 0; border-top: var(--ds-section-divider-width, 1px) solid var(--ds-hairline); }
.i-section__title { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
                    text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
                    color: var(--ds-text-muted); margin: 0 0 16px; }
.i-search { display: inline-flex; align-items: center; gap: 8px;
            padding: 10px 14px; background: var(--ds-surface);
            border: 1px solid var(--ds-border);
            border-radius: var(--ds-radius-full, 9999px); min-width: 320px; }
.i-search__icon { color: var(--ds-text-muted); font-size: var(--ds-text-base); }
.i-search__input { flex: 1; border: 0; background: transparent;
                   font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
                   color: var(--ds-text); outline: 0; }
.i-tags { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px;
          border: 1px solid var(--ds-border); border-radius: var(--ds-radius-sm, 6px);
          background: var(--ds-surface); }
.i-tag { display: inline-flex; align-items: center; gap: 6px;
         padding: 4px 8px; background: var(--ds-surface-2, var(--ds-bg));
         border: 1px solid var(--ds-hairline);
         border-radius: var(--ds-radius-sm, 4px);
         font-family: var(--ds-font-body); font-size: var(--ds-text-xs);
         color: var(--ds-text); }
.i-tag__x { border: 0; background: transparent; cursor: pointer;
            color: var(--ds-text-muted); font-size: var(--ds-text-sm);
            line-height: 1; padding: 0; }
.i-tags__input { flex: 1; min-width: 120px; border: 0; outline: 0;
                 background: transparent; font-family: var(--ds-font-body);
                 font-size: var(--ds-text-sm); color: var(--ds-text);
                 padding: 4px; }
.i-seg { display: inline-flex; padding: 2px; background: var(--ds-surface);
         border: 1px solid var(--ds-border);
         border-radius: var(--ds-radius-sm, 6px); }
.i-seg__btn { border: 0; background: transparent; cursor: pointer;
              padding: 6px 14px; font-family: var(--ds-font-body);
              font-size: var(--ds-text-sm); color: var(--ds-text-muted);
              border-radius: var(--ds-radius-xs, 4px); }
.i-seg__btn--active { background: var(--ds-bg); color: var(--ds-text);
                      box-shadow: var(--ds-shadow-xs, 0 1px 1px rgba(0,0,0,0.06)); }
.i-toggle { display: inline-flex; align-items: center; gap: 12px; cursor: pointer; }
.i-toggle__input { position: absolute; opacity: 0; pointer-events: none; }
.i-toggle__track { position: relative; display: inline-block; width: 36px; height: 20px;
                   background: var(--ds-border); border-radius: var(--ds-radius-full, 9999px);
                   transition: background var(--ds-duration-fast, 150ms) ease; }
.i-toggle__thumb { position: absolute; top: 2px; left: 2px; width: 16px; height: 16px;
                   background: var(--ds-bg); border-radius: var(--ds-radius-full, 9999px);
                   transition: transform var(--ds-duration-fast, 150ms) ease;
                   box-shadow: var(--ds-shadow-xs, 0 1px 1px rgba(0,0,0,0.1)); }
.i-toggle__input:checked + .i-toggle__track { background: var(--ds-accent); }
.i-toggle__input:checked + .i-toggle__track .i-toggle__thumb { transform: translateX(16px); }
.i-toggle__label { font-family: var(--ds-font-body); font-size: var(--ds-text-sm); }
.i-step { display: inline-flex; align-items: stretch;
          border: 1px solid var(--ds-border);
          border-radius: var(--ds-radius-sm, 6px);
          background: var(--ds-surface); overflow: hidden; }
.i-step__btn { border: 0; background: transparent; cursor: pointer;
               padding: 6px 14px; font-family: var(--ds-font-mono);
               font-size: var(--ds-text-base); color: var(--ds-text); }
.i-step__value { min-width: 48px; display: inline-flex; align-items: center;
                 justify-content: center; font-family: var(--ds-font-mono);
                 font-size: var(--ds-text-sm);
                 border-left: 1px solid var(--ds-hairline);
                 border-right: 1px solid var(--ds-hairline);
                 background: var(--ds-bg); }
"""

BADGES_BODY = """\
<main class="bd-page">
  <header class="bd-header">
    <span class="bd-kicker">{kicker}</span>
    <h1 class="bd-title">{title}</h1>
    <p class="bd-dek">{dek}</p>
  </header>

  <section class="bd-section">
    <h2 class="bd-section__title">Semantic</h2>
    <div class="bd-row">
      <span class="bd-badge bd-badge--info">{label_info}</span>
      <span class="bd-badge bd-badge--success">{label_success}</span>
      <span class="bd-badge bd-badge--warning">{label_warning}</span>
      <span class="bd-badge bd-badge--error">{label_error}</span>
      <span class="bd-badge bd-badge--neutral">{label_neutral}</span>
    </div>
  </section>

  <section class="bd-section">
    <h2 class="bd-section__title">Sizes</h2>
    <div class="bd-row">
      <span class="bd-badge bd-badge--neutral bd-badge--sm">{label_sm}</span>
      <span class="bd-badge bd-badge--neutral bd-badge--md">{label_md}</span>
      <span class="bd-badge bd-badge--neutral bd-badge--lg">{label_lg}</span>
    </div>
  </section>

  <section class="bd-section">
    <h2 class="bd-section__title">With icon</h2>
    <div class="bd-row">
      <span class="bd-badge bd-badge--success"><span class="bd-badge__dot" aria-hidden="true"></span>{label_with_icon_1}</span>
      <span class="bd-badge bd-badge--warning"><span class="bd-badge__dot" aria-hidden="true"></span>{label_with_icon_2}</span>
    </div>
  </section>

  <section class="bd-section">
    <h2 class="bd-section__title">Status indicators</h2>
    <div class="bd-row">
      <span class="bd-status"><span class="bd-status__dot bd-status__dot--online" aria-hidden="true"></span>{label_online}</span>
      <span class="bd-status"><span class="bd-status__dot bd-status__dot--away" aria-hidden="true"></span>{label_away}</span>
      <span class="bd-status"><span class="bd-status__dot bd-status__dot--offline" aria-hidden="true"></span>{label_offline}</span>
    </div>
  </section>
</main>
"""

BADGES_STYLES = """\
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { background: var(--ds-bg); color: var(--ds-text);
       font-family: var(--ds-font-body); font-size: var(--ds-text-base);
       line-height: var(--ds-leading-normal, 1.55); }
.bd-page { max-width: var(--ds-page-max-default, 880px); margin: 0 auto; padding: var(--ds-page-pad-y, 96px) var(--ds-page-pad-x, 32px) 160px; }
.bd-header { margin-bottom: 48px; }
.bd-kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
             text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
             color: var(--ds-text-muted); }
.bd-title { font-family: var(--ds-font-display); font-size: var(--ds-text-4xl);
            line-height: 1.1; margin: 16px 0 12px; letter-spacing: var(--ds-tracking-snug, -0.018em); }
.bd-dek { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
          color: var(--ds-text-muted); margin: 0; }
.bd-section { padding: 28px 0; border-top: var(--ds-section-divider-width, 1px) solid var(--ds-hairline); }
.bd-section__title { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
                     text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
                     color: var(--ds-text-muted); margin: 0 0 16px; }
.bd-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.bd-badge { display: inline-flex; align-items: center; gap: 6px;
            padding: var(--ds-badge-padding-y, 3px) var(--ds-badge-padding-x, 10px); font-family: var(--ds-font-body);
            font-size: var(--ds-badge-font-size, var(--ds-text-xs)); font-weight: var(--ds-badge-font-weight, 500);
            border-radius: var(--ds-badge-radius, var(--ds-radius-full, 9999px));
            border: var(--ds-badge-border-width, 1px) solid transparent; line-height: 1.4; }
.bd-badge--info { background: var(--ds-info); color: var(--ds-bg); }
.bd-badge--success { background: var(--ds-success); color: var(--ds-bg); }
.bd-badge--warning { background: var(--ds-warning); color: var(--ds-bg); }
.bd-badge--error { background: var(--ds-error); color: var(--ds-bg); }
.bd-badge--neutral { background: var(--ds-surface-2, var(--ds-surface));
                     color: var(--ds-text); border-color: var(--ds-border); }
.bd-badge--sm { font-size: var(--ds-text-2xs, 10px); padding: var(--ds-badge-sm-padding-y, 2px) var(--ds-badge-sm-padding-x, 8px); }
.bd-badge--md { font-size: var(--ds-text-xs); padding: var(--ds-badge-padding-y, 3px) var(--ds-badge-padding-x, 10px); }
.bd-badge--lg { font-size: var(--ds-text-sm); padding: var(--ds-badge-lg-padding-y, 5px) var(--ds-badge-lg-padding-x, 12px); }
.bd-badge__dot { width: 6px; height: 6px;
                 border-radius: var(--ds-radius-full, 9999px);
                 background: currentColor; opacity: 0.85; }
.bd-status { display: inline-flex; align-items: center; gap: 8px;
             font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
             color: var(--ds-text); }
.bd-status__dot { width: 8px; height: 8px;
                  border-radius: var(--ds-radius-full, 9999px);
                  display: inline-block; }
.bd-status__dot--online { background: var(--ds-success); }
.bd-status__dot--away { background: var(--ds-warning); }
.bd-status__dot--offline { background: var(--ds-text-muted); }
"""

CARDS_BODY = """\
<main class="cd-page">
  <header class="cd-header">
    <span class="cd-kicker">{kicker}</span>
    <h1 class="cd-title">{title}</h1>
    <p class="cd-dek">{dek}</p>
  </header>

  <section class="cd-grid">

    <article class="cd-card cd-card--basic">
      <h3 class="cd-card__title">{basic_title}</h3>
      <p class="cd-card__body">{basic_body}</p>
      <a class="cd-card__link" href="#">{basic_link}</a>
    </article>

    <article class="cd-card cd-card--image">
      <div class="cd-card__image" aria-hidden="true"></div>
      <div class="cd-card__pad">
        <h3 class="cd-card__title">{image_title}</h3>
        <p class="cd-card__body">{image_body}</p>
      </div>
    </article>

    <article class="cd-card cd-card--quote">
      <blockquote class="cd-card__quote">{quote_text}</blockquote>
      <footer class="cd-card__byline">
        <strong>{quote_author}</strong>
        <span>{quote_role}</span>
      </footer>
    </article>

    <article class="cd-card cd-card--pricing">
      <span class="cd-card__eyebrow">{pricing_eyebrow}</span>
      <h3 class="cd-card__title">{pricing_tier}</h3>
      <p class="cd-card__price"><strong>{pricing_amount}</strong><span>{pricing_period}</span></p>
      <p class="cd-card__body">{pricing_dek}</p>
      <a class="cd-card__cta" href="#">{pricing_cta}</a>
    </article>

    <article class="cd-card cd-card--stat">
      <span class="cd-card__eyebrow">{stat_label}</span>
      <p class="cd-card__stat">{stat_value}</p>
      <p class="cd-card__body">{stat_dek}</p>
    </article>

    <article class="cd-card cd-card--list">
      <h3 class="cd-card__title">{list_title}</h3>
      <ul class="cd-card__list">
        <li>{list_item_1}</li>
        <li>{list_item_2}</li>
        <li>{list_item_3}</li>
      </ul>
    </article>

  </section>
</main>
"""

CARDS_STYLES = """\
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { background: var(--ds-bg); color: var(--ds-text);
       font-family: var(--ds-font-body); font-size: var(--ds-text-base);
       line-height: var(--ds-leading-normal, 1.55); }
.cd-page { max-width: var(--ds-page-max-wide, 1100px); margin: 0 auto; padding: var(--ds-page-pad-y, 96px) var(--ds-page-pad-x, 32px) 160px; }
.cd-header { margin-bottom: 48px; }
.cd-kicker { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
             text-transform: uppercase; letter-spacing: var(--ds-tracking-wider, 0.08em);
             color: var(--ds-text-muted); }
.cd-title { font-family: var(--ds-font-display); font-size: var(--ds-text-4xl);
            line-height: 1.1; margin: 16px 0 12px; letter-spacing: var(--ds-tracking-snug, -0.018em); }
.cd-dek { font-family: var(--ds-font-body); font-size: var(--ds-text-lg);
          color: var(--ds-text-muted); margin: 0; }
.cd-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: var(--ds-card-grid-gap, 20px); }
.cd-card { background: var(--ds-surface); border: var(--ds-card-border-width, 1px) solid var(--ds-border);
           border-radius: var(--ds-card-radius, var(--ds-radius-md, 8px)); padding: var(--ds-card-padding, 24px);
           display: flex; flex-direction: column; gap: var(--ds-card-gap, 12px); }
.cd-card__eyebrow { font-family: var(--ds-font-mono); font-size: var(--ds-text-xs);
                    text-transform: uppercase; letter-spacing: var(--ds-tracking-wide, 0.06em);
                    color: var(--ds-text-muted); }
.cd-card__title { font-family: var(--ds-font-display); font-size: var(--ds-text-xl);
                  margin: 0; font-weight: var(--ds-font-weight-display, 600); line-height: 1.2; }
.cd-card__body { font-family: var(--ds-font-body); font-size: var(--ds-text-base);
                 color: var(--ds-text-muted); margin: 0; line-height: 1.5; }
.cd-card__link { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
                 font-weight: var(--ds-font-weight-medium, 500); color: var(--ds-accent); text-decoration: none; }
.cd-card--image { padding: 0; overflow: hidden; }
.cd-card__image { height: 140px;
                  background: linear-gradient(135deg,
                    var(--ds-accent) 0%,
                    var(--ds-accent-2, var(--ds-surface-2, var(--ds-surface))) 100%); }
.cd-card__pad { padding: 20px; display: flex; flex-direction: column; gap: 8px; }
.cd-card--quote .cd-card__quote { font-family: var(--ds-font-display);
                                   font-size: var(--ds-text-lg); font-style: italic;
                                   color: var(--ds-text); margin: 0;
                                   border-left: 3px solid var(--ds-accent);
                                   padding-left: 16px; line-height: 1.4; }
.cd-card__byline { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
                   color: var(--ds-text-muted); display: flex; flex-direction: column;
                   gap: 2px; }
.cd-card__byline strong { color: var(--ds-text); font-weight: var(--ds-font-weight-display, 600); }
.cd-card--pricing { border-color: var(--ds-accent); border-width: var(--ds-card-border-width, 1px); }
.cd-card__price { display: flex; align-items: baseline; gap: 4px;
                  font-family: var(--ds-font-display); margin: 0; }
.cd-card__price strong { font-size: var(--ds-text-3xl); font-weight: var(--ds-font-weight-display, 600); color: var(--ds-text); }
.cd-card__price span { font-family: var(--ds-font-body); font-size: var(--ds-text-sm);
                       color: var(--ds-text-muted); }
.cd-card__cta { display: inline-block; padding: var(--ds-button-padding-y, 10px) var(--ds-button-padding-x, 16px);
                background: var(--ds-accent); color: var(--ds-bg);
                font-family: var(--ds-button-font-family, var(--ds-font-body)); font-size: var(--ds-button-font-size, var(--ds-text-sm));
                font-weight: var(--ds-button-font-weight, 500); border-radius: var(--ds-button-radius, var(--ds-radius-sm, 6px));
                text-decoration: none; text-align: center; margin-top: auto; }
.cd-card__stat { font-family: var(--ds-font-display); font-size: var(--ds-text-5xl);
                 line-height: 1; margin: 0; color: var(--ds-text);
                 letter-spacing: var(--ds-tracking-tight, -0.02em); font-weight: var(--ds-font-weight-display, 600); }
.cd-card__list { padding: 0 0 0 20px; margin: 0; font-family: var(--ds-font-body);
                 font-size: var(--ds-text-sm); color: var(--ds-text); }
.cd-card__list li { padding: 4px 0;
                    border-bottom: 1px solid var(--ds-hairline); }
.cd-card__list li:last-child { border-bottom: 0; }
"""

# ----------------------------------------------------------------------
# Layout templates compose whole-bodies in sequence.
# `compose.py` reads the section_sequence list from the layout's
# SectionOutline and concatenates the matching whole bodies/styles.
# ----------------------------------------------------------------------


class TemplateBundle(TypedDict):
    """A class's HTML body + CSS styles + the placeholder names they accept."""
    body: str
    styles: str
    placeholders: tuple[str, ...]


# Placeholder lists must stay in sync with the template strings.
# Tests in test_templates.py enforce this.

ALPHABET_PLACEHOLDERS = (
    "kicker", "display_headline", "dek",
    "display_sample", "display_sample_2", "h2_sample", "h3_sample",
    "lead_sample", "body_sample", "dek_sample", "small_sample",
    "footnote_sample", "kicker_sample", "nav_link_sample",
    "button_sample", "mono_sample", "wordmark_sample",
)

LIBRARY_PLACEHOLDERS = (
    "kicker", "title", "dek",
    "button_primary", "button_ghost",
    "card_title", "card_body", "card_link",
    "nav_link", "wordmark",
    "recommended_label", "tier_name", "tier_price", "tier_dek",
    "testimonial_quote", "testimonial_author", "testimonial_role",
    "kicker_sample",
)

HERO_PLACEHOLDERS = ("kicker", "headline", "dek", "cta_primary", "cta_secondary")
NAV_PLACEHOLDERS = ("wordmark", "link_1", "link_2", "link_3", "link_4", "signin", "signup")
FOOTER_PLACEHOLDERS = (
    "wordmark", "tagline",
    "col_1_title", "col_1_link_1", "col_1_link_2", "col_1_link_3",
    "col_2_title", "col_2_link_1", "col_2_link_2", "col_2_link_3",
    "col_3_title", "col_3_link_1", "col_3_link_2", "col_3_link_3",
    "copyright_line",
)
FEATURE_GRID_PLACEHOLDERS = (
    "kicker", "title", "dek",
    "tile_1_title", "tile_1_dek", "tile_1_link",
    "tile_2_title", "tile_2_dek", "tile_2_link",
    "tile_3_title", "tile_3_dek", "tile_3_link",
    "tile_4_title", "tile_4_dek", "tile_4_link",
    "tile_5_title", "tile_5_dek", "tile_5_link",
    "tile_6_title", "tile_6_dek", "tile_6_link",
)
CTA_BLOCK_PLACEHOLDERS = ("kicker", "title", "dek", "cta_primary", "cta_secondary")
PRICING_TABLE_PLACEHOLDERS = (
    "kicker", "title", "recommended_label",
    "tier_1_name", "tier_1_price", "tier_1_dek", "tier_1_cta",
    "tier_2_name", "tier_2_price", "tier_2_dek", "tier_2_cta",
    "tier_3_name", "tier_3_price", "tier_3_dek", "tier_3_cta",
)
TESTIMONIALS_PLACEHOLDERS = (
    "kicker", "title",
    "quote_1", "author_1", "role_1",
    "quote_2", "author_2", "role_2",
    "quote_3", "author_3", "role_3",
)
PROCESS_STEPS_PLACEHOLDERS = (
    "kicker", "title",
    "step_1_title", "step_1_dek",
    "step_2_title", "step_2_dek",
    "step_3_title", "step_3_dek",
    "step_4_title", "step_4_dek",
)
ARTICLE_LAYOUT_PLACEHOLDERS = (
    "kicker", "title", "dek",
    "lead", "section_2_title", "section_2_body",
    "pull_quote", "section_3_title", "section_3_body",
)
ABOUT_TEAM_PLACEHOLDERS = (
    "kicker", "title", "dek",
    "member_1_name", "member_1_role",
    "member_2_name", "member_2_role",
    "member_3_name", "member_3_role",
    "member_4_name", "member_4_role",
)
NEWS_LIST_PLACEHOLDERS = (
    "kicker", "title",
    "item_1_date", "item_1_title", "item_1_dek",
    "item_2_date", "item_2_title", "item_2_dek",
    "item_3_date", "item_3_title", "item_3_dek",
    "item_4_date", "item_4_title", "item_4_dek",
)

BUTTONS_PLACEHOLDERS = (
    "kicker", "title", "dek",
    "label_primary", "label_secondary", "label_outline",
    "label_ghost", "label_destructive",
    "label_sm", "label_md", "label_lg",
    "label_icon_leading", "label_icon_trailing",
    "label_disabled", "label_disabled_outline",
)

FORM_FIELDS_PLACEHOLDERS = (
    "kicker", "title", "dek",
    "legend_text",
    "label_text", "placeholder_text", "help_text",
    "label_textarea", "placeholder_textarea",
    "label_select", "option_1", "option_2", "option_3",
    "label_checkbox",
    "radio_group_label", "radio_1", "radio_2", "radio_3",
    "label_date", "label_file",
    "label_error", "error_text",
)

INPUTS_PLACEHOLDERS = (
    "kicker", "title", "dek",
    "search_label", "search_placeholder",
    "tags_label", "tag_1", "tag_2", "tag_3", "tags_placeholder",
    "segmented_label", "seg_1", "seg_2", "seg_3",
    "toggle_label",
    "stepper_label", "stepper_value",
)

BADGES_PLACEHOLDERS = (
    "kicker", "title", "dek",
    "label_info", "label_success", "label_warning",
    "label_error", "label_neutral",
    "label_sm", "label_md", "label_lg",
    "label_with_icon_1", "label_with_icon_2",
    "label_online", "label_away", "label_offline",
)

CARDS_PLACEHOLDERS = (
    "kicker", "title", "dek",
    "basic_title", "basic_body", "basic_link",
    "image_title", "image_body",
    "quote_text", "quote_author", "quote_role",
    "pricing_eyebrow", "pricing_tier", "pricing_amount",
    "pricing_period", "pricing_dek", "pricing_cta",
    "stat_label", "stat_value", "stat_dek",
    "list_title", "list_item_1", "list_item_2", "list_item_3",
)


TEMPLATES_BY_CLASS: dict[str, TemplateBundle] = {
    "alphabet": {"body": ALPHABET_BODY, "styles": ALPHABET_STYLES,
                 "placeholders": ALPHABET_PLACEHOLDERS},
    "library": {"body": LIBRARY_BODY, "styles": LIBRARY_STYLES,
                "placeholders": LIBRARY_PLACEHOLDERS},
    "hero": {"body": HERO_BODY, "styles": HERO_STYLES,
             "placeholders": HERO_PLACEHOLDERS},
    "navigation": {"body": NAV_BODY, "styles": NAV_STYLES,
                   "placeholders": NAV_PLACEHOLDERS},
    "footer": {"body": FOOTER_BODY, "styles": FOOTER_STYLES,
               "placeholders": FOOTER_PLACEHOLDERS},
    "feature-grid": {"body": FEATURE_GRID_BODY, "styles": FEATURE_GRID_STYLES,
                     "placeholders": FEATURE_GRID_PLACEHOLDERS},
    "cta-block": {"body": CTA_BLOCK_BODY, "styles": CTA_BLOCK_STYLES,
                  "placeholders": CTA_BLOCK_PLACEHOLDERS},
    "pricing-table": {"body": PRICING_TABLE_BODY, "styles": PRICING_TABLE_STYLES,
                      "placeholders": PRICING_TABLE_PLACEHOLDERS},
    "testimonials": {"body": TESTIMONIALS_BODY, "styles": TESTIMONIALS_STYLES,
                     "placeholders": TESTIMONIALS_PLACEHOLDERS},
    "process-steps": {"body": PROCESS_STEPS_BODY, "styles": PROCESS_STEPS_STYLES,
                      "placeholders": PROCESS_STEPS_PLACEHOLDERS},
    "article-layout": {"body": ARTICLE_LAYOUT_BODY, "styles": ARTICLE_LAYOUT_STYLES,
                       "placeholders": ARTICLE_LAYOUT_PLACEHOLDERS},
    "about-team": {"body": ABOUT_TEAM_BODY, "styles": ABOUT_TEAM_STYLES,
                   "placeholders": ABOUT_TEAM_PLACEHOLDERS},
    "news-list": {"body": NEWS_LIST_BODY, "styles": NEWS_LIST_STYLES,
                  "placeholders": NEWS_LIST_PLACEHOLDERS},
    # Component-library categories (Resemblio Library v1.1, D3).
    "buttons": {"body": BUTTONS_BODY, "styles": BUTTONS_STYLES,
                "placeholders": BUTTONS_PLACEHOLDERS},
    "form-fields": {"body": FORM_FIELDS_BODY, "styles": FORM_FIELDS_STYLES,
                    "placeholders": FORM_FIELDS_PLACEHOLDERS},
    "inputs": {"body": INPUTS_BODY, "styles": INPUTS_STYLES,
               "placeholders": INPUTS_PLACEHOLDERS},
    "badges": {"body": BADGES_BODY, "styles": BADGES_STYLES,
               "placeholders": BADGES_PLACEHOLDERS},
    "cards": {"body": CARDS_BODY, "styles": CARDS_STYLES,
              "placeholders": CARDS_PLACEHOLDERS},
}
"""Master template registry. Each class points to its body, styles, and placeholders."""


def get_template(class_name: str) -> TemplateBundle:
    """Look up a template bundle, raise ValueError if unknown."""
    t = TEMPLATES_BY_CLASS.get(class_name)
    if t is None:
        raise ValueError(
            f"no template for class '{class_name}'. "
            f"Known: {sorted(TEMPLATES_BY_CLASS)}"
        )
    return t
