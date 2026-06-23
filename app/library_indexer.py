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

from sqlalchemy import case, func, select, update
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
#
# 2026-06-02 OUTAGE NOTE: a 3-hour Library outage on prod was caused by this
# module-load contract being implicit. Some import path in the running
# service loaded ``app.library_indexer`` before ``app.extractor_bridge``'s
# ``sys.path`` install had run, every job in the queue silently failed with
# ``ModuleNotFoundError: No module named '_scripts'``, and ``library_pages``
# stayed empty until the eventual fix in commit c5631c8. The implicit
# contract (this import line MUST execute before any compose call) was not
# enforced by code, only by a comment. The runtime guard below
# (``_assert_drl_path_ready``) and the structured startup log
# (``_emit_startup_log``) turn that implicit contract into an explicit,
# fail-loud one: if the path install did not run, module load itself raises
# an ``ImportError`` with a specific message naming the failure mode, and a
# downstream operator can grep ``library_indexer.startup`` in journald to
# confirm the load order on every CLI tick.
from app import extractor_bridge as _extractor_bridge  # noqa: F401
from app.asset_versions import get_asset_component
from app.brand_capture_manifest import BrandCaptureManifest, build_capture_manifest
from app.brand_names import pretty_brand_name
from app.library_render_policy import evaluate_category_render
from app.library_style_scope import scope_style_block
from app.missing_data_notice import build_hub_capture_signal, build_missing_notice
from app.library_web_fonts import (
    build_font_alternative_root_block,
    build_font_disclosure_payload,
    build_google_fonts_link_tag,
    render_font_disclosure_html,
)
from app.models import AssetComponent, AssetVersion, Extraction, LibraryIndexJob, LibraryPage
from extractor.button_override import apply_button_tokens
from extractor.button_tokens import ButtonTokens, derive_button_tokens
from extractor.computed_styles import ComputedStyleReport
from extractor.token_contract import BRAND_TOKEN_CONTRACT


logger = logging.getLogger("resemblio.library_indexer")


# ----------------------------------------------------------------------
# Module-load race guard + startup observability
# ----------------------------------------------------------------------
#
# Locked 2026-06-03 after the 2026-06-02 3-hour Library outage. See the
# OUTAGE NOTE above for context. The two helpers below run once, at module
# load time, immediately under this block. Together they enforce the
# previously-implicit ``extractor_bridge before library_indexer`` contract.


LIBRARY_INDEXER_STARTUP_LOG_SCHEMA_VERSION = "library_indexer_startup_v1"
"""Schema version stamped onto every ``library_indexer.startup`` log entry.

Operators grep this string in journald to confirm the indexer module loaded
cleanly on the most recent CLI tick. The version is bumped only if the
StartupLog dataclass shape below changes; downstream log-shipping consumers
key off this string for shape detection.
"""

_DRL_PATH_GUARD_REQUIRED_MODULE = "_scripts.templates"
"""DRL module the indexer's compose path lazy-imports. If the vendored DRL
``sys.path`` install (from ``app.extractor_bridge``) did not run, importing
this module raises ``ModuleNotFoundError`` and the worker silently writes
zero ``library_pages`` rows for every job. Probing this exact module is the
cheapest way to prove the load-order contract held."""

_DRL_PATH_GUARD_FAILURE_MSG = (
    "library_indexer module-load race detected: "
    "{required_module!r} is not importable, which means "
    "app.extractor_bridge did not run its DRL sys.path install before "
    "app.library_indexer loaded. This is the 2026-06-02 outage shape "
    "(commit c5631c8 fix). Restore the top-of-module "
    "``from app import extractor_bridge`` import or ensure the bridge "
    "loads before this module on every entrypoint. Original ImportError: "
    "{original!r}"
)
"""Failure-message template for the runtime guard. Specific enough that an
operator paging on it knows the exact shape (the 2026-06-02 outage) and the
exact remediation (the bridge-first import) without needing to read this
file."""


@dataclass(frozen=True)
class StartupLog:
    """Structured log entry emitted once per module load.

    The shape is grep-able from journald. ``module_load_order`` lists the
    DRL-related modules in the order they appear in ``sys.modules`` at
    guard-time so the next outage post-mortem can confirm whether the bridge
    actually loaded first. ``schema_version`` stamps the entry so downstream
    consumers can detect shape drift.
    """

    schema_version: str
    extractor_bridge_loaded: bool
    drl_templates_importable: bool
    module_load_order: tuple[str, ...]


def _drl_module_load_order() -> tuple[str, ...]:
    """Return the DRL-related modules in ``sys.modules`` insertion order.

    Insertion order matters for the post-mortem signal: ``app.extractor_bridge``
    must appear before any ``_scripts.*`` module on every healthy startup.
    The dict-iteration order is insertion order in CPython 3.7+; we rely on
    that contract here.
    """
    import sys as _sys

    interesting: list[str] = []
    for name in _sys.modules:
        if name == "app.extractor_bridge" or name.startswith("_scripts"):
            interesting.append(name)
    return tuple(interesting)


def _assert_drl_path_ready() -> None:
    """Fail loud at module load if the DRL ``sys.path`` install did not run.

    Probes ``_DRL_PATH_GUARD_REQUIRED_MODULE``. If the import succeeds, the
    bridge's path install ran; the guard is silent. If the import raises
    ``ModuleNotFoundError`` (or any ``ImportError``), the guard re-raises an
    ``ImportError`` whose message names the 2026-06-02 outage shape and the
    remediation, so an operator paging on the error gets actionable context
    without reading source.

    Defensive note: this guard runs once, at module load. It does not
    re-check on every ``drain_pending`` call; the cost would be a `sys.path`
    rescan per tick for no marginal protection (the bridge cannot un-install
    its path mid-process).
    """
    import importlib

    try:
        importlib.import_module(_DRL_PATH_GUARD_REQUIRED_MODULE)
    except ImportError as exc:
        raise ImportError(
            _DRL_PATH_GUARD_FAILURE_MSG.format(
                required_module=_DRL_PATH_GUARD_REQUIRED_MODULE,
                original=exc,
            )
        ) from exc


