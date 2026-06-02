"""Generate the prompt slate for one /dl system wave.

A "slate" is the set of 17 sub-agent prompts (1 alphabet + 1 library +
6 layouts + 9 wholes) that a /dl system wave dispatches. Writing those
prompts by hand burns orchestrator tokens and introduces drift between
waves. This script generates them mechanically from a system's seed
manifest plus a single canonical prompt template.

## Inputs

- `systems/<slug>/system.json` — the seed manifest. Must already exist
  on disk with at least `source`, `url`, `category`, `design_principles`,
  and a `completeness_checklist` block (the slate is derived from
  applicable items in the checklist).

## Outputs

- `_slates/<slug>/<class>.prompt.md` — one prompt file per applicable
  class, ready to copy into an Agent invocation.
- `_slates/<slug>/MANIFEST.json` — index of the slate (class list,
  expected output paths, prompt-file paths, schema_version).

## Behavior

Items in the checklist marked `not-applicable` are skipped (no prompt
generated). Items marked `present` get a "retry only if folder missing"
prompt. Items marked `missing` get the canonical first-attempt prompt.

The script is idempotent: re-running overwrites the slate without
side effects elsewhere.

## Constraints

- Never authors source content (no copy generation here).
- Never spawns agents (this script generates prompt text only).
- Output dir `_slates/` is git-ignored by convention; treat as cache.

## Run command

    python -m _scripts.slate <system-slug>
    python -m _scripts.slate <system-slug> --force  # overwrite even if up-to-date
    python -m _scripts.slate --list                  # list available seeded systems

Throwaway: no. Quality floor applies.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TypedDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""Repo root; everything resolves relative to here."""

SYSTEMS_ROOT = PROJECT_ROOT / "systems"
"""Where seeded system manifests live."""

SLATES_ROOT = PROJECT_ROOT / "_slates"
"""Where generated prompts land. One subdirectory per system slug."""

TEMPLATES_ROOT = PROJECT_ROOT / "_templates"
"""Where the prompt template lives (`agent_prompt.md.tmpl`)."""

SCHEMA_VERSION = 1
"""Bump when SlateManifest shape changes incompatibly."""

# The 7 page-types the completeness_checklist tracks under `layouts`.
LAYOUT_PAGE_TYPES: tuple[str, ...] = (
    "marketing", "about", "pricing", "customer-story",
    "article", "docs", "research",
)
"""Canonical layout slots in completeness_checklist. Stable across systems."""

# The whole-classes the completeness_checklist tracks under `wholes`.
# 11 page-section wholes + 5 component-library categories
# (Resemblio Library v1.1, D3: buttons / form-fields / inputs / badges / cards
# are URL-categorized standalone showcase pages).
WHOLE_CLASSES: tuple[str, ...] = (
    "hero", "navigation", "footer", "feature-grid", "cta-block",
    "pricing-table", "testimonials", "process-steps", "article-layout",
    "about-team", "news-list",
    "buttons", "form-fields", "inputs", "badges", "cards",
)
"""Canonical whole classes in completeness_checklist. Stable across systems."""

# Reference assets a sub-agent can read to anchor its extraction shape.
# Picked because they're known tier-A, well-validated, and span categories.
CLASS_REFERENCE_ASSETS: dict[str, str] = {
    "alphabet": "assets/alphabets/vercel/",
    "library": "assets/libraries/vercel/",
    "marketing-page": "assets/layouts/vercel-marketing-page-001/",
    "about-page": "assets/layouts/vercel-about-page-001/",
    "pricing-page": "assets/layouts/vercel-pricing-page-001/",
    "customer-story-page": "assets/layouts/vercel-customer-story-page-001/",
    "article-page": "assets/layouts/vercel-blog-page-001/",
    "docs-page": "assets/layouts/vercel-docs-page-001/",
    "research-page": "assets/layouts/anthropic-research-page-001/",
    "hero": "assets/wholes/heroes/vercel-hero-001/",
    "navigation": "assets/wholes/navigation/vercel-nav-001/",
    "footer": "assets/wholes/footers/dev-tool-status-footer-001/",
    "feature-grid": "assets/wholes/feature-grids/bento-asymmetric-grid-001/",
    "cta-block": "assets/wholes/cta-blocks/vercel-cta-block-001/",
    "pricing-table": "assets/wholes/pricing-tables/comparison-detailed-pricing-001/",
    "testimonials": "assets/wholes/testimonials/vercel-testimonials-001/",
    "process-steps": "assets/wholes/process-steps/locomotive-case-walkthrough-001/",
    "article-layout": "assets/wholes/article-layouts/vercel-article-layout-001/",
    "about-team": "assets/wholes/about-team/vercel-about-team-001/",
    "news-list": "assets/wholes/article-layouts/quanta-science-article-001/",
}
"""Reference asset path per class. Used in prompt templates as the shape anchor."""


