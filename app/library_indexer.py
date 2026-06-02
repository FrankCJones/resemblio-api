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
import sys
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
from app.models import AssetVersion, Extraction, LibraryIndexJob, LibraryPage


logger = logging.getLogger("resemblio.library_indexer")


# Ensure the DRL ``_scripts`` package is importable. The seed script
# (``scripts/seed_from_drl.py``) uses the same workspace-relative resolution;
# we mirror it here so the indexer composes from the same templates the seed
# corpus was authored against. On a deployed box ``DRL_SCRIPTS_PATH`` can be
# overridden via env (deferred; v1.1 ships with the workspace path baked in
# at module load).
# ``app/library_indexer.py`` -> parents[0]=app, [1]=code/api, [2]=code,
# [3]=Resemblio, [4]=projects. The DRL repo sits at
# ``projects/Design Reference Library``. The ``_scripts`` package inside DRL
# is what we import from; adding the DRL ROOT (not _scripts/) to sys.path
# lets ``from _scripts.compose import ...`` resolve cleanly.
_API_FILE = Path(__file__).resolve()
_PROJECTS_ROOT = _API_FILE.parents[4]
_DRL_ROOT = _PROJECTS_ROOT / "Design Reference Library"
if _DRL_ROOT.exists() and str(_DRL_ROOT) not in sys.path:
    sys.path.insert(0, str(_DRL_ROOT))

# The API's ``extractor_bridge`` imports the vendored ``_scripts`` package
# (``_vendored/drl/drl/_scripts``) at module load. That cached package object
# does NOT carry ``templates.py``, ``compose.py``, or ``slate.py`` — those
# DRL modules live only in the workspace DRL tree. If we leave the cached
# ``sys.modules['_scripts']`` untouched, ``from _scripts.templates import X``
# below resolves against the vendored package and raises
# ``ModuleNotFoundError``. Extending the cached package's ``__path__`` with
# the workspace ``_scripts/`` directory lets Python's import machinery find
# the missing submodules without disturbing the vendored modules already
# loaded under the same parent. No-op if the vendored package was never
# loaded (workspace-only test runs).
_WORKSPACE_SCRIPTS_DIR = _DRL_ROOT / "_scripts"
if _WORKSPACE_SCRIPTS_DIR.exists():
    _scripts_pkg = sys.modules.get("_scripts")
    if _scripts_pkg is not None and hasattr(_scripts_pkg, "__path__"):
        _workspace_scripts_text = str(_WORKSPACE_SCRIPTS_DIR)
        if _workspace_scripts_text not in list(_scripts_pkg.__path__):
            _scripts_pkg.__path__.append(_workspace_scripts_text)


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
    """Render the per-class HTML for a brand snapshot.

    Wraps ``compose.render_html`` with the minimal ``section`` shape it
    needs (empty ``content_samples`` so it falls back to DEFAULT_CONTENT,
    plus ``pattern_tags`` and ``notes`` filled with placeholders the
    skeleton requires). The returned string is the full asset.html.
    """
    from _scripts import compose  # local import: avoid cycle if DRL absent

    section: dict[str, Any] = {
        "content_samples": {},
        "pattern_tags": [],
        "notes": [],
        "inspired_by": [],
    }
    return compose.render_html(
        slug=brand_slug,
        class_folder=class_name,
        section=section,
        template_class=class_name,
    )


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