def _emit_startup_log() -> StartupLog:
    """Emit and return the structured startup log entry.

    Called once at module load time, right after the runtime guard passes.
    Returns the entry so callers (and tests) can introspect the shape.
    """
    import sys as _sys

    # Check the bridge two ways: sys.modules entry AND attribute on the
    # parent app package. In long-lived test sessions the sys.modules entry
    # may have been popped while the parent-package attribute is still set
    # (the indexer's ``from app import extractor_bridge`` re-binds locally
    # without re-running the submodule). Either signal proves the bridge
    # contract held for this module load.
    import app as _app_pkg
    bridge_in_sys = "app.extractor_bridge" in _sys.modules
    bridge_on_pkg = hasattr(_app_pkg, "extractor_bridge")
    entry = StartupLog(
        schema_version=LIBRARY_INDEXER_STARTUP_LOG_SCHEMA_VERSION,
        extractor_bridge_loaded=bridge_in_sys or bridge_on_pkg,
        drl_templates_importable=_DRL_PATH_GUARD_REQUIRED_MODULE in _sys.modules,
        module_load_order=_drl_module_load_order(),
    )
    logger.info(
        "library_indexer.startup schema_version=%s extractor_bridge_loaded=%s "
        "drl_templates_importable=%s module_load_order=%s",
        entry.schema_version,
        entry.extractor_bridge_loaded,
        entry.drl_templates_importable,
        list(entry.module_load_order),
    )
    return entry


# Run the guard + emit the log at module load. Order matters: the guard runs
# first so a failed startup raises before the log is emitted (an emitted
# "startup ok" log on a broken load would lie to the operator).
_assert_drl_path_ready()
_STARTUP_LOG: StartupLog = _emit_startup_log()


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


def slugify_version_label(label: str | None) -> str | None:
    """Slugify a free-form ``version_label`` for URL-safe storage.

    The seed pipeline writes ``asset_versions.version_label`` as a
    human-readable string (e.g. ``"DRL bootstrap 2026-05-21"``). The library
    indexer must write a URL-safe form to ``library_pages.version_label`` so
    the downstream ``/library/<brand>/<version>/...`` route resolves.

    Returns:
        ``None`` for a ``None`` input or a label that slugifies to the empty
        string (defensive; callers treat ``None`` as "no version scope").
        Otherwise the lowercase + dash-collapsed slug.
    """
    if label is None:
        return None
    slug = _slugify(label)
    return slug or None


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
    button_tokens: ButtonTokens | None = None,
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
    filled = {
        ph: _brand_placeholder(ph, brand_slug=brand_slug, category_slug=class_name)
        for ph in bundle["placeholders"]
    }
    body = bundle["body"].format(**filled)
    styles = bundle["styles"]
    # Selector-scope the DRL template CSS to the per-page article wrapper.
    # Vendored DRL emits document-level resets (`*, *::before, *::after`,
    # `html, body { ... }`) that leak out of the article and repaint the
    # surrounding Next.js page chrome. scope_style_block rewrites bare
    # selectors to be prefixed by `.rs-library-page`; :root and at-rules
    # are preserved. See app/library_style_scope.py for the rule table.
    scoped_styles = scope_style_block(styles)
    # L-6 / L-13 BLOCKER FIX (Phase B, 2026-06-03): text-only fallback for
    # the ABOUT_TEAM avatar slot. The vendored DRL template emits empty
    # ``<div class="at__avatar">`` shells that paint as gray circles in
    # the brand-surface palette (Phase A audit, ``aeon_1440x900.png``).
    # Path B per Jim's default: hide the avatar containers so the team
    # card renders text-only (role + dek), no placeholder circles. The
    # override is keyed on the scoped selector emitted by scope_style_block
    # so it lands at the same specificity as the DRL rule it overrides.
    scoped_styles = scoped_styles + LIBRARY_TEMPLATE_OVERRIDE_CSS
    inline_tokens_css = _emit_brand_root(tokens)
    # L-20 fix (Frank, 2026-06-04): the brand's --ds-font-* tokens are
    # already emitted into the :root block above, but the rendered HTML
    # never loaded the actual web fonts. Every brand was falling through
    # its family stack to ``Helvetica Neue`` / ``Georgia`` / ``Consolas``
    # and reading identically on /library/<brand>/alphabet/. Emit a
    # single Google Fonts <link> tag for every allowlisted family the
    # brand declares; brands whose faces are not on Google Fonts (private
    # CDN-only faces) still render the CSS fallback - graceful degrade,
    # no 404 burst. See app/library_web_fonts.py.
    web_font_link = build_google_fonts_link_tag(tokens)
    web_font_block = f"{web_font_link}\n" if web_font_link else ""
    # Phase 1 inspirado-no-copiado correction (Frank, 2026-06-04 02:35 UTC).
    # Override the brand's --ds-font-* slots to point at the free
    # alternative we actually loaded above so every specimen paints with
    # the loaded face rather than falling through to a system fallback.
    # The override block sits AFTER the brand :root block in source
    # order so the cascade wins. The disclosure aside surfaces the
    # brand's real font + the free-alternative attribution to the user.
    font_alt_root_block = build_font_alternative_root_block(tokens)
    disclosure_payload = build_font_disclosure_payload(tokens)
    disclosure_aside = render_font_disclosure_html(
        disclosure_payload,
        brand_display_name=pretty_brand_name(brand_slug),
    )
    # Wrap in a per-page article element so the fragment is self-contained
    # when injected into the Next.js library page. The data attribute
    # carries the class for downstream CSS scoping if needed.
    fragment = (
        f'<article class="rs-library-page" data-rs-class="{class_name}" data-rs-brand="{brand_slug}">\n'
        f"{web_font_block}"
        f"<style>\n{inline_tokens_css}\n{font_alt_root_block}{scoped_styles}\n</style>\n"
        f"{disclosure_aside}\n"
        f"{body}\n"
        f"</article>\n"
    )
    # Hybrid Path B button-fidelity seam (CTO 2026-06-02). When the brand
    # has an R3.1 computed-styles snapshot on disk we derive ButtonTokens
    # and inject a `.b-btn` override block so the rendered button matches
    # the source page's pill/chiclet/etc. For brands without a snapshot
    # (the current default for every DRL-seeded entry) `apply_button_tokens`
    # is a no-op and the vendored DRL default ships untouched. Override is
    # retired when DRL upstream lands the `--ds-button-*` contract (Path A).
    return apply_button_tokens(fragment, button_tokens)