# ----------------------------------------------------------------------
# Type contracts
# ----------------------------------------------------------------------


class SlateItem(TypedDict):
    """One row of the slate: a single sub-agent prompt assignment."""
    class_name: str          # e.g. "alphabet", "hero", "marketing-page"
    kind: str                # "alphabet" | "library" | "layout" | "whole"
    asset_folder: str        # repo-relative path the agent should write to
    prompt_path: str         # repo-relative path of the generated prompt file
    status: str              # "missing" (build), "present" (retry-if-empty), "not-applicable"


class SlateManifest(TypedDict):
    """The complete slate index for one system."""
    schema_version: int
    system_slug: str
    system_name: str
    system_url: str
    category: str
    design_principles: list[str]
    items: list[SlateItem]


# ----------------------------------------------------------------------
# Template (kept inline so the slate generator stays single-file)
# ----------------------------------------------------------------------

PROMPT_TEMPLATE = """\
Single source: {system_name} ({system_url}). {class_label} extraction.

**Working directory:** C:\\Users\\fjone\\Desktop\\Shared with Claude\\projects\\Design Reference Library

**System slug:** `{system_slug}`
**Category:** {category}

**Read first:**
- `_docs/AGENT_BRIEFING.md` (procedure)
- `_docs/PROVENANCE_RUBRIC.md` (scoring)
- `SCHEMA.md` (meta.json shape)
- `TOKEN_CONTRACT.md` (required CSS variable slots)
- `TAXONOMY.md` (controlled vocabulary)
- `systems/{system_slug}/system.json` (design principles for this system)
- `{reference_asset}` (structural reference for this class)

**Design principles to honor:**
{design_principles_bulleted}

**Task:** {task_description}

**Output folder:** `{asset_folder}`
**Output files:** asset.html, tokens.css, meta.json, README.md.

**Provenance order (per `_docs/AGENT_BRIEFING.md`):**
A (Chrome MCP devtools_computed_style) → B (WebFetch html_inspection) → D (Wayback) → skip.
NEVER author from memory. If A → B → D all fail, skip and log to `_INBOX/issues_observed.md`.

**Constraints:**
- Single dashes only; no em-dashes; no double dashes.
- Avoid banned words listed in workspace CLAUDE.md.
- Tag `system: {system_slug}` in meta.json.
- Lorem ipsum for content. Real tokens for design.

Report: score achieved, files written, one-line summary. Under 120 words.
"""

