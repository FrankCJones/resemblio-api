"""Mechanically assemble asset folders from an ExtractionRecord.

The deterministic counterpart to `_scripts/extraction.py`. Reads a validated
`ExtractionRecord` from `_extractions/<slug>/extraction.json` and emits the
full on-disk asset corpus: asset.html, tokens.css, meta.json, README.md per
section, in the canonical layout under `assets/`.

## Why this is the load-bearing module

Today every sub-agent does extraction + assembly in one prompt. Most of
the token spend is on the assembly side — rendering tokens.css from a
TokenSet, building a hero specimen page, writing meta.json boilerplate,
authoring a README. None of that requires LLM judgment once the extraction
is done.

This module turns those mechanical steps into Python templating. The result:
- Extraction agents return small structured JSON, not 4 files.
- Assembly is reproducible (same record → byte-identical output).
- Templates can be improved in one place and every asset re-renders.

## Inputs

- `_extractions/<slug>/extraction.json` — validated `ExtractionRecord`.
- `systems/<slug>/system.json` — for system metadata used in meta.json
  (`source`, `category`, `tier`, etc.).
- Template registry in `_scripts/templates.py`.

## Outputs

For each section in the extraction:
- `assets/<kind>s/<class>/<slug>/<asset_slug>/asset.html`
  (or `assets/<kind>s/<asset_slug>/asset.html` for alphabets/libraries/layouts
  which have a one-level folder structure)
- ... tokens.css
- ... meta.json
- ... README.md

Plus an `_extractions/<slug>/compose_report.json` summarizing what was
written.

## Run command

    python -m _scripts.compose <system-slug>       # full compose pass
    python -m _scripts.compose <system-slug> --dry  # validate only, no writes
    python -m _scripts.compose <system-slug> --section hero  # one section
    python -m _scripts.test_compose                 # unit tests

Throwaway: no. Quality floor applies.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import TypedDict

from _scripts import extraction as ex
from _scripts import templates as tpl
from _scripts.slate import WHOLE_CLASS_FOLDERS, LAYOUT_PAGE_TYPES, WHOLE_CLASSES

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""Repo root; everything resolves relative to here."""

ASSETS_ROOT = PROJECT_ROOT / "assets"
"""Where composed asset folders are written."""

SYSTEMS_ROOT = PROJECT_ROOT / "systems"

SCHEMA_VERSION = 1
"""Bump on incompatible ComposeReport shape changes."""