def _compose_real_component(
    component: AssetComponent,
    *,
    class_name: str,
    brand_slug: str,
    tokens: dict[str, str],
) -> str:
    """Render the HTML fragment for a page using the stored DRL component code.

    Unlike ``_compose_one_page`` which renders a generic DRL template tinted
    with brand tokens, this function wraps the actual markup + CSS extracted
    from the DRL ``asset.html`` (stored in ``asset_components`` by the seed
    pipeline). The result carries real interaction states (:hover, :focus-visible,
    etc.) rather than a recolored generic chiclet.

    The key difference from ``_compose_one_page`` is that ``component.component_html``
    is DRL-authored markup for ONE specific component, not a template filled with
    Lorem-stable placeholder content. The CSS is likewise component-specific and
    is scoped identically to the template path so it does not leak into the
    surrounding Next.js page chrome.

    The ``data-rs-source="drl-component"`` attribute on the article wrapper is the
    machine-checkable contract marker distinguishing this path from the generic
    template path. The web layer and acceptance tests key off this attribute.

    Note: ``apply_button_tokens`` is NOT called here. That hybrid Path-B override
    paints the generic ``.b-btn`` chiclet with Playwright-captured metrics; it is
    meaningless (and harmful) when the component HTML is already the real DRL markup
    with its own interaction CSS. The override is retired for assets whose
    ``asset_components`` row is populated.

    Args:
        component: the AssetComponent row carrying real DRL markup + CSS.
        class_name: the DRL template class (e.g. 'buttons'). Written to
            ``data-rs-class``; must equal ``asset_version.dtcg_json["class"]``.
        brand_slug: canonical brand identifier for the ``data-rs-brand`` attribute.
        tokens: flat ``{key: value}`` brand token dict from ``tokens_for_compose``.
    """
    # Scope the real component CSS to the per-page article, exactly as
    # _compose_one_page does for the generic DRL template CSS. Real DRL assets
    # can include document-level resets (*::before, html, body) that would leak
    # into the surrounding Next.js page without this rewrite.
    scoped_styles = scope_style_block(component.component_css)
    inline_tokens_css = _emit_brand_root(tokens)

    # Font loading strategy (Issue #38, AC1):
    #
    # When head_html is set (migration 0024+), use the DRL-curated <link> tags
    # verbatim. This guarantees the candidate page loads exactly the same font
    # families as the DRL reference so font-family computed styles match in the
    # fidelity oracle. The registry-derived alternative and its :root override
    # block are both suppressed for this path.
    #
    # When head_html is empty (rows seeded before migration 0024), fall back to
    # the registry path so existing behavior is preserved for legacy data.
    if component.head_html:
        web_font_block = component.head_html + "\n"
        # Suppress the secondary :root override that redirects --ds-font-* vars
        # to the registry alternative. For real components, those vars already
        # resolve to the brand-supplied families (which the browser then falls
        # through to the Google Font loaded via head_html). Applying the override
        # would change font-family away from what the DRL reference shows.
        font_alt_root_block = ""
    else:
        # Legacy fallback: derive font loading from the brand font registry.
        web_font_link = build_google_fonts_link_tag(tokens)
        web_font_block = f"{web_font_link}\n" if web_font_link else ""
        font_alt_root_block = build_font_alternative_root_block(tokens)

    disclosure_payload = build_font_disclosure_payload(tokens)
    disclosure_aside = render_font_disclosure_html(
        disclosure_payload,
        brand_display_name=pretty_brand_name(brand_slug),
    )
    # data-rs-source="drl-component" is the contract distinguishing real-component
    # pages from generic-template pages. Both the web layer and the acceptance tests
    # assert on this attribute. Do not add it to _compose_one_page output.
    fragment = (
        f'<article class="rs-library-page" data-rs-class="{class_name}"'
        f' data-rs-brand="{brand_slug}" data-rs-source="drl-component">\n'
        f"{web_font_block}"
        f"<style>\n{inline_tokens_css}\n{font_alt_root_block}{scoped_styles}\n</style>\n"
        f"{disclosure_aside}\n"
        f"{component.component_html}\n"
        f"</article>\n"
    )
    return fragment


def _compose_with_gate(
    class_name: str,
    *,
    brand_slug: str,
    tokens: dict[str, str],
    button_tokens: ButtonTokens | None,
    manifest: BrandCaptureManifest,
    dtcg_class: str | None = None,
    real_component: AssetComponent | None = None,
) -> str:
    """Apply the D2 render gate and compose the page HTML fragment.

    This is the single call-site that enforces Library v2 Decision D2:

    - **Page-pattern categories** (hero, navigation, footer, etc.) are NOT in
      ``CATEGORY_CAPTURE_REQUIREMENTS`` and always pass the gate; their HTML
      body is composed unconditionally. They demonstrate layout, not component
      geometry, and the cascade-safety ``var()`` fallbacks in the ``:root``
      block make every ``var(--ds-button-*)`` reference resolve to a defined
      value even when the button group is uncaptured.

    - **Showcase categories** (buttons, cards, badges, form-fields, inputs,
      library) are gated: if the brand's manifest shows the required component
      group is NOT captured, the body is omitted (``returned as ""``). An
      uncaptured showcase category renders identically to any other uncaptured
      brand at contract defaults; returning the empty string is the only way to
      distinguish "brand chose these exact values" from "we have no data."

    The web layer reads ``metadata_json.missing_data_notice`` to surface an
    honest "Not yet captured" acknowledgment in place of the empty body.

    Args:
        class_name: the DRL template class name (e.g. 'buttons', 'hero').
        brand_slug: canonical brand identifier (e.g. 'stripe').
        tokens: flat ``{key: value}`` brand token dict from ``tokens_for_compose``.
        button_tokens: R3.1 computed-style snapshot or ``None``.
        manifest: pre-built ``BrandCaptureManifest`` from
            ``build_capture_manifest``. Callers compute this ONCE per brand
            and pass it here for every class in the loop (avoids N re-computations).
        dtcg_class: the asset's own DRL class from ``dtcg_json["class"]`` (e.g.
            ``'buttons'``). When set and matching ``class_name``, the real-component
            path fires instead of the generic template. ``None`` falls through to
            the existing gate + template behavior (backward-compatible).
        real_component: the ``AssetComponent`` row for this asset_version (fetched
            once per asset_version before the class loop). ``None`` when the asset
            has no component row or when ``dtcg_class`` is unset.

    Returns:
        HTML fragment string (the full ``<article>`` block) if the category
        should render, or ``""`` if it is gated out or no component is stored.
    """
    # Real-component routing (issue #3): when this class is the asset's own
    # DRL class (class_name == dtcg_class), serve the stored component code
    # rather than the generic template. The class-match guard is intentional:
    # an asset_version represents exactly one DRL asset whose real component
    # code is for ONE class. Serving it for any other class would mix concerns
    # (e.g. buttons markup on a hero page). All non-matching classes continue
    # to use the existing gate + template path below.
    if dtcg_class is not None and class_name == dtcg_class:
        if real_component is not None:
            return _compose_real_component(
                real_component,
                class_name=class_name,
                brand_slug=brand_slug,
                tokens=tokens,
            )
        # Matching class but no component row stored: return empty so the web
        # layer shows an honest "not captured" notice. Never fabricate a generic
        # template here - a generic buttons chiclet for a buttons brand that
        # has no real buttons data is a false signal to the user.
        return ""

    decision = evaluate_category_render(class_name, manifest)
    if not decision.should_render:
        return ""
    return _compose_one_page(
        class_name,
        brand_slug=brand_slug,
        tokens=tokens,
        button_tokens=button_tokens,
    )