CLASS_TASK_DESCRIPTIONS: dict[str, str] = {
    "alphabet": (
        "Extract {system_name}'s complete type expression. Sample ≥ 2 pages "
        "(homepage plus one inner). Capture display, body, mono utility, button "
        "label, kicker, label, footnote, wordmark. The asset is a single specimen "
        "page that shows every type slot in use."
    ),
    "library": (
        "Extract {system_name}'s marketing-surface component vocabulary. "
        "Sample ≥ 3 pages. Capture: button, card with hairline chrome, nav link, "
        "hero CTA pair, feature tile, pricing tier card, testimonial card, "
        "footer link, wordmark, hairline divider, kicker. The asset is a single "
        "specimen page that demonstrates each component."
    ),
    "marketing-page": (
        "Extract {system_name}'s homepage marketing-page skeleton. URL: "
        "{system_url}. Capture the section sequence: nav, hero, product/feature "
        "sections, customer logos, testimonials, pricing teaser, CTA, footer."
    ),
    "about-page": (
        "Extract {system_name}'s about/company page. Browse from nav for the "
        "canonical path. Capture: nav, mission hero, mission/values, team or "
        "leadership grid, milestones, careers CTA, footer. SKIP if no canonical "
        "about page exists (flip layouts.about: not-applicable)."
    ),
    "pricing-page": (
        "Extract {system_name}'s pricing page. Browse from nav. Capture: nav, "
        "hero, tier cards, feature-comparison table, FAQ, CTA, footer. SKIP "
        "and flip not-applicable if no tier-card pricing exists."
    ),
    "customer-story-page": (
        "Extract {system_name}'s customer-story / case-study page. Pick a "
        "current story from the customers index. Capture: nav, hero (logo + "
        "headline), metric callouts, body narrative with quotes, results, "
        "related stories, CTA, footer. SKIP and flip not-applicable if no "
        "per-customer template exists."
    ),
    "article-page": (
        "Extract {system_name}'s blog/article page. Pick a current post. "
        "Capture: nav, article hero (kicker + title + byline + date + feature "
        "image), narrow body column with inline images and pull quotes, "
        "related posts, newsletter CTA, footer."
    ),
    "docs-page": (
        "Extract {system_name}'s docs / help-center page. Pick a current "
        "lesson or doc article. Capture docs grid: nav, breadcrumb, sidebar "
        "nav, main body (title + body + code blocks), TOC, prev/next pager, "
        "footer. SKIP and flip not-applicable if no docs surface exists."
    ),
    "research-page": (
        "Extract {system_name}'s research / publications page (for sources "
        "that publish original research). Capture: nav, hero, publications "
        "list with date + author + abstract, footer."
    ),
    "hero": (
        "Extract {system_name}'s marketing-page hero. Sample ≥ 2 pages. "
        "Capture: kicker, oversized display headline, dek, CTA pair, optional "
        "visual or screenshot beneath."
    ),
    "navigation": (
        "Extract {system_name}'s marketing top navigation. Sample ≥ 2 pages. "
        "Capture: wordmark, top-level items, search if present, login + signup "
        "CTAs."
    ),
    "footer": (
        "Extract {system_name}'s mega-footer. Sample ≥ 2 pages. Capture: "
        "logomark, column-grouped link lists, social row, status indicator "
        "if present, locale picker if present, copyright + legal line."
    ),
    "feature-grid": (
        "Extract {system_name}'s feature-grid pattern. Sample ≥ 2 pages. "
        "Capture the tile grid: tile with icon/screenshot + heading + dek + "
        "arrow link. Note the grid shape (3-up, 4-up, asymmetric bento)."
    ),
    "cta-block": (
        "Extract {system_name}'s mid- or end-of-page CTA block. Sample ≥ 2 "
        "pages. Capture: kicker, display headline, dek, dual CTA pair, "
        "optional decorative visual."
    ),
    "pricing-table": (
        "Extract {system_name}'s pricing-table whole. Tier names verified "
        "against the live pricing page. Capture tier-card row with "
        "recommended-tier treatment, feature checklists, comparison-row "
        "beneath if present. SKIP and flip not-applicable if pricing is "
        "platform-fee not tiers."
    ),
    "testimonials": (
        "Extract {system_name}'s testimonials block. Sample ≥ 2 pages. "
        "Capture: kicker, multi-up customer-quote cards (avatar + name + role "
        "+ company + quote), customer logo strip if present."
    ),
    "process-steps": (
        "Extract {system_name}'s numbered process-steps / how-it-works block. "
        "Sample ≥ 2 pages. Capture: numbered prefix (01/02/03), step heading, "
        "step dek, optional screenshot per step. SKIP and flip not-applicable "
        "if genuinely absent across the marketing surface (verify on home + "
        "features + product pages)."
    ),
    "article-layout": (
        "Extract {system_name}'s reusable blog/article-layout shell. Sample "
        "≥ 2 distinct posts. Capture only the WHOLE shell (kicker + title + "
        "byline + body column + callout/code-block pattern), not the page "
        "chrome."
    ),
    "about-team": (
        "Extract {system_name}'s about-team / leadership grid. Capture: "
        "avatar + name + role tile, arranged in a grid. SKIP and flip "
        "not-applicable if the source doesn't ship a team grid."
    ),
    "news-list": (
        "Extract {system_name}'s news/blog index (list of recent posts). "
        "Capture: header, filter chips if present, story-card grid or list, "
        "pagination, footer reference. SKIP and flip not-applicable if no "
        "news index exists."
    ),
}
"""Per-class task description fragment. Substituted into PROMPT_TEMPLATE."""


# ----------------------------------------------------------------------
# Slate construction
# ----------------------------------------------------------------------


def load_system_manifest(slug: str) -> dict:
    """Load `systems/<slug>/system.json` and return parsed dict.

    Raises FileNotFoundError if the manifest doesn't exist, ValueError if it
    can't be parsed. Validates that required fields are present so callers
    don't have to.
    """
    manifest_path = SYSTEMS_ROOT / slug / "system.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"system.json missing for {slug}: expected at {manifest_path}"
        )
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"system.json unparseable: {manifest_path}: {e}")

    required = ("source", "url", "category", "completeness_checklist")
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise ValueError(
            f"system.json for {slug} missing required fields: {missing}"
        )
    return data