# Default lorem-friendly content samples per class. Used when an extraction
# record's content_samples dict is missing a placeholder. Lorem-stable so
# composed output is deterministic across runs.
DEFAULT_CONTENT: dict[str, str] = {
    # Generic
    "kicker": "New",
    "title": "Lorem ipsum dolor sit amet",
    "headline": "Lorem ipsum dolor sit amet",
    "dek": "Consectetur adipiscing elit, sed do eiusmod tempor incididunt.",
    "wordmark": "Wordmark",
    "tagline": "Tagline goes here",
    "cta_primary": "Get started",
    "cta_secondary": "Learn more",
    "signin": "Sign in",
    "signup": "Sign up",
    "recommended_label": "Most popular",
    "copyright_line": "© 2026 Lorem Ipsum",
    # Alphabet specimen samples
    "display_headline": "Display headline",
    "display_sample": "Lorem ipsum dolor sit amet",
    "display_sample_2": "Consectetur adipiscing",
    "h2_sample": "Section heading",
    "h3_sample": "Subsection heading",
    "lead_sample": "Lead paragraph; longer than body, sets the tone.",
    "body_sample": ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
                    "Vivamus lacinia odio vitae vestibulum."),
    "dek_sample": "Dek paragraph; muted body weight, magazine register.",
    "small_sample": "Small text; metadata or secondary.",
    "footnote_sample": "1. Footnote; smallest reading size.",
    "kicker_sample": "Eyebrow kicker",
    "nav_link_sample": "Nav link",
    "button_sample": "Button label",
    "mono_sample": "code_sample_here",
    "wordmark_sample": "wordmark",
    # buttons
    "label_primary": "Primary", "label_secondary": "Secondary",
    "label_outline": "Outline", "label_ghost": "Ghost",
    "label_destructive": "Delete",
    "label_sm": "Small", "label_md": "Medium", "label_lg": "Large",
    "label_icon_leading": "New item", "label_icon_trailing": "Continue",
    "label_disabled": "Disabled", "label_disabled_outline": "Disabled",
    # form-fields
    "legend_text": "Account details",
    "label_text": "Full name", "placeholder_text": "Lorem ipsum",
    "help_text": "Lorem ipsum dolor sit amet.",
    "label_textarea": "Notes",
    "placeholder_textarea": "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
    "label_select": "Country",
    "option_1": "Option one", "option_2": "Option two", "option_3": "Option three",
    "label_checkbox": "Subscribe to updates",
    "radio_group_label": "Preferred contact",
    "radio_1": "Email", "radio_2": "Phone", "radio_3": "Mail",
    "label_date": "Start date",
    "label_file": "Upload document",
    "label_error": "Email address",
    "error_text": "Enter a valid email address.",
    # inputs
    "search_label": "Search the library",
    "search_placeholder": "Search components, tokens, sources",
    "tags_label": "Selected tags",
    "tag_1": "design", "tag_2": "tokens", "tag_3": "components",
    "tags_placeholder": "Add tag",
    "segmented_label": "View mode",
    "seg_1": "Grid", "seg_2": "List", "seg_3": "Table",
    "toggle_label": "Enable notifications",
    "stepper_label": "Quantity", "stepper_value": "3",
    # badges
    "label_info": "Info", "label_success": "Live",
    "label_warning": "Beta", "label_error": "Error", "label_neutral": "Draft",
    "label_with_icon_1": "Online", "label_with_icon_2": "Pending",
    "label_online": "Online", "label_away": "Away", "label_offline": "Offline",
    # cards
    "basic_title": "Lorem ipsum",
    "basic_body": "Consectetur adipiscing elit, sed do eiusmod tempor.",
    "basic_link": "Learn more",
    "image_title": "Featured asset",
    "image_body": "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
    "quote_text": "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
    "quote_author": "Jane Doe", "quote_role": "Design Lead",
    "pricing_eyebrow": "Studio",
    "pricing_tier": "Studio plan",
    "pricing_amount": "$49", "pricing_period": "/month",
    "pricing_dek": "Everything in Solo plus 100 extractions per month.",
    "pricing_cta": "Get started",
    "stat_label": "Active users", "stat_value": "12,480",
    "stat_dek": "Up 18 percent from last month.",
    "list_title": "Recent activity",
    "list_item_1": "Lorem ipsum dolor sit amet",
    "list_item_2": "Consectetur adipiscing elit",
    "list_item_3": "Sed do eiusmod tempor incididunt",
}


# ----------------------------------------------------------------------
# Type contracts
# ----------------------------------------------------------------------


class ComposedAsset(TypedDict):
    """One asset folder's worth of generated output (in-memory, pre-write)."""
    class_name: str
    asset_slug: str
    folder_path: str           # repo-relative
    asset_html: str
    tokens_css: str
    meta_json: dict
    readme_md: str


class ComposeReport(TypedDict):
    """Summary of one compose pass. Written to compose_report.json."""
    schema_version: int
    system_slug: str
    composed_at: str
    composed: list[str]        # class names successfully composed
    skipped_no_template: list[str]   # extraction had a section, no template exists
    skipped_in_extraction: list[str]  # extraction.skips entries
    errors: list[str]


# ----------------------------------------------------------------------
# Tokens.css rendering
# ----------------------------------------------------------------------