# L-6 / L-13 text-only-fallback override CSS appended to every rendered
# article's scoped style block. The ABOUT_TEAM template's avatar shells
# (``.at__avatar``) paint as gray placeholder circles in the brand surface
# palette; Path B of Phase B (per Jim's default 2026-06-03) is to hide the
# shells so the team card renders text-only. The selector is prefixed with
# ``.rs-library-page`` to match the scope ``scope_style_block`` applies to
# the DRL rule it overrides; ``display: none !important`` is needed because
# the DRL rule and the override land at the same specificity and the DRL
# rule comes first in source order.
LIBRARY_TEMPLATE_OVERRIDE_CSS = (
    "\n"
    "/* Resemblio override: text-only team-card fallback (L-6 / L-13). */\n"
    ".rs-library-page .at__avatar { display: none !important; }\n"
)
"""CSS block appended to every scoped article style.

Lives at module scope (not inside ``_compose_one_page``) so a regression
test can import it directly and assert the override is present in
rendered output without re-deriving the literal string from a snapshot
diff.
"""


# Subdirectory name under both the runtime root and the seed root. Path
# resolution lives in ``app.runtime_data``; see that module's docstring for
# the runtime-vs-seed split. Pre-2026-06-03 this constant pointed directly
# at the in-tree vendored path, which caused CI deploys to fail when
# runtime-owned files blocked ``git reset --hard``.
_BUTTON_SNAPSHOT_SUBDIR = "computed_styles"

# Preserved for backward compatibility with tests that monkeypatched the
# seed location directly. New code should use ``app.runtime_data`` instead.
# This attribute now resolves on demand via a property-like proxy so the
# legacy test pattern (``monkeypatch.setattr(library_indexer_mod,
# "_BUTTON_SNAPSHOT_DIR", tmp_path)``) keeps working: when set on the
# module object it overrides the runtime-data resolver.
_BUTTON_SNAPSHOT_DIR: Path | None = None
"""Test-only override hook. When set, ``_load_button_tokens`` looks here
exclusively and skips the runtime/seed split. Production code leaves this
``None`` so the runtime_data resolver controls path lookup."""


def _load_button_tokens(brand_slug: str) -> ButtonTokens | None:
    """Load the brand's R3.1 button tokens from its on-disk snapshot.

    Returns ``None`` when no snapshot exists for ``brand_slug``, when the
    snapshot is malformed, or when the report carries no usable ``cta``
    slot. The caller treats every ``None`` as "no override, ship the DRL
    default" - the override is fail-safe by design (CTO Hybrid Path B,
    2026-06-02). Failure to read a snapshot is logged but never raised.

    Path resolution: when ``_BUTTON_SNAPSHOT_DIR`` is set (test override),
    look only there. Otherwise delegate to ``app.runtime_data`` which tries
    the runtime-data root first and falls back to the in-tree seed
    location, so a brand newly captured by the running service is read
    from ``/var/lib/resemblio/computed_styles/`` while a brand still on
    its baseline snapshot is read from the vendored seed tree.
    """
    if not brand_slug:
        return None
    if _BUTTON_SNAPSHOT_DIR is not None:
        snapshot_path: Path | None = _BUTTON_SNAPSHOT_DIR / f"{brand_slug}.json"
        if snapshot_path is not None and not snapshot_path.exists():
            return None
    else:
        from app.runtime_data import resolve_read_path

        snapshot_path = resolve_read_path(
            _BUTTON_SNAPSHOT_SUBDIR, f"{brand_slug}.json"
        )
        if snapshot_path is None:
            return None
    try:
        import json as _json

        report_raw = _json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "library_indexer_button_snapshot_unreadable brand=%s path=%s err=%s",
            brand_slug,
            snapshot_path,
            exc,
        )
        return None
    if not isinstance(report_raw, dict):
        return None
    # Drop any human-facing README key the fixture format carries.
    report_raw.pop("_README", None)
    report: ComputedStyleReport = report_raw  # type: ignore[assignment]
    return derive_button_tokens(report)


# Category-slug -> Title-Case display-fragment mapping used to specialize
# the ``title``/``headline`` slots on per-category renders (L-15 fix,
# L-18 phrasing polish 2026-06-03).
#
# Phrasing convention
# -------------------
# Values are Title-Case noun phrases that read as editorial card titles.
# They are joined to the brand with a colon at the call site
# (``f"{pretty_brand}: {category_label}"``), producing magazine-cover-
# style strings:
#
#   - "Apple: About & Team"     (was "Apple about team")
#   - "Apple: Buttons"          (was "Apple buttons")
#   - "Apple: Alphabet"
#   - "Apple: Article Layout"
#   - "Apple: How It Works"
#
# Why colon-join over the prior bare-juxtaposition pattern
# ("Apple buttons"):
#
# 1. Grammatical: the prior pattern read as an implicit possessive
#    ("Apple buttons" = "Apple's buttons") which works for some slugs
#    ("buttons", "navigation") and reads broken for others
#    ("about team", "article layout", "how it works", "news list",
#    "cta block"). Colon-eyebrow works uniformly.
# 2. Editorial: the surface is a featured-card title on the brand's
#    library hub. Eyebrow + headline is the standard editorial card
#    composition; brand-as-eyebrow followed by category-as-headline
#    reads like a section label.
# 3. Internal-jargon scrub: slugs like "cta-block" become "Call to
#    Action" rather than leaking template jargon, and "form-fields"
#    becomes "Form Fields" rather than the ambiguous "forms".
#
# Slugs not in this map fall back to a humanize+Title-Case of the slug
# ("brand-new-category" -> "Brand New Category"). Snapshot/featured-
# snapshot classes return None so the title falls through to the brand-
# level "{Brand} design snapshot" copy on the brand-canonical page.
_CATEGORY_DISPLAY_LABELS: dict[str, str] = {
    # Component-library categories (Library v1.1).
    "buttons": "Buttons",
    "alphabet": "Alphabet",
    "badges": "Badges",
    "cards": "Cards",
    "inputs": "Inputs",
    "navigation": "Navigation",
    "pricing": "Pricing",
    "forms": "Forms",
    "form-fields": "Form Fields",
    # Page-pattern categories (DRL canon).
    "about-team": "About & Team",
    "article-layout": "Article Layout",
    "news-list": "News Feed",
    "how-it-works": "How It Works",
    "hero": "Hero",
    "footer": "Footer",
    "feature-grid": "Feature Grid",
    "cta-block": "Call to Action",
    "pricing-table": "Pricing Table",
    "testimonials": "Testimonials",
    "process-steps": "Process Steps",
    "library": "Library Index",
}


# Join character between brand and category in the specialized title.
# Magazine-cover convention: brand reads as eyebrow, category as
# headline. Kept as a module-level constant so the test suite and any
# downstream consumer can assert against a single source of truth.
CATEGORY_TITLE_JOIN: str = ": "