def build_slate_items(slug: str, manifest: dict) -> list[SlateItem]:
    """Walk the completeness_checklist and produce one SlateItem per slot.

    Returns items for every applicable slot (alphabet, library, every layout
    that isn't not-applicable, every whole that isn't not-applicable). Each
    item carries the status verbatim from the checklist; the prompt template
    uses that to decide whether to phrase the task as "build" or
    "retry-if-empty".
    """
    checklist = manifest.get("completeness_checklist") or {}
    items: list[SlateItem] = []

    # Alphabet (always one slot, no nested dict).
    alpha_status = checklist.get("alphabet", "missing")
    if alpha_status != "not-applicable":
        items.append(SlateItem(
            class_name="alphabet",
            kind="alphabet",
            asset_folder=f"assets/alphabets/{slug}/",
            prompt_path=f"_slates/{slug}/alphabet.prompt.md",
            status=alpha_status,
        ))

    # Library (always one slot).
    lib_status = checklist.get("library", "missing")
    if lib_status != "not-applicable":
        items.append(SlateItem(
            class_name="library",
            kind="library",
            asset_folder=f"assets/libraries/{slug}/",
            prompt_path=f"_slates/{slug}/library.prompt.md",
            status=lib_status,
        ))

    # Layouts (7 page-types).
    layouts_block = checklist.get("layouts") or {}
    for page_type in LAYOUT_PAGE_TYPES:
        status = layouts_block.get(page_type, "missing")
        if status == "not-applicable":
            continue
        class_name = f"{page_type}-page"
        items.append(SlateItem(
            class_name=class_name,
            kind="layout",
            asset_folder=f"assets/layouts/{slug}-{page_type}-page-001/",
            prompt_path=f"_slates/{slug}/{class_name}.prompt.md",
            status=status,
        ))

    # Wholes (11 classes; map class name → folder name).
    wholes_block = checklist.get("wholes") or {}
    for whole_class in WHOLE_CLASSES:
        status = wholes_block.get(whole_class, "missing")
        if status == "not-applicable":
            continue
        # Folder names in assets/wholes/ are plural for most classes.
        folder_class = _whole_class_folder(whole_class)
        items.append(SlateItem(
            class_name=whole_class,
            kind="whole",
            asset_folder=f"assets/wholes/{folder_class}/{slug}-{whole_class}-001/",
            prompt_path=f"_slates/{slug}/{whole_class}.prompt.md",
            status=status,
        ))

    return items


# Map of whole-class names (singular, as used in checklist) to their
# canonical asset-folder names (plural where applicable).
WHOLE_CLASS_FOLDERS: dict[str, str] = {
    "hero": "heroes",
    "navigation": "navigation",
    "footer": "footers",
    "feature-grid": "feature-grids",
    "cta-block": "cta-blocks",
    "pricing-table": "pricing-tables",
    "testimonials": "testimonials",
    "process-steps": "process-steps",
    "article-layout": "article-layouts",
    "about-team": "about-team",
    "news-list": "news-lists",
    # Component-library categories: folder name == class name (already plural).
    "buttons": "buttons",
    "form-fields": "form-fields",
    "inputs": "inputs",
    "badges": "badges",
    "cards": "cards",
}
"""Class name → folder name. Folder names follow TAXONOMY.md."""


def _whole_class_folder(class_name: str) -> str:
    """Resolve a whole-class name to its on-disk folder name."""
    return WHOLE_CLASS_FOLDERS.get(class_name, class_name + "s")


# ----------------------------------------------------------------------
# Prompt rendering
# ----------------------------------------------------------------------


def render_prompt(item: SlateItem, manifest: dict) -> str:
    """Render one prompt file body for a slate item.

    Substitutes the per-class task description and the manifest's
    design_principles into PROMPT_TEMPLATE. The output is plain Markdown,
    ready to copy into an Agent invocation's `prompt` parameter.
    """
    system_slug = _slug_from_source(manifest["source"])
    principles = manifest.get("design_principles") or []
    principles_bulleted = (
        "\n".join(f"- {p}" for p in principles) if principles
        else "- (no design principles yet — agent should infer from sampled pages)"
    )

    class_label = _class_label(item["class_name"])
    task_template = CLASS_TASK_DESCRIPTIONS.get(
        item["class_name"],
        "Extract {system_name}'s " + item["class_name"] + " for the library.",
    )
    task_description = task_template.format(
        system_name=manifest["source"], system_url=manifest["url"]
    )
    if item["status"] == "present":
        task_description = (
            "RETRY-IF-MISSING. Check whether `" + item["asset_folder"] +
            "` already has all four files. If yes, exit without writing. "
            "If incomplete, " + task_description[0].lower() + task_description[1:]
        )

    reference = CLASS_REFERENCE_ASSETS.get(item["class_name"], "(no canonical reference)")

    return PROMPT_TEMPLATE.format(
        system_name=manifest["source"],
        system_url=manifest["url"],
        system_slug=system_slug,
        category=manifest.get("category", "(unset)"),
        class_label=class_label,
        design_principles_bulleted=principles_bulleted,
        task_description=task_description,
        asset_folder=item["asset_folder"],
        reference_asset=reference,
    )