def tokens_to_css(tokens: dict, *, slug: str, system_name: str,
                  captured: str, extraction_method: str,
                  provenance_score: str, primary_url: str) -> str:
    """Render a TokenSet into a TOKEN_CONTRACT-conformant tokens.css.

    Outputs every key in the contract in canonical order. Keys missing from
    the input fall back to documented defaults (none, transparent, system
    fallback fonts) so the asset still validates against the contract.
    """
    # Canonical key order matches TOKEN_CONTRACT.md.
    colors = [
        "bg", "surface", "surface_2", "text", "text_muted", "text_strong",
        "border", "hairline", "accent", "accent_2",
        "success", "warning", "error", "info", "focus_ring",
    ]
    families = ["font_display", "font_body", "font_mono", "font_accent"]
    sizes = [
        "text_2xs", "text_xs", "text_sm", "text_base", "text_lg",
        "text_xl", "text_2xl", "text_3xl", "text_4xl",
        "text_5xl", "text_6xl", "text_7xl",
    ]
    leadings = ["leading_tight", "leading_snug", "leading_normal",
                "leading_relaxed", "leading_loose"]
    trackings = ["tracking_tight", "tracking_normal",
                 "tracking_wide", "tracking_wider"]
    spacings = ["space_0", "space_1", "space_2", "space_3", "space_4",
                "space_5", "space_6", "space_8", "space_10",
                "space_12", "space_16", "space_32"]
    radii = ["radius_none", "radius_xs", "radius_sm", "radius_md",
             "radius_lg", "radius_full"]
    shadows = ["shadow_none", "shadow_xs", "shadow_sm", "shadow_md",
               "shadow_lg", "shadow_2xl"]
    motions = ["ease_standard", "ease_emphasize", "ease_decelerate",
               "ease_accelerate", "duration_instant", "duration_fast",
               "duration_normal", "duration_slow"]

    def render_group(label: str, keys: list[str]) -> list[str]:
        lines = [f"  /* {label} */"]
        for key in keys:
            css_key = "--ds-" + key.replace("_", "-")
            value = tokens.get(key, _default_for(key))
            lines.append(f"  {css_key}: {value};")
        return lines

    lines: list[str] = []
    lines.extend(render_group("Colors", colors))
    lines.append("")
    lines.extend(render_group("Type families", families))
    lines.append("")
    lines.extend(render_group("Type sizes", sizes))
    lines.append("")
    lines.extend(render_group("Line heights", leadings))
    lines.append("")
    lines.extend(render_group("Tracking", trackings))
    lines.append("")
    lines.extend(render_group("Spacing", spacings))
    lines.append("")
    lines.extend(render_group("Radii", radii))
    lines.append("")
    lines.extend(render_group("Shadows", shadows))
    lines.append("")
    lines.extend(render_group("Motion", motions))

    return tpl.TOKENS_CSS_TEMPLATE.format(
        slug=slug,
        system_name=system_name,
        captured=captured,
        extraction_method=extraction_method,
        provenance_score=provenance_score,
        primary_url=primary_url,
        tokens_body="\n".join(lines),
    )