def _category_display_label(category_slug: str | None) -> str | None:
    """Return the Title-Case display label for a category slug, or None.

    Returns None when:

    - ``category_slug`` is None / empty (legacy callers).
    - The slug is one of the brand-snapshot family classes (``snapshot``,
      ``featured-snapshot``) for which the brand-level title is correct
      and must not be category-specialized.

    For known slugs returns the curated Title-Case display label; for
    unknown slugs returns a humanized + title-cased form
    (``"some-new-category"`` -> ``"Some New Category"``) so a new DRL
    category class still renders a sensible title without a code edit.
    Pure-data; no I/O.

    The returned label is joined to the brand via ``CATEGORY_TITLE_JOIN``
    at the call site in ``_brand_placeholder``; see the
    ``_CATEGORY_DISPLAY_LABELS`` docstring for the phrasing rationale.
    """
    if not category_slug:
        return None
    if category_slug in {"snapshot", "featured-snapshot"}:
        return None
    if category_slug in _CATEGORY_DISPLAY_LABELS:
        return _CATEGORY_DISPLAY_LABELS[category_slug]
    # Unknown slug: humanize ("article-layout" -> "article layout") and
    # title-case ("Article Layout"). Keeps the convention uniform for
    # any DRL class added after this map was last updated.
    return category_slug.replace("-", " ").title()


def _brand_placeholder(
    name: str,
    *,
    brand_slug: str,
    category_slug: str | None = None,
) -> str:
    """Return a neutral, non-Lorem placeholder for a template content slot.

    The DRL ``DEFAULT_CONTENT`` map is Lorem-ipsum-heavy because in the DRL
    repo the composed output is a developer-facing specimen page; Lorem is
    appropriate there. In the Resemblio library, the page is user-facing
    and Lorem leaks across as visible junk text ("Lorem ipsum dolor sit
    amet"). We provide a small map of human-readable, brand-aware
    placeholders that read like a real navigation/section/article without
    pretending to be brand-authored copy.

    Brand display name (L-7 fix, Phase B 2026-06-03)
    ------------------------------------------------
    Pre-2026-06-03 ``pretty_brand`` was a naive ``slug.replace("-", " ").title()``
    which mis-rendered every brand whose canonical caps differ from
    title-case (``openai`` -> "Openai", ``read-cv`` -> "Read Cv",
    ``are-na`` -> "Are Na"). Now routed through ``app.brand_names.
    pretty_brand_name`` which maintains a slug -> canonical-display map.
    Unknown slugs still fall back to the title-case humanize so an
    organic row never raises.

    Category specialization (L-15 fix, Phase B 2026-06-03; L-18 phrasing
    polish 2026-06-03)
    ------------------------------------------------------
    When ``category_slug`` is supplied AND the slot is one of the
    title-family slots (``title`` / ``headline``), the preset emits a
    colon-joined ``{Brand}: {Title-Case Category}`` phrase (e.g.
    ``"Aeon: Buttons"``, ``"Apple: About & Team"``) so the category
    page's featured-card frame does not read as a copy of the brand-
    snapshot frame. The colon-eyebrow convention replaced the prior
    bare-juxtaposition pattern ("Apple about team") which read as broken
    English for slugs that were not natural possessive nouns. See
    ``_CATEGORY_DISPLAY_LABELS`` for the curated phrase map.
    ``category_slug`` is optional: callers that omit it (legacy tests,
    brand-canonical render path) get the previous brand-level title.

    Unknown placeholders fall back to a humanized version of the slot name
    (e.g. ``col_1_title`` -> "Col 1 Title") so the page still has visible,
    sensible text even for template slots we haven't enumerated.
    """
    pretty_brand = pretty_brand_name(brand_slug)
    # Category-specialized title (L-15 fix). Only fires when the caller
    # passes a category_slug AND the category is not the
    # FEATURED_SNAPSHOT class itself (which is by definition the
    # brand-level frame). For the brand-canonical page the indexer
    # composes EVERY class so each per-class rendered_html row gets its
    # own category-specialized title; the consumer of the brand-canonical
    # row is the brand-snapshot page which surfaces the snapshot-class
    # row only, so the brand-level "design snapshot" copy still ships
    # there.
    category_label = _category_display_label(category_slug)
    if name in {"title", "headline"} and category_label is not None:
        return f"{pretty_brand}{CATEGORY_TITLE_JOIN}{category_label}"
    presets: dict[str, str] = {
        # Generic copy slots
        "kicker": "Featured",
        "title": f"{pretty_brand} design snapshot",
        "headline": f"{pretty_brand} design snapshot",
        "dek": f"Inspired by {pretty_brand}'s design system. Code-bearing tokens, ready for your stack.",
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
        # News-list items (the NEWS_LIST template at
        # _scripts/templates.py:589-592 uses item_N_date / item_N_title /
        # item_N_dek slots). Pre-2026-06-03 these fell through to the
        # humanize-fallback and rendered as "Item 1 Title" / "Item 1 Dek"
        # / "Item 1 Date" in plain English on every brand canonical page;
        # the BLOCKER 1 fix for the URL-first flag-flip
        # (cto-reviews/2026-06-03-library-ux-audit.md) is to enumerate
        # them here so the page reads as a real "what shipped recently"
        # vignette rather than a stub. Dates are stable text rather than
        # live-clock values so render output is deterministic for tests
        # and visual-fidelity-check snapshots.
        "item_1_date": "MAR 12",
        "item_1_title": f"{pretty_brand} ships a refreshed palette",
        "item_1_dek": "New accent values land across the system, with dark-mode pairs.",
        "item_2_date": "MAR 05",
        "item_2_title": "Typography scale extends to display sizes",
        "item_2_dek": "Three new heading steps cover hero and editorial layouts.",
        "item_3_date": "FEB 22",
        "item_3_title": "Button tokens land for every variant",
        "item_3_dek": "Primary, secondary, outline, ghost, and destructive captured.",
        "item_4_date": "FEB 09",
        "item_4_title": "Spacing rhythm tightens at small viewports",
        "item_4_dek": "Compact rails replace the prior 24px default on mobile.",
        # Step-list slots (HOW_IT_WORKS template). Same fall-through
        # pattern; humanize would have produced "Step 1 Title".
        "step_1_title": "Capture",
        "step_1_dek": "We read the source page's computed styles end to end.",
        "step_2_title": "Normalize",
        "step_2_dek": "Tokens collapse into a single DTCG-compatible namespace.",
        "step_3_title": "Compose",
        "step_3_dek": "Every component re-renders against the captured tokens. Inspirado, no copiado.",
        "step_4_title": "Ship",
        "step_4_dek": "Export to your stack: Tailwind, CSS, Figma Variables, or JSON.",
        # Team-member slots (ABOUT_TEAM template). Generic role labels so
        # the page does not invent named people. Under the Inspirado-no-copiado
        # correction (Frank-locked 2026-06-04) the right-of-publicity boundary
        # keeps real photos out; generic role labels are the safe default
        # pending the Phase 3.2 avatar-policy gate.
        "member_1_name": "Design lead",
        "member_1_role": "Systems and tokens",
        "member_2_name": "Engineering lead",
        "member_2_role": "Component library",
        "member_3_name": "Product lead",
        "member_3_role": "Roadmap and review",
        "member_4_name": "Research lead",
        "member_4_role": "Audit and discovery",
        # Article-layout slots (ARTICLE_LAYOUT template). Brand-aware
        # specimen body for editorial pages; never reads as filler.
        # D19: "author" and "date" removed - al__byline div removed from
        # ARTICLE_LAYOUT_BODY so these slots are no longer rendered.
        "lead": (
            "A short editorial lead establishes the reading rhythm for the "
            "article and demonstrates how body type sets at the default size."
        ),
        "section_2_title": "Section heading",
        "section_2_body": (
            "Section bodies carry the reading work. The typography scale "
            "keeps line length comfortable while letting the heading lead."
        ),
        "pull_quote": "The system carries the reading work; the page stays out of the way.",
        "section_3_title": "Closing notes",
        "section_3_body": (
            "Closing sections recap the through-line and point to related "
            "components elsewhere in the system."
        ),
    }
    if name in presets:
        return presets[name]
    return name.replace("_", " ").strip().title() or name