def _slug_from_source(source: str) -> str:
    """Convert a source name into a directory-safe kebab-case slug.

    Mirrors migrate_to_systems.slugify so naming stays consistent.
    """
    import re
    s = re.sub(r"[\(\)]", "", source)
    s = re.sub(r"[\s\.]+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return s.lower().strip("-")


def _class_label(class_name: str) -> str:
    """Human-friendly label for the prompt header (e.g. "Hero", "Article-page")."""
    return class_name.replace("-", " ").title().replace(" ", "-")


# ----------------------------------------------------------------------
# Slate write + read
# ----------------------------------------------------------------------


def write_slate(slug: str, *, force: bool = False) -> SlateManifest:
    """Generate the full slate for one system. Returns the SlateManifest."""
    manifest = load_system_manifest(slug)
    system_slug = _slug_from_source(manifest["source"])
    if system_slug != slug:
        # Caller passed a slug that doesn't match the manifest's source.
        # That's almost always a typo; refuse rather than write to the wrong dir.
        raise ValueError(
            f"slug mismatch: caller gave '{slug}' but manifest source "
            f"'{manifest['source']}' resolves to '{system_slug}'"
        )
    items = build_slate_items(slug, manifest)

    slate_dir = SLATES_ROOT / slug
    if slate_dir.exists() and not force:
        # Caller didn't pass --force; only rewrite if any prompt file is
        # missing or older than the system.json on disk.
        sys_mtime = (SYSTEMS_ROOT / slug / "system.json").stat().st_mtime
        all_fresh = True
        for item in items:
            p = PROJECT_ROOT / item["prompt_path"]
            if not p.exists() or p.stat().st_mtime < sys_mtime:
                all_fresh = False
                break
        if all_fresh:
            # Reload existing manifest, return it without rewriting.
            existing = slate_dir / "MANIFEST.json"
            if existing.exists():
                return json.loads(existing.read_text(encoding="utf-8"))

    slate_dir.mkdir(parents=True, exist_ok=True)
    for item in items:
        body = render_prompt(item, manifest)
        out = PROJECT_ROOT / item["prompt_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")

    slate_manifest = SlateManifest(
        schema_version=SCHEMA_VERSION,
        system_slug=slug,
        system_name=manifest["source"],
        system_url=manifest["url"],
        category=manifest.get("category", ""),
        design_principles=manifest.get("design_principles") or [],
        items=items,
    )
    (slate_dir / "MANIFEST.json").write_text(
        json.dumps(slate_manifest, indent=2) + "\n", encoding="utf-8"
    )
    return slate_manifest


def list_seeded_systems() -> list[str]:
    """Return slugs of every system with a system.json on disk."""
    if not SYSTEMS_ROOT.exists():
        return []
    return sorted(
        d.name for d in SYSTEMS_ROOT.iterdir()
        if d.is_dir() and (d / "system.json").exists()
    )


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Returns shell exit code."""
    ap = argparse.ArgumentParser(
        description="Generate the 17-prompt slate for one /dl system wave.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("slug", nargs="?",
                    help="System slug (e.g. anthropic, vercel, hugging-face).")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite even if prompts appear up-to-date.")
    ap.add_argument("--list", action="store_true", dest="list_mode",
                    help="List every seeded system on disk and exit.")
    args = ap.parse_args(argv)

    if args.list_mode:
        slugs = list_seeded_systems()
        if not slugs:
            print("(no seeded systems found)")
            return 1
        print(f"Seeded systems ({len(slugs)}):")
        for s in slugs:
            print(f"  {s}")
        return 0

    if not args.slug:
        ap.print_help()
        return 1

    try:
        slate = write_slate(args.slug, force=args.force)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    n = len(slate["items"])
    print(f"Wrote slate for {args.slug}: {n} prompts at _slates/{args.slug}/")
    counts = {"missing": 0, "present": 0}
    for item in slate["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    print(f"  missing (build): {counts.get('missing', 0)}")
    print(f"  present (retry-if-empty): {counts.get('present', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
