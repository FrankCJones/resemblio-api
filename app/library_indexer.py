"""Library indexer service: drain library_index_jobs, run compose, write pages.

Mission Phase 4 (``projects/OptSus Team/missions/resemblio-library-v1.1.md``).
A queue-and-worker shape rather than a long-running daemon: the CLI
(``app.cli.library_indexer``) drains up to ``LIBRARY_INDEX_BATCH_SIZE`` jobs
per tick and exits cleanly, fired every 60 seconds by a systemd timer
(``deploy/systemd/resemblio-library-indexer.timer``).

Architecture
------------
- ``enqueue_for_asset_version`` is the trigger-side helper. Called from the
  seed script and the POST /v1/extractions success path when the new asset
  version is eligible to be indexed (``is_public=True`` for the route path).
  Idempotent: if a non-terminal job already exists for the asset_version, no
  duplicate row is inserted.
- ``drain_pending`` is the worker-side entry point. Probes pending jobs,
  flips them to ``running``, calls ``_process_job`` for each, and accumulates
  a structured result the CLI logs.
- ``_process_job`` is the per-job pipeline: load the asset_version, run the
  quality gate, translate DTCG to TokenSet, compose every registered class,
  upsert ``library_pages`` rows, and reconcile ``is_canonical`` flags.

Quality gate (mission D2)
-------------------------
A job is eligible iff (a) ``asset_version.is_public`` is TRUE and (b) the
associated ``extractions`` row (joined via ``asset_version_id``) carries
``quality_score >= LIBRARY_INDEX_QUALITY_THRESHOLD`` AND no penalty flags
fired. Seed rows that bypass the scorer (their ``quality_score`` is NULL)
are eligible — the DRL bootstrap corpus is the curated reference set, not
scorer-gated content.

is_canonical flip
-----------------
After writing per-page rows for an asset_version, the worker sets
``is_canonical=True`` for those rows iff no other asset_version for the same
``brand_slug`` has a later ``fetched_at``. All older versions of the same
``(brand_slug, category_slug)`` are flipped FALSE in the same transaction.
This is the read-path optimization for ``/library/<brand>/<category>/``
(canonical-version page); versioned URLs read every row regardless.

Backoff
-------
Compose failures flip the row back to ``pending``, bump ``attempts``, and
record the exception repr in ``last_error``. Once ``attempts`` reaches
``LIBRARY_INDEX_MAX_ATTEMPTS`` the row is parked at ``failed``. The next
attempt is picked up on the next timer tick; there is no in-process retry
loop (a poison row would otherwise block forward progress).

Schema
------
``metadata_json`` on every ``library_pages`` row carries
``schema_version = LIBRARY_PAGE_METADATA_SCHEMA_VERSION`` so downstream
consumers (Next.js routes, OG-image generator) can detect shape drift.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import (
    LIBRARY_INDEX_BATCH_SIZE,
    LIBRARY_INDEX_MAX_ATTEMPTS,
    LIBRARY_INDEX_QUALITY_THRESHOLD,
    LIBRARY_PAGE_METADATA_SCHEMA_VERSION,
)
# Importing ``app.extractor_bridge`` for its side effects: at module-load
# time it prepends the vendored ``_vendored/drl/drl`` tree onto ``sys.path``
# and verifies the DRL corpus is intact. The indexer's lazy ``from _scripts
# import ...`` calls inside ``_all_template_classes`` and ``_compose_one_page``
# depend on that path setup having already run. Without this import the CLI
# entrypoint (``python -m app.cli.library_indexer``) loads this module but
# never triggers the path install, and every job in the queue fails with
# ``ModuleNotFoundError: No module named '_scripts'``. Do not remove.
from app import extractor_bridge as _extractor_bridge  # noqa: F401
from app.models import AssetVersion, Extraction, LibraryIndexJob, LibraryPage


logger = logging.getLogger("resemblio.library_indexer")


# The DRL ``_scripts`` package (templates, compose, slate, extraction) is
# vendored under ``_vendored/drl/drl/_scripts``. The ``sys.path`` install
# that makes those modules importable is performed by
# ``app.extractor_bridge`` at its module-load time; we import it above
# purely for that side effect so that the indexer's lazy ``from _scripts
# import ...`` calls below resolve correctly when this module is loaded
# from the CLI entrypoint (which does not otherwise import the bridge).
# CI checks out only the resemblio-api repo and must resolve these imports
# against the vendored tree alone; do not reintroduce workspace-relative
# lookups.


# ----------------------------------------------------------------------
# Result types
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class JobOutcome:
    """Per-job result. ``pages_written`` is 0 for quality-gated skips."""

    job_id: int
    asset_version_id: int
    status: str  # "complete" | "failed" | "pending_retry"
    pages_written: int
    reason: str | None = None


@dataclass(frozen=True)
class DrainResult:
    """Aggregate result of one ``drain_pending`` call."""

    jobs_run: int
    pages_written: int
    outcomes: tuple[JobOutcome, ...] = field(default_factory=tuple)
    schema_version: str = "library_indexer_drain_result_v1"


# ----------------------------------------------------------------------
# Brand-slug derivation
# ----------------------------------------------------------------------


# Pattern matching the seed-script URL convention
# (``resemblio://seed/drl_v1/<system>/<class>/<slug>``). The first path
# component after ``drl_v1/`` is the brand-equivalent (DRL system slug).
_SEED_URL_RE = re.compile(r"^resemblio://seed/[^/]+/([^/]+)/")


def derive_brand_slug(url: str) -> str:
    """Return the URL-safe brand slug for a library row.

    Two URL shapes are handled:

    - Seed rows (``resemblio://seed/drl_v1/<system>/<class>/<slug>``) emit
      the DRL system slug verbatim.
    - Organic rows (``https://stripe.com/pricing``) emit the registered
      domain with TLD collapsed (``stripe-com``). Subdomains are kept
      (``shop.example.com`` -> ``shop-example-com``) so two different
      surfaces of the same root don't collide in library URLs.

    Falls back to a sanitized form of the full URL if neither parse
    branch yields a non-empty result; the indexer never refuses to write a
    page just because the slug was awkward.
    """
    match = _SEED_URL_RE.match(url)
    if match:
        return _slugify(match.group(1))
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host:
        return _slugify(host)
    return _slugify(url) or "unknown"


def _slugify(text: str) -> str:
    """Lowercase + dash-collapse a string for use in a library URL slug."""
    lowered = text.lower().strip()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return cleaned


# ----------------------------------------------------------------------
# DTCG payload -> compose TokenSet shape
# ----------------------------------------------------------------------


def tokens_for_compose(dtcg: dict[str, Any]) -> dict[str, str]:
    """Return a flat ``{key: value}`` dict the DRL compose pipeline accepts.

    The DRL templates read tokens via the format string `{key}` against the
    flat names emitted by ``transformer.StrippedEntry`` and stored under
    ``dtcg_json['tokens']`` for seed rows. Organic rows do not carry a
    nested ``tokens`` key; their DTCG payload itself IS the flat token map
    (the route's bundle_from_token_set helper writes it that way). Try the
    nested shape first; fall back to treating the top-level dict as flat.

    The returned dict only includes string-valued entries; nested structures
    (``patterns``, ``mood`` lists) are filtered out because compose templates
    cannot interpolate them and would raise a KeyError-like fault.
    """
    candidate: dict[str, Any]
    if isinstance(dtcg.get("tokens"), dict):
        candidate = dtcg["tokens"]
    else:
        candidate = dtcg
    return {key: str(value) for key, value in candidate.items() if isinstance(value, (str, int, float))}


# ----------------------------------------------------------------------
# Quality gate
# ----------------------------------------------------------------------


def _is_quality_gated(session: Session, asset_version: AssetVersion) -> tuple[bool, str | None]:
    """Return (skip, reason). Skip=True means do NOT generate pages.

    The gate honors mission D2 ("quality_score >= 0.7 AND no penalty
    flags") against the most recent organic extraction joined to this
    asset_version. Seed rows that lack any extraction with a scorer result
    are NOT gated — the bootstrap corpus is the curated reference and
    bypasses the scorer entirely.
    """
    if not asset_version.is_public:
        return True, "asset_version.is_public is False"

    # Probe the highest scored extraction for this asset_version. Multiple
    # extractions can collapse to one asset_version; we use the highest
    # penalized score as the representative signal. NULL scores (seed rows
    # or pre-S20 organics) are treated as "ungated" — see docstring.
    stmt = (
        select(Extraction.quality_score, Extraction.quality_dimension_scores)
        .where(Extraction.asset_version_id == asset_version.id)
        .where(Extraction.quality_score.is_not(None))
        .order_by(Extraction.quality_score.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        return False, None
    score, dimensions = row
    if score is None:
        return False, None
    if score < LIBRARY_INDEX_QUALITY_THRESHOLD:
        return True, f"quality_score {score:.3f} below threshold {LIBRARY_INDEX_QUALITY_THRESHOLD}"
    penalty_flags = _penalty_flags_from_dimensions(dimensions)
    if penalty_flags:
        return True, f"penalty flags present: {sorted(penalty_flags)!r}"
    return False, None


def _penalty_flags_from_dimensions(dimensions: Any) -> list[str]:
    """Extract penalty-flag names from a quality_dimension_scores blob.

    The schema is loosely typed (dict of dimension -> sub-payload); we look
    for a top-level ``penalty_flags`` list as the canonical place the route
    writes them. Anything else is ignored. Defensive against the column
    being NULL, a non-dict, or a dict with no penalty-flags key.
    """
    if not isinstance(dimensions, dict):
        return []
    flags = dimensions.get("penalty_flags")
    if isinstance(flags, list):
        return [str(flag) for flag in flags if flag]
    return []


# ----------------------------------------------------------------------
# Compose dispatch
# ----------------------------------------------------------------------


def _all_template_classes() -> tuple[str, ...]:
    """Return every class registered in ``TEMPLATES_BY_CLASS`` from DRL.

    Imported lazily so the indexer module can be loaded in environments
    where the DRL repo path is not yet on ``sys.path`` (test setup, CI on a
    fresh checkout). The function caches nothing; the templates module
    itself is the source of truth.
    """
    from _scripts.templates import TEMPLATES_BY_CLASS  # local import

    return tuple(sorted(TEMPLATES_BY_CLASS.keys()))


def _compose_one_page(
    class_name: str,
    *,
    brand_slug: str,
    tokens: dict[str, str],
) -> str:
    """Render the per-class HTML body fragment for a brand snapshot.

    Wraps the DRL ``templates.get_template`` lookup directly (rather than
    going through ``compose.render_html``) for two reasons:

    1. **Body fragment, not full document.** DRL's ``render_html`` returns
       a full ``<!doctype html><html>...</html>`` document because in the
       DRL repo the output is written as a standalone ``asset.html`` file.
       In the Resemblio web context the fragment is injected into a
       Next.js page; a nested ``<html>`` would be invalid markup and the
       browser would surface the inner ``<head>``/``<body>`` as raw text.
    2. **Real brand tokens reach the page.** DRL's skeleton links to a
       sibling ``tokens.css`` file; on the web the file does not exist,
       so every ``var(--ds-*)`` reference would resolve to its CSS
       fallback (or nothing). We inline a ``:root { --ds-*: ...; }`` block
       built from the brand's actual DTCG payload so the template styles
       paint with the brand's real palette and typography.

    Content placeholders (``{title}``, ``{wordmark}``, etc.) are still
    filled from DEFAULT_CONTENT (Lorem-stable) because those are copy
    samples, not tokens; the brand-truth carried into the page is the
    visual token set, not editorial content. If a brand later carries a
    wordmark/tagline in its DTCG payload we can route those through here.
    """
    from _scripts import templates as tpl  # local import

    bundle = tpl.get_template(class_name)
    filled = {ph: _brand_placeholder(ph, brand_slug=brand_slug) for ph in bundle["placeholders"]}
    body = bundle["body"].format(**filled)
    styles = bundle["styles"]
    inline_tokens_css = _tokens_to_inline_css(tokens)
    # Wrap in a per-page article element so the fragment is self-contained
    # when injected into the Next.js library page. The data attribute
    # carries the class for downstream CSS scoping if needed.
    return (
        f'<article class="rs-library-page" data-rs-class="{class_name}" data-rs-brand="{brand_slug}">\n'
        f"<style>\n{inline_tokens_css}\n{styles}\n</style>\n"
        f"{body}\n"
        f"</article>\n"
    )


def _brand_placeholder(name: str, *, brand_slug: str) -> str:
    """Return a neutral, non-Lorem placeholder for a template content slot.

    The DRL ``DEFAULT_CONTENT`` map is Lorem-ipsum-heavy because in the DRL
    repo the composed output is a developer-facing specimen page; Lorem is
    appropriate there. In the Resemblio library, the page is user-facing
    and Lorem leaks across as visible junk text ("Lorem ipsum dolor sit
    amet"). We provide a small map of human-readable, brand-aware
    placeholders that read like a real navigation/section/article without
    pretending to be brand-authored copy.

    Unknown placeholders fall back to a humanized version of the slot name
    (e.g. ``col_1_title`` -> "Col 1 Title") so the page still has visible,
    sensible text even for template slots we haven't enumerated.
    """
    pretty_brand = brand_slug.replace("-", " ").title()
    presets: dict[str, str] = {
        # Generic copy slots
        "kicker": "Featured",
        "title": f"{pretty_brand} design snapshot",
        "headline": f"{pretty_brand} design snapshot",
        "dek": "A brand-stripped, code-bearing view of the design system.",
        "wordmark": pretty_brand,
        "tagline": "Design tokens, captured.",
        "cta_primary": "Explore",
        "cta_secondary": "View tokens",
        "signin": "Sign in",
        "signup": "Sign up",
        "recommended_label": "Recommended",
        "copyright_line": f"© {pretty_brand}",
        # Navigation links
        "link_1": "Overview",
        "link_2": "Components",
        "link_3": "Tokens",
        "link_4": "Patterns",
        # Footer columns
        "col_1_title": "Product",
        "col_1_link_1": "Overview",
        "col_1_link_2": "Components",
        "col_1_link_3": "Tokens",
        "col_2_title": "Resources",
        "col_2_link_1": "Patterns",
        "col_2_link_2": "Guides",
        "col_2_link_3": "Changelog",
        "col_3_title": "Company",
        "col_3_link_1": "About",
        "col_3_link_2": "Contact",
        "col_3_link_3": "Terms",
        # Alphabet samples (typography specimens)
        "display_headline": "Display headline",
        "display_sample": "The quick brown fox jumps over the lazy dog",
        "display_sample_2": "Numbers 0123456789",
        "h2_sample": "Section heading",
        "h3_sample": "Subsection heading",
        "lead_sample": "A lead paragraph sets the tone for the article.",
        "body_sample": (
            "Body copy renders here at the default text size. "
            "It carries the bulk of the reading work on the page."
        ),
        "dek_sample": "A dek paragraph sits between headline and body.",
        "small_sample": "Small text, often used for metadata.",
        "footnote_sample": "1. Footnote text, the smallest reading size.",
        "kicker_sample": "Eyebrow kicker",
        "nav_link_sample": "Nav link",
        "button_sample": "Button label",
        "mono_sample": "code_sample_here",
        "wordmark_sample": pretty_brand.lower(),
        # Buttons / labels
        "label_primary": "Primary",
        "label_secondary": "Secondary",
        "label_outline": "Outline",
        "label_ghost": "Ghost",
        "label_destructive": "Delete",
        "label_sm": "Small",
        "label_md": "Medium",
        "label_lg": "Large",
        "label_icon_leading": "New item",
        "label_icon_trailing": "Continue",
        "label_disabled": "Disabled",
        "label_disabled_outline": "Disabled",
        # Form fields
        "legend_text": "Account details",
        "label_text": "Full name",
        "placeholder_text": "Your name",
        "help_text": "We use this to address you in emails.",
        "label_textarea": "Notes",
        "placeholder_textarea": "Add any context here.",
        "label_select": "Country",
        "option_1": "Option one",
        "option_2": "Option two",
        "option_3": "Option three",
        "label_checkbox": "Subscribe to updates",
        "radio_group_label": "Preferred contact",
        "radio_1": "Email",
        "radio_2": "Phone",
        "radio_3": "Mail",
        "label_date": "Start date",
        "label_file": "Upload document",
        "label_error": "Email address",
        "error_text": "Enter a valid email address.",
        # Inputs
        "search_label": "Search",
        "search_placeholder": "Search components, tokens, sources",
        "tags_label": "Selected tags",
        "tag_1": "design",
        "tag_2": "tokens",
        "tag_3": "components",
        "tags_placeholder": "Add tag",
        "segmented_label": "View mode",
        "seg_1": "Grid",
        "seg_2": "List",
        "seg_3": "Table",
        "toggle_label": "Enable notifications",
        "stepper_label": "Quantity",
        "stepper_value": "3",
        # Badges
        "label_info": "Info",
        "label_success": "Live",
        "label_warning": "Beta",
        "label_neutral": "Draft",
        "label_with_icon_1": "Online",
        "label_with_icon_2": "Pending",
        "label_online": "Online",
        "label_away": "Away",
        "label_offline": "Offline",
        # Cards
        "basic_title": f"{pretty_brand} card",
        "basic_body": "Short card body sits under the title.",
        "basic_link": "Learn more",
        "image_title": "Featured asset",
        "image_body": "Card body sits under the title.",
        "quote_text": "Design is how it works.",
        "quote_author": "Jane Doe",
        "quote_role": "Design Lead",
        "pricing_eyebrow": "Studio",
        "pricing_tier": "Studio plan",
        "pricing_amount": "$49",
        "pricing_period": "/month",
        "pricing_dek": "Everything in Solo plus more capacity.",
        "pricing_cta": "Get started",
        "stat_label": "Active users",
        "stat_value": "12,480",
        "stat_dek": "Up from last month.",
        "list_title": "Recent activity",
        "list_item_1": "Released a new component",
        "list_item_2": "Updated the color palette",
        "list_item_3": "Shipped dark-mode variants",
    }
    if name in presets:
        return presets[name]
    return name.replace("_", " ").strip().title() or name


def _tokens_to_inline_css(tokens: dict[str, str]) -> str:
    """Render the brand's DTCG token dict as a ``:root { --ds-*: ...; }`` block.

    The DRL templates reference design tokens via ``var(--ds-<key>)`` where
    ``<key>`` is the flat token name with underscores replaced by dashes
    (e.g. ``font_display`` -> ``--ds-font-display``). We emit a custom
    property per token so the template styles paint with the brand's
    actual palette/typography rather than the browser default.

    Tokens are emitted in sorted order for deterministic output (the
    fragment ends up in ``library_pages.rendered_html`` and stable text
    output keeps diffs reviewable).
    """
    if not tokens:
        return ":root {}"
    lines = [":root {"]
    for key in sorted(tokens):
        css_key = "--ds-" + key.replace("_", "-")
        lines.append(f"  {css_key}: {tokens[key]};")
    lines.append("}")
    return "\n".join(lines)


def _metadata_for(class_name: str, *, brand_slug: str, tokens: dict[str, str]) -> dict[str, Any]:
    """Return the OG-image + page-copy metadata envelope.

    Subset of tokens (bg, surface, text, accent, font_display, font_body)
    plus the schema-version tag. Downstream consumers may add fields; we
    keep the v1 shape intentionally small so the OG image generator can
    render off this payload alone.
    """
    return {
        "schema_version": LIBRARY_PAGE_METADATA_SCHEMA_VERSION,
        "brand_slug": brand_slug,
        "category_slug": class_name,
        "bg": tokens.get("bg"),
        "surface": tokens.get("surface"),
        "text": tokens.get("text"),
        "accent": tokens.get("accent"),
        "font_display": tokens.get("font_display"),
        "font_body": tokens.get("font_body"),
    }


# ----------------------------------------------------------------------
# Canonical flag reconciliation
# ----------------------------------------------------------------------


def _reconcile_canonical(session: Session, asset_version: AssetVersion) -> None:
    """Set ``is_canonical`` on this asset_version's pages and flip older ones FALSE.

    The "latest version" per (brand_slug, category_slug) is the one whose
    asset_version has the most recent ``fetched_at``. Run after pages are
    written so the just-inserted rows participate in the comparison.
    """
    brand_slug = derive_brand_slug(asset_version.url)
    # Find the latest asset_version_id per brand_slug. We compute it by
    # joining library_pages back to asset_versions and ordering by
    # fetched_at; the winner per brand_slug is the asset_version whose
    # pages should be canonical.
    latest_stmt = (
        select(AssetVersion.id)
        .join(LibraryPage, LibraryPage.asset_version_id == AssetVersion.id)
        .where(LibraryPage.brand_slug == brand_slug)
        .order_by(AssetVersion.fetched_at.desc())
        .limit(1)
    )
    latest_id = session.execute(latest_stmt).scalar_one_or_none()
    if latest_id is None:
        return
    # Pages owned by the winning asset_version flip to canonical=True;
    # pages owned by older asset_versions for the same brand flip FALSE.
    session.execute(
        update(LibraryPage)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(LibraryPage.asset_version_id == latest_id)
        .values(is_canonical=True)
    )
    session.execute(
        update(LibraryPage)
        .where(LibraryPage.brand_slug == brand_slug)
        .where(LibraryPage.asset_version_id != latest_id)
        .values(is_canonical=False)
    )


# ----------------------------------------------------------------------
# Job processing
# ----------------------------------------------------------------------


def _process_job(session: Session, job: LibraryIndexJob) -> JobOutcome:
    """Run one job to completion (or quality-gated skip). Commits per call.

    Failures raise; the caller (``drain_pending``) catches and updates the
    row to either ``pending`` (retry) or ``failed`` (retry budget exhausted).
    """
    asset_version = session.get(AssetVersion, job.asset_version_id)
    if asset_version is None:
        job.status = "failed"
        job.last_error = "asset_version row missing"
        job.completed_at = datetime.now(timezone.utc)
        session.commit()
        return JobOutcome(
            job_id=job.id,
            asset_version_id=job.asset_version_id,
            status="failed",
            pages_written=0,
            reason="asset_version row missing",
        )

    gated, reason = _is_quality_gated(session, asset_version)
    if gated:
        job.status = "complete"
        job.last_error = reason
        job.completed_at = datetime.now(timezone.utc)
        session.commit()
        return JobOutcome(
            job_id=job.id,
            asset_version_id=job.asset_version_id,
            status="complete",
            pages_written=0,
            reason=reason,
        )

    brand_slug = derive_brand_slug(asset_version.url)
    tokens = tokens_for_compose(asset_version.dtcg_json or {})
    written = 0
    for class_name in _all_template_classes():
        rendered = _compose_one_page(class_name, brand_slug=brand_slug, tokens=tokens)
        metadata = _metadata_for(class_name, brand_slug=brand_slug, tokens=tokens)
        page = LibraryPage(
            asset_version_id=asset_version.id,
            category_slug=class_name,
            brand_slug=brand_slug,
            version_label=asset_version.version_label,
            rendered_html=rendered,
            metadata_json=metadata,
            is_canonical=False,
        )
        session.add(page)
        try:
            session.flush()
            written += 1
        except IntegrityError:
            # UNIQUE(asset_version_id, category_slug) trip: the row already
            # exists from a prior run. The contract is idempotent — log and
            # continue without counting it as a new write.
            session.rollback()
            continue

    _reconcile_canonical(session, asset_version)

    job.status = "complete"
    job.last_error = None
    job.completed_at = datetime.now(timezone.utc)
    session.commit()
    return JobOutcome(
        job_id=job.id,
        asset_version_id=job.asset_version_id,
        status="complete",
        pages_written=written,
    )


# ----------------------------------------------------------------------
# Public entry points
# ----------------------------------------------------------------------


def enqueue_for_asset_version(session: Session, asset_version_id: int) -> LibraryIndexJob | None:
    """Insert a job row for ``asset_version_id`` unless one is already live.

    "Live" means status in ``{pending, running}``. Completed or failed
    rows for the same asset_version do NOT block a new enqueue, so a
    rescored asset (organic re-extraction that bumps quality_score above
    the gate) can be re-indexed cleanly. Returns the new row, or None if
    a live job already covers it.

    The caller is responsible for the surrounding transaction; this
    helper flushes for an immediate primary-key fetch but does not commit.
    """
    existing_stmt = (
        select(LibraryIndexJob)
        .where(LibraryIndexJob.asset_version_id == asset_version_id)
        .where(LibraryIndexJob.status.in_(("pending", "running")))
        .limit(1)
    )
    existing = session.execute(existing_stmt).scalar_one_or_none()
    if existing is not None:
        return None
    job = LibraryIndexJob(
        asset_version_id=asset_version_id,
        status="pending",
        attempts=0,
    )
    session.add(job)
    session.flush()
    return job


def drain_pending(
    session: Session,
    *,
    batch_size: int = LIBRARY_INDEX_BATCH_SIZE,
    now: datetime | None = None,
) -> DrainResult:
    """Drain up to ``batch_size`` pending jobs and return a structured result.

    Each job is processed in its own transaction (committed inside
    ``_process_job`` for the success / quality-gated path; rolled back +
    re-committed for the retry / failure path here). One poison row
    therefore cannot lose work from earlier successful rows in the batch.
    """
    effective_now = now if now is not None else datetime.now(timezone.utc)
    stmt = (
        select(LibraryIndexJob)
        .where(LibraryIndexJob.status == "pending")
        .order_by(LibraryIndexJob.enqueued_at.asc())
        .limit(batch_size)
    )
    jobs = list(session.execute(stmt).scalars())

    outcomes: list[JobOutcome] = []
    pages_total = 0
    for job in jobs:
        # Optimistic move to running; if the worker crashes between this
        # commit and the per-job commit, the row stays at running and the
        # next tick can reclaim it (a future hardening pass can add a
        # stale-lease sweep; v1.1 keeps the simpler shape).
        job.status = "running"
        job.started_at = effective_now
        job.attempts = (job.attempts or 0) + 1
        session.commit()
        try:
            outcome = _process_job(session, job)
        except Exception as exc:  # noqa: BLE001 - all compose faults must be caught
            session.rollback()
            # Re-fetch the row inside a fresh transaction so the
            # status-update below isn't fighting a stale ORM object.
            row = session.get(LibraryIndexJob, job.id)
            if row is None:
                # Should not happen; record the failure and move on.
                logger.exception("library_indexer_job_lost id=%s", job.id)
                continue
            error_repr = repr(exc)[:1024]
            if row.attempts >= LIBRARY_INDEX_MAX_ATTEMPTS:
                row.status = "failed"
                row.completed_at = datetime.now(timezone.utc)
            else:
                row.status = "pending"
            row.last_error = error_repr
            session.commit()
            outcomes.append(
                JobOutcome(
                    job_id=row.id,
                    asset_version_id=row.asset_version_id,
                    status="failed" if row.status == "failed" else "pending_retry",
                    pages_written=0,
                    reason=error_repr,
                )
            )
            logger.warning(
                "library_indexer_job_error id=%s attempts=%s error=%s",
                row.id,
                row.attempts,
                error_repr,
            )
            continue
        outcomes.append(outcome)
        pages_total += outcome.pages_written

    return DrainResult(
        jobs_run=len(jobs),
        pages_written=pages_total,
        outcomes=tuple(outcomes),
    )