def _ds_var_name(key: str) -> str:
    """Return the ``--ds-*`` CSS custom-property name for a token key.

    The DRL token parser (``scripts/seed_from_drl.py:parse_tokens_css``)
    captures the full token identifier including the ``ds-`` namespace
    prefix (its regex strips only the leading ``--``). Some token sources
    feed us keys that already start with ``ds-`` (e.g. ``ds-bg``); others
    feed bare keys (e.g. ``bg``, ``font_display``). Both must normalize
    to a single ``--ds-<name>`` form so DRL templates' ``var(--ds-bg)``
    references resolve. Without this guard, ``ds-bg`` would become
    ``--ds-ds-bg`` and every brand var would fall through to browser
    defaults (root cause of the 2026-06-02 library visual-fidelity audit).
    """
    normalized = key.replace("_", "-")
    if normalized.startswith("ds-"):
        return f"--{normalized}"
    return f"--ds-{normalized}"


def _emit_brand_root(tokens: dict[str, str]) -> str:
    """Render a complete ``:root`` block populated from the brand-token contract.

    Path C Phase 2 (per CTO sign-off
    ``projects/OptSus Team/cto-reviews/2026-06-03-resemblio-path-c-phase2-contract-signoff.md``):
    the DRL templates reference every visual decision through ``var(--ds-*)``
    slots backed by the central ``BRAND_TOKEN_CONTRACT``. This emitter
    guarantees EVERY contract slot has a value at the document root, so
    rendered pages always paint with a defined slot value rather than
    falling through to the in-line ``var()`` fallback in the template.

    For each slot:

    - If the brand's ``tokens`` dict supplies a value (matched via the
      same key-normalization rules as ``_ds_var_name``: underscores
      collapse to dashes; both ``bg`` and ``ds-bg`` map to ``--ds-bg``),
      that value wins.
    - Otherwise the slot's ``default`` from the contract is emitted.

    The wrapping selector is scoped to ``.rs-library-page`` so the brand
    root never leaks out of the article fragment when it lands inside
    the host Next.js page. Output is deterministic (slots sorted) so
    ``library_pages.rendered_html`` diffs stay reviewable.

    The shape returned is a single ``:root`` block; ``library_style_scope``
    rewrites the selector during ``scope_style_block`` so the rules apply
    only within the page article. Empty brand tokens still produce a
    populated block (every slot at contract default) which is the
    back-compat guarantee Path C inherits from Phase 1.
    """
    # Normalize the brand-supplied tokens to a {slot_name: value} map
    # using the same rules ``_ds_var_name`` applies, but stripped of the
    # leading ``--`` so the result keys match BRAND_TOKEN_CONTRACT keys.
    overrides: dict[str, str] = {}
    for raw_key, value in (tokens or {}).items():
        var_name = _ds_var_name(raw_key)  # "--ds-bg"
        slot_name = var_name[2:] if var_name.startswith("--") else var_name
        overrides[slot_name] = value

    contract_slots = set(BRAND_TOKEN_CONTRACT["slots"].keys())
    lines = [":root {"]
    # Contract slots first: every slot named in BRAND_TOKEN_CONTRACT emits
    # with brand-override-or-contract-default. This guarantees the
    # rendered page paints with a defined value for every templated slot.
    for slot_name in sorted(contract_slots):
        slot = BRAND_TOKEN_CONTRACT["slots"][slot_name]
        value = overrides.get(slot_name, slot["default"])
        lines.append(f"  --{slot_name}: {value};")
    # Pass-through any extra brand-supplied keys (e.g. ``ds-font-body``)
    # that are not yet contract slots. Keeps existing DRL templates that
    # reference ``var(--ds-font-body)`` resolving even though the
    # contract has not formally adopted font-family slots yet.
    extras = sorted(name for name in overrides if name not in contract_slots)
    for slot_name in extras:
        lines.append(f"  --{slot_name}: {overrides[slot_name]};")
    lines.append("}")
    return "\n".join(lines)


def _tokens_to_inline_css(tokens: dict[str, str]) -> str:
    """Compatibility alias for ``_emit_brand_root``.

    Retained so the pre-Phase-2 regression tests at
    ``tests/test_tokens_to_inline_css.py`` keep their import target. The
    contract is unchanged - both bare and already-namespaced keys map to
    a single ``--ds-<name>`` form - but the body is now contract-driven
    rather than a raw projection of the brand dict.
    """
    return _emit_brand_root(tokens)