def _default_for(key: str) -> str:
    """Sensible defaults for token slots an extraction omitted.

    These are deliberately conservative — they let the template render
    without breaking, but a missing token is a real gap that the
    extraction agent should fill in the next pass.
    """
    defaults = {
        "surface_2": "var(--ds-surface)",
        "text_strong": "var(--ds-text)",
        "accent_2": "var(--ds-accent)",
        "success": "#0F8060",
        "warning": "#B88E3A",
        "error": "#A14438",
        "info": "var(--ds-accent)",
        "focus_ring": "var(--ds-accent)",
        "font_accent": "var(--ds-font-display)",
        "text_2xs": "10px", "text_xs": "12px", "text_sm": "14px",
        "text_5xl": "72px", "text_6xl": "88px", "text_7xl": "120px",
        "leading_snug": "1.2", "leading_relaxed": "1.65", "leading_loose": "1.85",
        "tracking_tight": "-0.02em", "tracking_normal": "0",
        "tracking_wide": "0.04em", "tracking_wider": "0.12em",
        "space_0": "0", "space_8": "48px", "space_10": "64px",
        "space_12": "96px", "space_16": "128px", "space_32": "192px",
        "radius_none": "0", "radius_xs": "2px", "radius_lg": "16px",
        "radius_full": "9999px",
        "shadow_none": "none",
        "shadow_xs": "0 1px 1px rgba(0,0,0,0.04)",
        "shadow_sm": "0 1px 3px rgba(0,0,0,0.08)",
        "shadow_md": "0 4px 12px rgba(0,0,0,0.10)",
        "shadow_lg": "0 12px 32px rgba(0,0,0,0.14)",
        "shadow_2xl": "0 24px 56px rgba(0,0,0,0.20)",
        "ease_standard": "cubic-bezier(0.4, 0, 0.2, 1)",
        "ease_emphasize": "cubic-bezier(0.2, 0, 0, 1)",
        "ease_decelerate": "cubic-bezier(0, 0, 0.2, 1)",
        "ease_accelerate": "cubic-bezier(0.4, 0, 1, 1)",
        "duration_instant": "80ms", "duration_fast": "150ms",
        "duration_normal": "240ms", "duration_slow": "400ms",
    }
    return defaults.get(key, "/* unset */")


# ----------------------------------------------------------------------
# HTML rendering
# ----------------------------------------------------------------------


def render_html(*, slug: str, class_folder: str, section: dict,
                template_class: str) -> str:
    """Compose asset.html from a section outline + a template lookup.

    For layouts, the `template_class` is the page-type's section_sequence
    composed inline — handled by `render_layout_html`. For single-class
    assets (alphabet, library, wholes), it's the class name itself.
    """
    bundle = tpl.get_template(template_class)
    content_samples = section.get("content_samples") or {}
    filled = {ph: content_samples.get(ph, DEFAULT_CONTENT.get(ph, "Lorem"))
              for ph in bundle["placeholders"]}
    body = bundle["body"].format(**filled)
    styles = bundle["styles"]
    return _render_skeleton(slug=slug, class_folder=class_folder,
                            section=section, body=body, styles=styles)


def render_layout_html(*, slug: str, layout_section: dict) -> str:
    """Compose a layout asset.html by concatenating section-sequence whole bodies.

    The layout's SectionOutline carries a `section_sequence` list naming
    the wholes to render in order. Each name must match a key in
    `templates.TEMPLATES_BY_CLASS`. Bodies + styles are concatenated.
    """
    sequence = layout_section.get("section_sequence") or []
    if not sequence:
        raise ValueError(
            f"layout {slug}: section_sequence is empty; cannot compose"
        )

    bodies: list[str] = []
    style_set: list[str] = []
    seen_styles: set[str] = set()  # dedupe styles when same class appears twice

    for class_name in sequence:
        if class_name not in tpl.TEMPLATES_BY_CLASS:
            # Unknown class — keep going but log a comment so the gap is visible.
            bodies.append(f"<!-- TODO: no template for section '{class_name}' -->\n")
            continue
        bundle = tpl.get_template(class_name)
        placeholders = {ph: DEFAULT_CONTENT.get(ph, "Lorem")
                        for ph in bundle["placeholders"]}
        bodies.append(bundle["body"].format(**placeholders))
        if class_name not in seen_styles:
            style_set.append(bundle["styles"])
            seen_styles.add(class_name)

    body = "\n".join(bodies)
    styles = "\n".join(style_set)
    return _render_skeleton(slug=slug, class_folder="layouts",
                            section=layout_section, body=body, styles=styles)


