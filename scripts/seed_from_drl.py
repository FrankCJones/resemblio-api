"""Bulk-seed the Resemblio ``extractions`` table from the Design Reference Library.

Usage
-----
::

    # Dry-run (default): print the plan, write nothing
    python -m scripts.seed_from_drl

    # Apply: actually write to Postgres + R2
    python -m scripts.seed_from_drl --apply

    # Bounded: stop after N rows (for smoke tests)
    python -m scripts.seed_from_drl --apply --limit 25

    # Subset: only one DRL system
    python -m scripts.seed_from_drl --apply --source-system anthropic

Dependencies
------------
- ``RESEMBLIO_DB_URL`` env var (read by ``app.config.get_settings``)
- ``RESEMBLIO_KEY_PEPPER`` env var (required by the same settings loader; the
  seed script does not hash keys itself but the app module imports require it)
- R2 credentials in ``app.config.Settings`` (only consulted in ``--apply``
  mode; dry-run skips storage entirely)
- ``projects/Design Reference Library/corpus.json`` reachable on disk via the
  ``--drl-root`` flag (default: workspace-relative)

Design reference: ``scripts/SEED_FROM_DRL_DESIGN.md``.

This script is written to run unattended; all user-facing output uses
``logging`` rather than ``print``. Idempotency is anchored on the
``(seed_source, source_id)`` partial unique index added by migration
``0007_extractions_seed_source``; re-running with the same arguments is safe
and produces no duplicate rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol, TypedDict
from zipfile import ZIP_DEFLATED, ZipFile

# The seed script lives at ``code/api/scripts/seed_from_drl.py``. Adding the
# API root to ``sys.path`` lets ``python -m scripts.seed_from_drl`` resolve
# ``app.*`` and the vendored top-level ``transformer`` package. ``transformer``
# was vendored into the API repo on 2026-05-31 so CI (which checks out only
# this repo) can import it without a sibling checkout. The upstream copy at
# ``projects/Resemblio/code/transformer/`` remains the source of truth; sync
# convention is documented in ``projects/Resemblio/Resemblio_INFRA.md`` under
# "Vendored transformer package".
_API_ROOT = Path(__file__).resolve().parents[1]
_path_text = str(_API_ROOT)
if _path_text not in sys.path:
    sys.path.insert(0, _path_text)

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import SCHEMA_V1
from transformer import STRIPPED_SCHEMA_VERSION, StrippedEntry, brand_strip

# NOTE: ``app.db`` and ``app.models`` are intentionally NOT imported at module
# scope. Importing ``app.db`` calls ``create_engine`` against the configured
# Postgres URL eagerly, and even though ``create_engine`` itself does not open
# a connection, the prior code path then called ``SessionLocal()`` from dry-run
# and immediately issued a SELECT - which timed out when the script was run
# from a machine that cannot reach prod Postgres. Dry-run must be safe to run
# anywhere with zero network. See the lazy imports inside ``find_existing``,
# ``upsert_extraction``, and ``main`` (apply branch only).


LOG = logging.getLogger("seed_from_drl")
# Explicit propagation guarantees that records this script emits flow up to
# the root logger. Pytest's ``caplog`` plugin attaches its capture handler to
# root; without propagation it would never see the script's INFO/WARNING
# records and tests asserting on log output would silently fail.
LOG.propagate = True

# --- Named constants (workspace quality floor) -------------------------------
SEED_SOURCE_DRL_V1 = "drl_v1"
"""Marker stored in ``extractions.seed_source`` for DRL bulk-seed rows."""

DRL_BOOTSTRAP_USER_ID: int | None = None
"""Audit-field value written to ``asset_versions.first_extracted_by_user_id``
for DRL bootstrap rows. NULL signals "not an organic user extraction" so the
library indexer + downstream audit queries can cleanly separate the
bootstrap corpus from real per-user activity. The ``extractions`` row keeps
its FK to a real user (``--seed-user-id``, default 1) because that column is
NOT NULL; the audit-trail distinction lives on the asset_versions side."""

DRL_VERSION_LABEL_PREFIX = "DRL bootstrap"
"""Prefix for ``asset_versions.version_label`` on DRL-seeded rows. The full
label is ``"{prefix} {captured_date}"`` where ``captured_date`` is the
top-level ``corpus.json:generated`` field (ISO date, e.g. ``2026-05-21``).
Format: ``"DRL bootstrap 2026-05-21"``. Library timeline views group on the
prefix; downstream sort uses the trailing date."""

DEFAULT_BATCH_SIZE = 25
"""Rows per DB transaction. The R2 PUT happens outside the transaction (S3
has no two-phase commit); on partial failure the next dry-run reconciles."""

DEFAULT_DRL_ROOT = (
    Path(__file__).resolve().parents[4] / "Design Reference Library"
)
"""Workspace-relative default. Override with ``--drl-root``."""

R2_KEY_TEMPLATE = "seed/drl/{source_id}.zip"
"""Per-asset R2 object key. ``source_id`` contains slashes so the key
naturally nests under ``seed/drl/<system>/<class>/<slug>.zip``."""

# DRL ``tokens.css`` files declare CSS custom properties under ``:root``.
# This regex captures ``--name: value`` pairs across line breaks.
_CSS_VAR_PATTERN = re.compile(r"--([a-zA-Z0-9_-]+)\s*:\s*([^;]+);")


class DrlAssetDict(TypedDict, total=False):
    """One ``assets[*]`` entry inside ``corpus.json``."""

    slug: str
    cls: str
    kind: str
    path: str
    tokens_path: str
    tldr: str
    patterns: list[str]
    mood: list[str]
    applicable_to: list[str]
    tags: list[str]
    provenance_score: str


class DrlSystemDict(TypedDict, total=False):
    """One ``systems[*]`` entry inside ``corpus.json``."""

    slug: str
    name: str
    tier: str
    category: str
    asset_count: int
    assets: list[DrlAssetDict]


class SeedPlanRow(TypedDict):
    """Dry-run plan row. One per DRL asset the seeder would touch."""

    source_id: str
    operation: str  # "insert" | "update" | "skip"
    tokens_bytes: int
    zip_bytes: int
    tokens_count: int
    r2_key: str


@dataclass(frozen=True)
class SeedBundle:
    """Per-asset payload ready for DB insert + R2 upload."""

    source_id: str
    tokens_json: dict[str, Any]
    dtcg_json: dict[str, Any]
    zip_bytes: bytes
    zip_sha256: str


class StorageClient(Protocol):
    """Subset of ``app.storage.R2Storage`` the seeder needs.

    Tests pass an in-memory fake conforming to this protocol; production
    code passes the real ``R2Storage`` instance.
    """

    def put_object_at_key(self, key: str, body: bytes, content_type: str) -> None:
        """Upload ``body`` to the given object ``key``."""
        ...


# --- DRL corpus loading + tokens.css parsing ---------------------------------

def load_corpus(drl_root: Path) -> dict[str, Any]:
    """Read ``corpus.json`` from the DRL root.

    Raises ``FileNotFoundError`` with a clear message when the path is wrong;
    that is the seed script's most common operator error.
    """
    corpus_path = drl_root / "corpus.json"
    if not corpus_path.exists():
        raise FileNotFoundError(
            f"DRL corpus.json not found at {corpus_path!s}. Pass --drl-root."
        )
    with corpus_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def iter_assets(corpus: dict[str, Any]) -> Iterator[tuple[DrlSystemDict, DrlAssetDict]]:
    """Yield every ``(system, asset)`` pair in the DRL corpus."""
    for system in corpus.get("systems", []) or []:
        for asset in system.get("assets", []) or []:
            yield system, asset


def parse_tokens_css(css_text: str) -> dict[str, str]:
    """Extract ``--name: value`` declarations from a DRL ``tokens.css`` blob.

    Returns a flat dict keyed by the declaration name (with the leading
    ``--`` stripped). Values are stripped of leading and trailing whitespace.
    Comments and at-rules are ignored. Duplicate names take the last value
    seen, mirroring CSS cascade behaviour at the same specificity.
    """
    tokens: dict[str, str] = {}
    for match in _CSS_VAR_PATTERN.finditer(css_text):
        name = match.group(1).strip()
        value = match.group(2).strip()
        tokens[name] = value
    return tokens


def load_tokens_for_asset(drl_root: Path, asset: DrlAssetDict) -> dict[str, str]:
    """Load and parse the ``tokens.css`` file for one asset.

    Returns an empty dict if the asset has no ``tokens_path`` or the file is
    missing on disk; the caller decides whether that is fatal. The seed
    script logs and skips such rows by default.
    """
    tokens_rel = asset.get("tokens_path")
    if not tokens_rel:
        return {}
    tokens_path = drl_root / tokens_rel
    if not tokens_path.exists():
        return {}
    return parse_tokens_css(tokens_path.read_text(encoding="utf-8"))


def load_system_json(drl_root: Path, brand_slug: str) -> dict[str, Any] | None:
    """Read ``systems/<brand_slug>/system.json`` from the DRL root.

    Returns the parsed JSON dict when the file exists, or ``None`` when it is
    absent. The caller treats ``None`` as "curated metadata not yet authored
    for this brand" and simply omits the optional fields from the bundle.

    Why a separate file from ``corpus.json``?
    ``corpus.json`` is the flat catalogue used for asset iteration; it carries
    ``tier`` and ``category`` but NOT ``design_principles`` or
    ``commercial_signal`` (those are authored per-system on the DRL side and
    live only in the per-system ``system.json``). This function is the seam
    that bridges both sources.

    Args:
        drl_root: Filesystem root of the Design Reference Library checkout.
        brand_slug: DRL system slug (e.g. ``"linear"``, ``"stripe"``).

    Returns:
        Parsed ``system.json`` dict, or ``None`` if the file does not exist.
    """
    system_path = drl_root / "systems" / brand_slug / "system.json"
    if not system_path.exists():
        return None
    return json.loads(system_path.read_text(encoding="utf-8"))


# --- Bundle assembly (mirrors ``app.extractor_bridge.bundle_from_token_set``) -

def build_bundle(
    stripped: StrippedEntry,
    tokens: dict[str, str],
    *,
    design_principles: list[str] | None = None,
    commercial_signal: str | None = None,
) -> SeedBundle:
    """Construct the per-asset bundle persisted to Postgres + R2.

    Mirrors the structure produced for organic extractions
    (``app.extractor_bridge.bundle_from_token_set``): a tokens dict, a DTCG
    JSON envelope, and a ZIP carrying ``tokens.json`` + ``manifest.json``.
    Seeded rows additionally embed the stripped entry's metadata so the
    public corpus surfaces the design-behaviour fields (``patterns``,
    ``mood``, ``applicable_to``, ``tags``) the DRL curated.

    Phase 3 (2026-06-08): ``tier`` and ``category`` from ``StrippedEntry``,
    plus ``design_principles`` and ``commercial_signal`` from
    ``systems/<slug>/system.json``, are now embedded in ``dtcg_json`` so the
    library route layer can surface them without a separate DB lookup.

    Args:
        stripped: Brand-stripped DRL entry (from ``brand_strip``).
        tokens: Flat ``{name: value}`` token dict (from ``load_tokens_for_asset``).
        design_principles: Optional list from ``system.json``; omitted when None.
        commercial_signal: Optional string from ``system.json``; omitted when None.
    """
    tokens_json: dict[str, Any] = dict(tokens)
    dtcg_json: dict[str, Any] = {
        "schema_version": SCHEMA_V1,
        "transformer_schema_version": STRIPPED_SCHEMA_VERSION,
        "slug": stripped.slug,
        "class": stripped.cls,
        "kind": stripped.kind,
        "tldr": stripped.tldr,
        "patterns": list(stripped.patterns),
        "mood": list(stripped.mood),
        "applicable_to": list(stripped.applicable_to),
        "tags": list(stripped.tags),
        # Curated metadata (Phase 3): sourced from StrippedEntry + system.json.
        # ``tier`` and ``category`` always present (corpus.json carries them).
        # ``design_principles`` and ``commercial_signal`` present only when
        # system.json was found on disk; omitting the key (rather than storing
        # None) lets consumers distinguish "not authored yet" from "empty list".
        "tier": stripped.tier,
        "category": stripped.category,
        "tokens": tokens_json,
    }
    if design_principles is not None:
        dtcg_json["design_principles"] = design_principles
    if commercial_signal is not None:
        dtcg_json["commercial_signal"] = commercial_signal

    tokens_bytes = json.dumps(dtcg_json, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest = {
        "schema_version": SCHEMA_V1,
        "seed_source": SEED_SOURCE_DRL_V1,
        "source_id": stripped.source_id,
        "tier": stripped.tier,
        "category": stripped.category,
        "provenance_score": stripped.provenance_score,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "tokens_sha256": hashlib.sha256(tokens_bytes).hexdigest(),
    }
    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, "w", ZIP_DEFLATED) as zip_file:
        zip_file.writestr("tokens.json", json.dumps(dtcg_json, indent=2, sort_keys=True))
        zip_file.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    zip_bytes = zip_buffer.getvalue()
    return SeedBundle(
        source_id=stripped.source_id,
        tokens_json=tokens_json,
        dtcg_json=dtcg_json,
        zip_bytes=zip_bytes,
        zip_sha256=hashlib.sha256(zip_bytes).hexdigest(),
    )


# --- DB UPSERT ---------------------------------------------------------------

def find_existing(session: Session, source_id: str) -> "Extraction | None":  # noqa: F821 - lazy import
    """Return the existing seed row for ``source_id``, if any.

    Imports ``app.models.Extraction`` lazily so dry-run code paths that pass
    ``session=None`` never touch the DB layer or its dependencies.
    """
    from app.models import Extraction  # local import: dry-run safety

    statement = select(Extraction).where(
        Extraction.seed_source == SEED_SOURCE_DRL_V1,
        Extraction.source_id == source_id,
    )
    return session.execute(statement).scalar_one_or_none()


def upsert_extraction(
    session: Session,
    user_id: int,
    stripped: StrippedEntry,
    bundle: SeedBundle,
    r2_zip_key: str,
    captured_date: str,
) -> tuple["Extraction", str]:  # noqa: F821 - lazy import
    """Insert a new seed row or update an existing one in place.

    Returns the ``(row, operation)`` tuple where ``operation`` is ``"insert"``
    or ``"update"``. The session is flushed but not committed; the caller
    batches commits.

    The ``asset_versions`` row carries the DRL bootstrap audit shape:

    - ``is_public=True`` so the library indexer (mission Phase 4) picks
      bootstrap entries up on its first run without a moderation step.
    - ``version_label="DRL bootstrap {captured_date}"`` so the timeline
      view distinguishes the corpus bootstrap from organic re-extractions.
    - ``first_extracted_by_user_id=None`` so the audit trail does not
      attribute the bootstrap corpus to the ``--seed-user-id`` operator.
    """
    from app.asset_versions import insert_or_reuse_asset_version
    from app.library_indexer import enqueue_for_asset_version
    from app.models import Extraction  # local import: dry-run safety

    existing = find_existing(session, stripped.source_id)
    public_url = f"resemblio://seed/{SEED_SOURCE_DRL_V1}/{stripped.source_id}"
    asset_version = insert_or_reuse_asset_version(
        session,
        url=public_url,
        dtcg=bundle.dtcg_json,
        first_extracted_by_user_id=DRL_BOOTSTRAP_USER_ID,
        manifest_schema_version=SCHEMA_V1,
        is_public=True,
        version_label=f"{DRL_VERSION_LABEL_PREFIX} {captured_date}",
    )
    if existing is None:
        row = Extraction(
            user_id=user_id,
            api_key_id=None,
            url=public_url,
            url_normalized=public_url,
            status="ok",
            tokens_json=bundle.tokens_json,
            asset_version_id=asset_version.id,
            r2_zip_key=r2_zip_key,
            zip_sha256=bundle.zip_sha256,
            schema_version=SCHEMA_V1,
            credit_cents=0,
            seed_source=SEED_SOURCE_DRL_V1,
            source_id=stripped.source_id,
        )
        session.add(row)
        session.flush()
        # Enqueue this asset_version for the library indexer (mission Phase 4).
        # Idempotent: a no-op if a pending/running job already exists. Failures
        # here MUST NOT abort the seed transaction; the indexer can also be
        # backfilled by an operator running ``python -m app.cli.library_indexer``
        # on demand if the enqueue ever drops a row.
        try:
            enqueue_for_asset_version(session, asset_version.id)
        except Exception as enqueue_exc:  # noqa: BLE001 - log and continue
            LOG.warning(
                "enqueue_for_asset_version failed for source_id=%s: %r",
                stripped.source_id, enqueue_exc,
            )
        return row, "insert"

    existing.tokens_json = bundle.tokens_json
    existing.asset_version_id = asset_version.id
    existing.r2_zip_key = r2_zip_key
    existing.zip_sha256 = bundle.zip_sha256
    existing.schema_version = SCHEMA_V1
    session.flush()
    try:
        enqueue_for_asset_version(session, asset_version.id)
    except Exception as enqueue_exc:  # noqa: BLE001 - log and continue
        LOG.warning(
            "enqueue_for_asset_version failed for source_id=%s: %r",
            stripped.source_id, enqueue_exc,
        )
    return existing, "update"


# --- Orchestration -----------------------------------------------------------

@dataclass(frozen=True)
class SeedArgs:
    """Parsed CLI arguments for the seed script."""

    apply: bool
    drl_root: Path
    limit: int | None
    source_system: str | None
    seed_user_id: int
    batch_size: int


def parse_args(argv: list[str] | None = None) -> SeedArgs:
    """Parse argv into a ``SeedArgs``. Dry-run is the default."""
    parser = argparse.ArgumentParser(description="Bulk-seed DRL into the Resemblio extractions table.")
    parser.add_argument("--apply", action="store_true", help="Actually write. Default is dry-run.")
    parser.add_argument("--drl-root", type=Path, default=DEFAULT_DRL_ROOT, help="Path to the DRL folder.")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N assets (smoke testing).")
    parser.add_argument(
        "--source-system",
        type=str,
        default=None,
        help="Only seed assets from this DRL system slug (e.g. 'anthropic').",
    )
    parser.add_argument(
        "--seed-user-id",
        type=int,
        default=1,
        help="User id that owns seed rows. Defaults to 1 (the bootstrap user).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per DB transaction (default {DEFAULT_BATCH_SIZE}).",
    )
    namespace = parser.parse_args(argv)
    return SeedArgs(
        apply=bool(namespace.apply),
        drl_root=Path(namespace.drl_root).resolve(),
        limit=namespace.limit,
        source_system=namespace.source_system,
        seed_user_id=int(namespace.seed_user_id),
        batch_size=int(namespace.batch_size),
    )


def filter_assets(
    pairs: Iterable[tuple[DrlSystemDict, DrlAssetDict]],
    source_system: str | None,
    limit: int | None,
) -> Iterator[tuple[DrlSystemDict, DrlAssetDict]]:
    """Apply ``--source-system`` and ``--limit`` filters in stream order."""
    emitted = 0
    for system, asset in pairs:
        if source_system and system.get("slug") != source_system:
            continue
        yield system, asset
        emitted += 1
        if limit is not None and emitted >= limit:
            return


def plan_only(
    pairs: Iterable[tuple[DrlSystemDict, DrlAssetDict]],
    drl_root: Path,
    session: Session | None,
) -> list[SeedPlanRow]:
    """Build the dry-run plan. Writes nothing to DB or R2.

    If ``session`` is provided, classifies each row as ``"insert"`` vs
    ``"update"`` by querying the existing seed-source rows. If ``session``
    is None, every row is classified as ``"insert"`` (the bootstrap case).
    """
    rows: list[SeedPlanRow] = []
    for system, asset in pairs:
        try:
            stripped = brand_strip(system, asset)
        except ValueError as exc:
            LOG.warning("skipping malformed DRL row: %s", exc)
            continue
        tokens = load_tokens_for_asset(drl_root, asset)
        if not tokens:
            LOG.warning("skipping %s: no tokens.css on disk", stripped.source_id)
            continue
        bundle = build_bundle(stripped, tokens)
        operation = "insert"
        if session is not None and find_existing(session, stripped.source_id) is not None:
            operation = "update"
        rows.append(
            SeedPlanRow(
                source_id=stripped.source_id,
                operation=operation,
                tokens_bytes=len(json.dumps(bundle.tokens_json)),
                zip_bytes=len(bundle.zip_bytes),
                tokens_count=len(bundle.tokens_json),
                r2_key=R2_KEY_TEMPLATE.format(source_id=stripped.source_id),
            )
        )
    return rows


DEFAULT_CAPTURED_DATE = "unknown"
"""Fallback ``captured_date`` value when ``corpus.json`` lacks a ``generated``
field. Real DRL corpora always have one; the fallback exists so tests with
truncated synthetic corpora do not crash."""


def apply_seed(
    pairs: Iterable[tuple[DrlSystemDict, DrlAssetDict]],
    drl_root: Path,
    session: Session,
    storage: StorageClient,
    seed_user_id: int,
    batch_size: int,
    captured_date: str = DEFAULT_CAPTURED_DATE,
) -> dict[str, int]:
    """Execute the bulk seed against the DB and R2.

    Commits the SQL transaction every ``batch_size`` rows. R2 PUTs run
    one-per-row inside the loop but outside the SQL transaction; on partial
    failure the next dry-run reconciles and re-running is safe under the
    partial unique index.

    Returns a counts dict: ``{"inserted": int, "updated": int, "skipped": int}``.
    """
    counts = {"inserted": 0, "updated": 0, "skipped": 0}
    batch_since_commit = 0
    # Cache system.json loads so a multi-asset brand (e.g. a brand with
    # alphabets + buttons + wholes) only reads disk once per slug.
    _system_json_cache: dict[str, dict[str, Any] | None] = {}
    for system, asset in pairs:
        try:
            stripped = brand_strip(system, asset)
        except ValueError as exc:
            LOG.warning("skipping malformed DRL row: %s", exc)
            counts["skipped"] += 1
            continue
        tokens = load_tokens_for_asset(drl_root, asset)
        if not tokens:
            LOG.warning("skipping %s: no tokens.css on disk", stripped.source_id)
            counts["skipped"] += 1
            continue
        # Load system.json for curated metadata (Phase 3 - Gap C fix).
        # The brand slug is the first segment of the source_id path (e.g.
        # "linear/buttons/linear-btn-001" -> "linear"). Using the system dict
        # directly is safer than parsing source_id, and the slug is always
        # present in a valid DRL system dict.
        brand_slug = str(system.get("slug") or "")
        if brand_slug not in _system_json_cache:
            _system_json_cache[brand_slug] = load_system_json(drl_root, brand_slug)
        system_meta = _system_json_cache[brand_slug] or {}
        raw_dp = system_meta.get("design_principles")
        design_principles: list[str] | None = (
            [str(p) for p in raw_dp] if isinstance(raw_dp, list) else None
        )
        raw_cs = system_meta.get("commercial_signal")
        commercial_signal: str | None = (
            str(raw_cs) if isinstance(raw_cs, str) and raw_cs else None
        )
        bundle = build_bundle(
            stripped, tokens,
            design_principles=design_principles,
            commercial_signal=commercial_signal,
        )
        r2_key = R2_KEY_TEMPLATE.format(source_id=stripped.source_id)
        storage.put_object_at_key(r2_key, bundle.zip_bytes, "application/zip")
        _row, operation = upsert_extraction(
            session, seed_user_id, stripped, bundle, r2_key, captured_date
        )
        counts["inserted" if operation == "insert" else "updated"] += 1
        batch_since_commit += 1
        if batch_since_commit >= batch_size:
            session.commit()
            batch_since_commit = 0
            LOG.info("committed batch; running totals: %s", counts)
    if batch_since_commit:
        session.commit()
    return counts


# --- Storage adapter (wraps app.storage.R2Storage for the seeder) ------------

class _R2SeedAdapter:
    """Wrap ``app.storage.R2Storage`` to match ``StorageClient`` shape.

    The production R2 client is keyed by ``(user_id, extraction_id)``; the
    seeder needs a direct ``put_object_at_key`` so the key derives from the
    DRL ``source_id`` instead. This adapter delegates the underlying boto
    call with retry behaviour preserved.
    """

    def __init__(self, settings: Any) -> None:
        """Build the underlying R2 client and reuse its retry helper."""
        from app.storage import R2Storage  # local import: avoids hard dep in tests

        self._inner = R2Storage(settings)

    def put_object_at_key(self, key: str, body: bytes, content_type: str) -> None:
        """Upload ``body`` to the ``resemblio-extractions`` bucket at ``key``."""
        self._inner.ensure_bucket()
        # Reuse the inner client's retry helper for backoff consistency.
        self._inner._with_retries(  # noqa: SLF001 - private helper reuse is intentional
            lambda: self._inner.client.put_object(
                Bucket=self._inner.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )
        )


def _configure_logging() -> None:
    """Attach a stderr handler the first time the script's logger is configured.

    Only adds a handler when ``LOG`` has none, which protects two cases:

    1. Test runs where pytest's ``caplog`` plugin has attached its
       ``LogCaptureHandler`` to this logger via ``caplog.at_level(..., logger=
       seeder.LOG.name)``. Clearing handlers would strip pytest's capture and
       cause every record emitted inside ``main`` to disappear from
       ``caplog.records``.
    2. Library callers that already configured logging globally and would not
       want a duplicate stderr emit.

    The level is always set to INFO so unattended cron / scheduler invocations
    surface the per-asset plan rows the script intends to log.
    """
    if not LOG.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    _configure_logging()
    args = parse_args(argv)
    LOG.info(
        "seed_from_drl starting: apply=%s drl_root=%s limit=%s source_system=%s",
        args.apply,
        args.drl_root,
        args.limit,
        args.source_system,
    )

    corpus = load_corpus(args.drl_root)
    captured_date = str(corpus.get("generated") or DEFAULT_CAPTURED_DATE)
    pairs = list(filter_assets(iter_assets(corpus), args.source_system, args.limit))
    LOG.info("planning %d DRL asset(s) (captured=%s)", len(pairs), captured_date)

    if not args.apply:
        # Dry-run: zero network, zero DB, zero R2. Pass ``session=None`` so
        # ``plan_only`` classifies every row as ``insert`` without touching
        # the DB. Every row is logged so an operator running on a machine
        # that cannot reach prod Postgres can still preview the work.
        plan = plan_only(iter(pairs), args.drl_root, None)
        total_zip = sum(row["zip_bytes"] for row in plan)
        LOG.info(
            "DRY RUN: would write %d row(s), total_zip_bytes=%d (no DB, no R2, no network)",
            len(plan),
            total_zip,
        )
        for row in plan:
            LOG.info(
                "plan: source_id=%s op=%s tokens=%d zip=%dB r2_key=%s",
                row["source_id"],
                row["operation"],
                row["tokens_count"],
                row["zip_bytes"],
                row["r2_key"],
            )
        return 0

    # Apply path: import the DB layer lazily so dry-run never pays the cost
    # (or risk) of instantiating an engine against a possibly unreachable DB.
    from app.config import get_settings
    from app.db import SessionLocal

    storage = _R2SeedAdapter(get_settings())
    with SessionLocal() as session:
        counts = apply_seed(
            iter(pairs),
            args.drl_root,
            session,
            storage,
            args.seed_user_id,
            args.batch_size,
            captured_date=captured_date,
        )
    LOG.info("seed complete: %s", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