def _metadata_for(
    class_name: str,
    *,
    brand_slug: str,
    tokens: dict[str, str],
    manifest: BrandCaptureManifest | None = None,
) -> dict[str, Any]:
    """Return the OG-image + page-copy metadata envelope.

    Subset of tokens (bg, surface, text, accent, font_display, font_body)
    plus the schema-version tag, and the Library v2 provenance fields:
    ``capture_manifest``, ``hub_capture_signal``, and ``missing_data_notice``.

    Args:
        class_name: template class name (e.g. 'buttons').
        brand_slug: canonical brand identifier.
        tokens: flat brand token dict from ``tokens_for_compose``.
        manifest: pre-built ``BrandCaptureManifest`` from ``_process_job``.
            When provided, the manifest is reused rather than recomputed,
            saving one ``build_capture_manifest`` call per template class
            (the manifest is per-brand, not per-class). When ``None`` (e.g.
            in unit tests that call ``_metadata_for`` directly), a fresh
            manifest is computed from ``tokens``.

    Key normalization mirrors the ``_tokens_to_inline_css`` /
    ``_ds_var_name`` fix shipped in commit ``066f503``: DRL-seeded brands
    arrive with already-namespaced (``ds-bg``, ``ds-font-body``) and/or
    underscored keys (``font_display``); organic rows arrive with bare
    keys (``bg``, ``font_body``). Without normalization the envelope
    returned ``None`` for every field on DRL-seeded input, which is the
    bug 11 cause from the 2026-06-02 failure trail. Look up each
    envelope field under both the bare and the ``ds-``-prefixed name.

    Library v2 fields (2026-06-07)
    --------------------------------
    ``capture_manifest`` carries the full per-group provenance signal so the
    render-real-or-hide decision (Phase 2) and the honest acknowledgment
    (Phase 3) can be driven from a single stored payload per page row.

    v2 shape (issue #11, 2026-06-20): each group dict now includes
    ``"provenance": "native" | "mined" | "synthesized-states" | "none"``
    alongside ``"captured"``. Mined synthetics pass their atom class via
    ``mined_atom_classes`` to ``build_capture_manifest`` before this function
    is called, so the provenance is already in ``manifest`` at write time.

    ``hub_capture_signal`` carries the coarse N-of-M count consumed by the
    hub card grid without requiring the full manifest deserialization.
    Mined groups count toward the captured_count (they are real components).

    ``missing_data_notice`` carries the structured missing-item list for
    the brand page's acknowledgment section. Mined groups do NOT appear
    in missing_items (they are captured, not absent). Stored alongside the
    page row so the web can render the notice without a separate API call.
    """
    def _lookup(field: str) -> str | None:
        # Underscore -> dash so ``font_display`` and ``font-display``
        # collapse to a single canonical form before the ds- prefix check.
        dashed = field.replace("_", "-")
        # Bare-key spelling first (organic rows), then DRL ds- spelling.
        value = tokens.get(field)
        if value is not None:
            return value
        value = tokens.get(dashed)
        if value is not None:
            return value
        return tokens.get(f"ds-{dashed}")

    # Use the caller-supplied manifest when available (avoids recomputing once
    # per template class when called from _process_job, which builds the
    # manifest once for the whole brand). Fall back to computing from tokens
    # for callers (unit tests) that invoke _metadata_for directly.
    _manifest = manifest if manifest is not None else build_capture_manifest(tokens)
    hub_signal = build_hub_capture_signal(_manifest)
    notice = build_missing_notice(_manifest)

    return {
        "schema_version": LIBRARY_PAGE_METADATA_SCHEMA_VERSION,
        "brand_slug": brand_slug,
        "category_slug": class_name,
        "bg": _lookup("bg"),
        "surface": _lookup("surface"),
        "text": _lookup("text"),
        "accent": _lookup("accent"),
        "font_display": _lookup("font_display"),
        "font_body": _lookup("font_body"),
        # Library v2 provenance fields (2026-06-07, plan Phase 4).
        # capture_manifest v2 (issue #11): added 'provenance' per group so
        # downstream consumers can distinguish native / mined / none capture.
        "capture_manifest": {
            "schema_version": _manifest["schema_version"],
            "groups": {
                group: {
                    "captured": detail["captured"],
                    "provenance": detail["provenance"],
                    "present_source_fields": list(detail["present_source_fields"]),
                    "absent_source_fields": list(detail["absent_source_fields"]),
                }
                for group, detail in _manifest["groups"].items()
            },
        },
        "hub_capture_signal": {
            "schema_version": hub_signal.schema_version,
            "captured_count": hub_signal.captured_count,
            "total_showcase_groups": hub_signal.total_showcase_groups,
        },
        "missing_data_notice": {
            "schema_version": notice.schema_version,
            "missing_items": [
                {"category_slug": item.category_slug, "display_name": item.display_name}
                for item in notice.missing_items
            ],
        },
    }


# ----------------------------------------------------------------------
# Canonical flag reconciliation
# ----------------------------------------------------------------------