def _render_skeleton(*, slug: str, class_folder: str, section: dict,
                     body: str, styles: str) -> str:
    """Wrap a body + styles in the standard asset.html skeleton.

    Adds the required header comment block per SCHEMA.md.
    """
    pattern_tags = section.get("pattern_tags") or [section.get("variant", "")]
    pattern_str = ", ".join(p for p in pattern_tags if p) or "(none)"
    inspired = section.get("inspired_by") or []
    inspired_summary = ", ".join(
        f"{e.get('site', 'unknown')} ({e.get('url', '')})"
        for e in inspired
    ) or "original"
    notes = section.get("notes") or []
    notes_block = "\n".join(f"    - {n}" for n in notes) or "    - (no notes)"

    return tpl.HTML_SKELETON.format(
        slug=slug,
        class_folder=class_folder,
        pattern_tags=pattern_str,
        inspired_by_summary=inspired_summary,
        notes_block=notes_block,
        styles=styles,
        body=body,
    )


# ----------------------------------------------------------------------
# meta.json + README rendering (Phase D content, included here for one-stop compose)
# ----------------------------------------------------------------------


def render_meta(*, slug: str, asset_kind: str, asset_class: str,
                section: dict, system_slug: str, captured: str) -> dict:
    """Build a meta.json dict from a SectionOutline.

    See SCHEMA.md for required keys. `asset_class` is the on-disk class
    folder name (plural where applicable); `section.class_name` is the
    logical class (singular). Both are recorded so downstream tooling can
    use either.
    """
    inspired = []
    for entry in section.get("inspired_by") or []:
        inspired.append({
            "site": entry.get("site", ""),
            "url": entry.get("url", ""),
            "captured": entry.get("captured", captured),
            "archive_url": entry.get("archive_url"),
            "element": entry.get("element", ""),
            "extraction_method": entry.get("extraction_method", "webfetch_html_inspection"),
            "provenance_score": entry.get("provenance_score", "B"),
        })
    patterns = list(section.get("pattern_tags") or [])
    mood = list(section.get("mood") or [])
    # `tags` is a flat de-duplicated bag of taxonomy terms used by the index
    # filters. We synthesize it from patterns + mood + applicable_to so the
    # validator's required-keys check passes without the LLM needing to
    # author it separately.
    applicable_to = list(section.get("applicable_to") or [])
    # Include `asset_class` in tags so strict-mode validate's tags-include-class
    # check passes. Asset class is the most reliable filter axis for consumers.
    tags = sorted({asset_class, *patterns, *mood, *applicable_to})
    return {
        "schema_version": 1,
        "kind": asset_kind,
        "class": asset_class,
        "slug": slug,
        "system": system_slug,
        "title": section.get("variant", asset_class).replace("-", " ").title(),
        "tldr": section.get("tldr", "Composed asset; see README.")[:140],
        "mood": mood,
        "patterns": patterns,
        "applicable_to": applicable_to,
        "tags": tags,
        "ages_well": "probable",
        "inspired_by": inspired,
        "composition_atoms": list(section.get("composition_atoms") or []),
        "used_in_wholes": [],
        "captured": captured,
        "last_updated": captured,
        "notes": section.get("notes") or [],
    }


def render_readme(*, slug: str, section: dict, asset_class: str,
                  system_name: str) -> str:
    """Generate the README.md body for an asset.

    Pulled from section tldr, notes, pattern_tags, and inspired_by URLs.
    No LLM call needed — the structured fields contain everything.
    """
    title = section.get("variant", asset_class).replace("-", " ").title()
    tldr = section.get("tldr", "")
    notes = section.get("notes") or []
    patterns = section.get("pattern_tags") or []
    inspired = section.get("inspired_by") or []

    notes_block = (
        "## Design intent\n\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    ) if notes else ""

    patterns_block = (
        "## Patterns\n\n" + ", ".join(f"`{p}`" for p in patterns) + "\n"
    ) if patterns else ""

    inspired_block = ""
    if inspired:
        rows = []
        for e in inspired:
            score = e.get("provenance_score", "?")
            method = e.get("extraction_method", "?")
            url = e.get("url", "")
            rows.append(f"- {url} (score {score}, {method})")
        inspired_block = "## Provenance\n\n" + "\n".join(rows) + "\n"

    return (
        f"# {slug}\n\n"
        f"{tldr}\n\n"
        f"_Generated by `_scripts/compose.py` from a verified ExtractionRecord. "
        f"Source: {system_name}._\n\n"
        f"{notes_block}\n"
        f"{patterns_block}\n"
        f"{inspired_block}"
    )


# ----------------------------------------------------------------------
# Asset path resolution
# ----------------------------------------------------------------------


def resolve_asset_paths(*, system_slug: str, section: dict) -> tuple[str, str, str]:
    """Return (folder_path, asset_slug, asset_class_folder) for a section.

    asset_class_folder is what `class` should be in meta.json (e.g.
    "heroes", "navigation"). asset_slug is the leaf folder name
    (e.g. `acme-hero-001`).
    """
    class_name = section["class_name"]
    kind = section["kind"]

    if kind == "alphabet":
        return (f"assets/alphabets/{system_slug}", system_slug, "alphabet")
    if kind == "library":
        return (f"assets/libraries/{system_slug}", system_slug, "library")
    if kind == "layout":
        page_type = class_name.replace("-page", "")
        slug = f"{system_slug}-{page_type}-page-001"
        return (f"assets/layouts/{slug}", slug, "layout")
    if kind == "whole":
        folder_class = WHOLE_CLASS_FOLDERS.get(class_name, class_name + "s")
        slug = f"{system_slug}-{class_name}-001"
        return (f"assets/wholes/{folder_class}/{slug}", slug, folder_class)
    raise ValueError(f"unknown kind '{kind}' for section '{class_name}'")


# ----------------------------------------------------------------------
# One-shot compose for a section
# ----------------------------------------------------------------------


def compose_section(*, system_slug: str, system_meta: dict,
                    section: dict, captured: str) -> ComposedAsset:
    """Compose one section into a ComposedAsset (in-memory; no write yet).

    Caller writes via `write_composed`. This separation lets `--dry`
    validate the full compose pipeline without touching disk.
    """
    folder_path, asset_slug, asset_class = resolve_asset_paths(
        system_slug=system_slug, section=section
    )
    inspired = section.get("inspired_by") or []
    primary_url = inspired[0].get("url", "") if inspired else ""
    extraction_method = (inspired[0].get("extraction_method", "webfetch_html_inspection")
                         if inspired else "webfetch_html_inspection")
    provenance_score = section.get("provenance_score", "B")
    system_name = system_meta.get("source", system_slug)

    tokens_css = tokens_to_css(
        system_meta.get("_tokens") or {},
        slug=asset_slug, system_name=system_name, captured=captured,
        extraction_method=extraction_method,
        provenance_score=provenance_score, primary_url=primary_url,
    )

    if section["kind"] == "layout":
        asset_html = render_layout_html(slug=asset_slug, layout_section=section)
    elif section["class_name"] in tpl.TEMPLATES_BY_CLASS:
        asset_html = render_html(
            slug=asset_slug, class_folder=asset_class,
            section=section, template_class=section["class_name"],
        )
    else:
        # No matching template — caller (compose_system) handles as skipped_no_template.
        raise KeyError(f"no template for class '{section['class_name']}'")

    meta_json = render_meta(
        slug=asset_slug, asset_kind=section["kind"], asset_class=asset_class,
        section=section, system_slug=system_slug, captured=captured,
    )
    readme_md = render_readme(
        slug=asset_slug, section=section, asset_class=asset_class,
        system_name=system_name,
    )

    return ComposedAsset(
        class_name=section["class_name"],
        asset_slug=asset_slug,
        folder_path=folder_path,
        asset_html=asset_html,
        tokens_css=tokens_css,
        meta_json=meta_json,
        readme_md=readme_md,
    )


def write_composed(asset: ComposedAsset) -> Path:
    """Write a ComposedAsset to disk. Returns the folder Path."""
    folder = PROJECT_ROOT / asset["folder_path"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "asset.html").write_text(asset["asset_html"], encoding="utf-8")
    (folder / "tokens.css").write_text(asset["tokens_css"], encoding="utf-8")
    (folder / "meta.json").write_text(
        json.dumps(asset["meta_json"], indent=2) + "\n", encoding="utf-8"
    )
    (folder / "README.md").write_text(asset["readme_md"], encoding="utf-8")
    return folder


# ----------------------------------------------------------------------
# System-wide compose orchestration
# ----------------------------------------------------------------------


def compose_system(slug: str, *, dry_run: bool = False,
                   section_filter: str | None = None) -> ComposeReport:
    """Compose every section in the extraction for one system.

    Reads `_extractions/<slug>/extraction.json`, validates it, walks each
    section, composes + writes. Returns a ComposeReport summarizing what
    happened.

    `section_filter`: if set, only compose that one class_name (useful for
    targeted re-compose during template development).
    """
    record = ex.read_extraction(slug)
    sys_manifest = _load_system_manifest(slug)
    # Stash the tokens on the manifest object so compose_section can read them
    # without an extra argument. Keeps the function signature small.
    sys_manifest["_tokens"] = record["tokens"]
    captured = record.get("captured") or dt.date.today().isoformat()

    composed: list[str] = []
    skipped_no_template: list[str] = []
    errors: list[str] = []

    for class_name, section in record["sections"].items():
        if section_filter and class_name != section_filter:
            continue
        try:
            asset = compose_section(
                system_slug=slug, system_meta=sys_manifest,
                section=section, captured=captured,
            )
        except KeyError as e:
            skipped_no_template.append(class_name)
            continue
        except Exception as e:
            errors.append(f"{class_name}: {e!r}")
            continue
        if not dry_run:
            write_composed(asset)
        composed.append(class_name)

    skipped_in_extraction = [s["class_name"] for s in record.get("skips", [])]

    report = ComposeReport(
        schema_version=SCHEMA_VERSION,
        system_slug=slug,
        composed_at=dt.datetime.now().isoformat(timespec="seconds"),
        composed=composed,
        skipped_no_template=skipped_no_template,
        skipped_in_extraction=skipped_in_extraction,
        errors=errors,
    )

    if not dry_run:
        out = ex.EXTRACTIONS_ROOT / slug / "compose_report.json"
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    return report


def _load_system_manifest(slug: str) -> dict:
    """Load the system's manifest. Returns an empty dict if missing
    (compose still works against the extraction's own tokens)."""
    path = SYSTEMS_ROOT / slug / "system.json"
    if not path.exists():
        return {"source": slug, "category": "", "tier": None}
    return json.loads(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Returns shell exit code."""
    ap = argparse.ArgumentParser(
        description="Compose asset folders from a validated ExtractionRecord.",
    )
    ap.add_argument("slug", help="System slug (e.g. anthropic).")
    ap.add_argument("--dry", action="store_true",
                    help="Validate + render to memory; do not write to disk.")
    ap.add_argument("--section", help="Only compose this single class name.")
    args = ap.parse_args(argv)

    try:
        report = compose_system(
            args.slug, dry_run=args.dry, section_filter=args.section
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ex.ExtractionValidationError as e:
        print(f"extraction invalid: {e}", file=sys.stderr)
        return 2

    print(f"Composed {len(report['composed'])} section(s) for {args.slug}.")
    if report["skipped_no_template"]:
        print(f"  no template: {report['skipped_no_template']}")
    if report["skipped_in_extraction"]:
        print(f"  extraction skipped: {report['skipped_in_extraction']}")
    if report["errors"]:
        print(f"  errors: {report['errors']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