def _reconcile_canonical(session: Session, asset_version: AssetVersion) -> None:
    """Set ``is_canonical`` per (brand_slug, category_slug) independently.

    For each category that exists for this brand, the canonical page is the
    best page in that category, ranked by:

      1. **Non-empty rendered_html first.** A real-content page always beats
         an empty placeholder. This is the issue-#31 fix: in the cross-category
         page model every whole asset_version writes a page for EVERY template
         class, and the classes it does not own render to an empty string. When
         a brand's real buttons whole and a sibling whole (e.g. a footer) carry
         the *same* ``fetched_at`` (common after a single corpus re-seed, where
         all wholes share one timestamp), a pure ``fetched_at`` ordering could
         crown the sibling's EMPTY buttons page canonical, so the live
         ``/library/<brand>/buttons`` page served blank. Ranking non-empty
         first guarantees the real component wins regardless of the tie.
      2. **Most recent ``fetched_at``.** Among equally-non-empty pages, the
         newest asset_version wins (the genuine version-flip behaviour).
      3. **Highest ``asset_version.id``.** A deterministic final tiebreak so
         the winner is stable across re-runs when (1) and (2) tie.

    This per-category approach is required to handle mined synthetic
    asset_versions correctly: a mined synthetic (e.g. apple/buttons) writes
    only ONE category page. If reconcile were per-brand (choosing one winner
    asset_version for the whole brand), the mined synthetic's later
    ``fetched_at`` would cause the whole's cta-blocks page to lose canonical
    status even though no mined page competes for that category.  Per-category
    reconcile avoids that regression: each category is settled independently.

    Run after pages are written so the just-inserted rows participate in
    the comparison.
    """
    brand_slug = derive_brand_slug(asset_version.url)

    # All distinct categories for this brand in library_pages.
    categories: list[str] = session.execute(
        select(LibraryPage.category_slug)
        .where(LibraryPage.brand_slug == brand_slug)
        .distinct()
    ).scalars().all()

    # Non-empty content ranks above empty placeholders. Use an explicit
    # integer ``case`` (1 for content, 0 for empty) rather than ordering by a
    # raw boolean expression: boolean ORDER BY is not portably sortable across
    # dialects, whereas integer DESC reliably puts content (1) ahead of empty
    # (0). ``rendered_html`` is never NULL (the indexer writes ``""`` for
    # omitted categories), so func.length is always defined.
    _has_content = case((func.length(LibraryPage.rendered_html) > 0, 1), else_=0)

    for category_slug in categories:
        # Best asset_version for this (brand, category): real content first,
        # then most recent, then highest id for a deterministic tiebreak.
        winner_id = session.execute(
            select(LibraryPage.asset_version_id)
            .join(AssetVersion, AssetVersion.id == LibraryPage.asset_version_id)
            .where(LibraryPage.brand_slug == brand_slug)
            .where(LibraryPage.category_slug == category_slug)
            .order_by(
                _has_content.desc(),
                AssetVersion.fetched_at.desc(),
                AssetVersion.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if winner_id is None:
            continue
        # Flip the winner canonical for this (brand, category).
        session.execute(
            update(LibraryPage)
            .where(LibraryPage.brand_slug == brand_slug)
            .where(LibraryPage.category_slug == category_slug)
            .where(LibraryPage.asset_version_id == winner_id)
            .values(is_canonical=True)
        )
        # Flip every other page for this (brand, category) non-canonical.
        session.execute(
            update(LibraryPage)
            .where(LibraryPage.brand_slug == brand_slug)
            .where(LibraryPage.category_slug == category_slug)
            .where(LibraryPage.asset_version_id != winner_id)
            .values(is_canonical=False)
        )


# ----------------------------------------------------------------------
# Mined-atom marker helper
# ----------------------------------------------------------------------


def _mined_atom_class(dtcg: dict[str, object]) -> str | None:
    """Return the mined atom class name if this asset_version is a mined synthetic.

    Mined asset_versions carry ``dtcg["mined_atom_class"]`` set to the single
    atom class they serve (e.g. ``"buttons"``).  This is the D2 guard key: the
    indexer restricts its class loop to this one class so a mined synthetic
    can never overwrite the canonical status of other category pages (e.g.
    ``cta-blocks``) that belong to the brand's real captured wholes.

    Returns ``None`` for all other asset_versions (key absent or empty string).
    Using a dedicated key rather than overloading ``version_label`` keeps
    control flow out of string parsing and makes this function unit-testable
    in isolation.

    Args:
        dtcg: The ``dtcg_json`` payload from an ``asset_versions`` row.

    Returns:
        The atom class name string, or ``None`` if the marker is absent/empty.
    """
    val = dtcg.get("mined_atom_class")
    return str(val) if val else None


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
    # Hybrid Path B button-fidelity override (CTO 2026-06-02). One disk
    # read per asset version (None for the common no-snapshot case).
    button_tokens = _load_button_tokens(brand_slug)
    # D2 guard (issue #28): mined synthetic asset_versions must compose exactly
    # one page - their atom_class.  If the full _all_template_classes() loop
    # ran for a mined synthetic, its later fetched_at could cause the per-brand
    # reconcile to demote the whole's real pages (e.g. cta-blocks) from canonical.
    # _mined_atom_class returns the single class name when the marker is present,
    # or None for all other asset_versions (no change to their behaviour).
    # Computed BEFORE build_capture_manifest so mined provenance is reflected
    # in the manifest stored with the page (issue #11).
    dtcg_json = asset_version.dtcg_json or {}
    mined_class = _mined_atom_class(dtcg_json)
    # Library v2 D2 gate: compute the manifest ONCE per brand (not per class).
    # _compose_with_gate uses it to decide whether each showcase category
    # should be composed (captured) or omitted (empty string, not fabricated).
    # _metadata_for receives the same manifest so it does not recompute.
    # mined_atom_classes (issue #11): when the asset is a mined synthetic, its
    # atom class is passed so the manifest records honest "mined" provenance for
    # that group. Whole asset_versions pass an empty frozenset (no change).
    brand_manifest = build_capture_manifest(
        tokens,
        button_tokens=button_tokens,
        mined_atom_classes=frozenset({mined_class}) if mined_class else frozenset(),
    )
    # Real-component lookup (issue #3): fetch the stored DRL component ONCE
    # per asset_version before the class loop. The seed pipeline (#2) writes
    # one asset_components row per asset; the indexer serves that row for the
    # one page whose class_name matches dtcg["class"]. All other pages use
    # the existing generic-template path. None when the asset has no component
    # row or dtcg carries no class key (e.g. organic extractions pre-#2).
    dtcg_class: str | None = dtcg_json.get("class")
    real_component = get_asset_component(session, asset_version.id) if dtcg_class else None
    classes_to_compose: tuple[str, ...] = (
        (mined_class,) if mined_class else _all_template_classes()
    )
    written = 0
    for class_name in classes_to_compose:
        rendered = _compose_with_gate(
            class_name,
            brand_slug=brand_slug,
            tokens=tokens,
            button_tokens=button_tokens,
            manifest=brand_manifest,
            dtcg_class=dtcg_class,
            real_component=real_component,
        )
        metadata = _metadata_for(class_name, brand_slug=brand_slug, tokens=tokens, manifest=brand_manifest)
        page = LibraryPage(
            asset_version_id=asset_version.id,
            category_slug=class_name,
            brand_slug=brand_slug,
            # Slugify here, not at read-time: ``asset_versions.version_label``
            # is a human-readable string (e.g. "DRL bootstrap 2026-05-21");
            # ``library_pages.version_label`` must be URL-safe so the
            # downstream /library/<brand>/<version>/... route resolves.
            version_label=slugify_version_label(asset_version.version_label),
            rendered_html=rendered,
            metadata_json=metadata,
            is_canonical=False,
        )
        # Wrap the speculative INSERT in a SAVEPOINT so a UNIQUE collision
        # rolls back ONLY this iteration's insert, not the surrounding
        # transaction. The previous implementation called session.rollback()
        # in the except branch, which wiped every prior iteration's UPDATE
        # in the same loop (L-17 regression root cause 2026-06-03: alphabet
        # ran early, its UPDATE was flushed, then a later template's
        # IntegrityError rolled the whole transaction back including the
        # alphabet UPDATE). Nested transactions (SAVEPOINTs) scope the
        # rollback to the failing statement only.
        sp = session.begin_nested()
        session.add(page)
        try:
            session.flush()
            sp.commit()
            written += 1
        except IntegrityError:
            # UNIQUE(asset_version_id, category_slug) trip: the row already
            # exists from a prior run. The contract is idempotent. Pre-2026-06-03
            # this branch simply rolled back and skipped, which silently froze
            # any row that had been written under an older template or compose
            # pipeline (L-17 root cause: brands whose alphabet row was written
            # by an earlier indexer version with no substantive body markup
            # stayed empty forever because every subsequent re-enqueue hit this
            # path and bailed). Self-heal by UPDATE-ing rendered_html +
            # metadata_json in place so a re-enqueue of an already-indexed
            # asset_version actually refreshes stale content.
            sp.rollback()
            existing = session.execute(
                select(LibraryPage)
                .where(LibraryPage.asset_version_id == asset_version.id)
                .where(LibraryPage.category_slug == class_name)
            ).scalar_one_or_none()
            if existing is not None:
                existing.rendered_html = rendered
                existing.metadata_json = metadata
                existing.brand_slug = brand_slug
                existing.version_label = slugify_version_label(
                    asset_version.version_label
                )
                session.flush()
                written += 1
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
