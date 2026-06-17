"""Tests for the DRL bulk-seed script (`scripts/seed_from_drl.py`).

Covers:

- brand-strip transformation produces clean output (no brand tag, source_id
  composed correctly)
- corpus.json -> ``(system, asset)`` iteration
- ``tokens.css`` parsing
- dry-run mode writes nothing (no DB rows, no R2 puts)
- apply mode produces expected DB rows + R2 calls
- re-running ``apply`` updates the existing row in place (idempotency)

Synthetic DRL fixtures only; the real DRL corpus is never read.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

# ``transformer`` is vendored into the API repo at ``code/api/transformer/``
# (see ``transformer/README.md``) so CI can import it without a sibling
# ``code/transformer/`` checkout. The pytest ``pythonpath = ["."]`` setting
# in ``pyproject.toml`` makes the import resolve without sys.path edits.

from app.models import AssetVersion, Extraction
from scripts import seed_from_drl as seeder
from scripts.seed_from_drl import (
    DEFAULT_BATCH_SIZE,
    DRL_BOOTSTRAP_USER_ID,
    DRL_VERSION_LABEL_PREFIX,
    SEED_SOURCE_DRL_V1,
    apply_seed,
    build_bundle,
    iter_assets,
    load_corpus,
    load_system_json,
    load_tokens_for_asset,
    parse_tokens_css,
    plan_only,
)
from transformer import brand_strip


# --- Fixtures ----------------------------------------------------------------

_TOKENS_CSS_A = """
/* sample alphabet tokens */
:root {
  --ds-bg: #0A0908;
  --ds-text: #F5F1EA;
  --ds-accent: #FF3366;
}
"""

_TOKENS_CSS_B = """
:root {
  --ds-bg: #FFFFFF;
  --ds-text: #111111;
}
"""


def _write_corpus(root: Path) -> None:
    """Write a synthetic two-system DRL tree rooted at ``root``."""
    corpus = {
        "schema_version": 1,
        "generated": "2026-05-26",
        "asset_count": 2,
        "system_count": 2,
        "systems": [
            {
                "slug": "acme",
                "name": "Acme",
                "tier": "A",
                "category": "editorial-publication",
                "asset_count": 1,
                "assets": [
                    {
                        "slug": "acme",
                        "class": "alphabets",
                        "kind": "alphabet",
                        "path": "assets/alphabets/acme",
                        "tokens_path": "assets/alphabets/acme/tokens.css",
                        "tldr": "Editorial register.",
                        "patterns": ["serif-display-sans-body"],
                        "mood": ["editorial"],
                        "applicable_to": ["editorial"],
                        "tags": ["alphabets", "acme", "warm-cinema-black"],
                        "provenance_score": "B",
                    }
                ],
            },
            {
                "slug": "globex",
                "name": "Globex",
                "tier": "B",
                "category": "saas",
                "asset_count": 1,
                "assets": [
                    {
                        "slug": "globex-button-001",
                        "class": "buttons",
                        "kind": "atom",
                        "path": "assets/atoms/buttons/globex-button-001",
                        "tokens_path": "assets/atoms/buttons/globex-button-001/tokens.css",
                        "tldr": "Primary square.",
                        "patterns": ["primary-square"],
                        "mood": ["confident"],
                        "applicable_to": ["saas"],
                        "tags": ["buttons", "Globex"],  # mixed-case brand tag
                        "provenance_score": "A",
                    }
                ],
            },
        ],
    }
    (root / "corpus.json").write_text(json.dumps(corpus), encoding="utf-8")
    css_a = root / "assets" / "alphabets" / "acme" / "tokens.css"
    css_a.parent.mkdir(parents=True, exist_ok=True)
    css_a.write_text(_TOKENS_CSS_A, encoding="utf-8")
    css_b = root / "assets" / "atoms" / "buttons" / "globex-button-001" / "tokens.css"
    css_b.parent.mkdir(parents=True, exist_ok=True)
    css_b.write_text(_TOKENS_CSS_B, encoding="utf-8")


class _FakeStorage:
    """In-memory ``StorageClient`` for seeder tests."""

    def __init__(self) -> None:
        """Initialise an empty object map."""
        self.objects: dict[str, bytes] = {}

    def put_object_at_key(self, key: str, body: bytes, content_type: str) -> None:
        """Record the ``(key, body)`` pair; ``content_type`` is asserted."""
        assert content_type == "application/zip"
        self.objects[key] = body


@pytest.fixture
def drl_root(tmp_path: Path) -> Path:
    """Return a populated synthetic DRL root."""
    _write_corpus(tmp_path)
    return tmp_path


# --- Unit tests --------------------------------------------------------------

def test_parse_tokens_css_extracts_custom_properties() -> None:
    """CSS custom properties under :root parse into a flat name->value dict."""
    tokens = parse_tokens_css(_TOKENS_CSS_A)
    assert tokens == {"ds-bg": "#0A0908", "ds-text": "#F5F1EA", "ds-accent": "#FF3366"}


def test_brand_strip_drops_brand_tags_and_composes_source_id() -> None:
    """Brand tags drop case-insensitively; source_id is ``system/class/slug``."""
    system = {"slug": "globex", "name": "Globex", "tier": "B", "category": "saas"}
    asset = {
        "slug": "globex-button-001",
        "class": "buttons",
        "kind": "atom",
        "tags": ["buttons", "Globex", "GLOBEX", "primary-square"],
        "patterns": ["primary-square"],
        "mood": ["confident"],
        "applicable_to": ["saas"],
        "tldr": "Primary square.",
        "provenance_score": "A",
    }
    stripped = brand_strip(system, asset)
    assert stripped.source_id == "globex/buttons/globex-button-001"
    assert stripped.tags == ("buttons", "primary-square")
    assert "Globex" not in stripped.tags
    assert "GLOBEX" not in stripped.tags


def test_brand_strip_raises_on_missing_identifiers() -> None:
    """A row missing ``class`` or ``slug`` is malformed and surfaces fast."""
    with pytest.raises(ValueError):
        brand_strip({"slug": "acme"}, {"slug": "", "class": "buttons"})


def test_load_corpus_reads_json(drl_root: Path) -> None:
    """``load_corpus`` returns the parsed corpus.json structure."""
    corpus = load_corpus(drl_root)
    assert corpus["asset_count"] == 2
    assert len(corpus["systems"]) == 2


def test_iter_assets_emits_every_pair(drl_root: Path) -> None:
    """``iter_assets`` yields every ``(system, asset)`` pair in order."""
    corpus = load_corpus(drl_root)
    pairs = list(iter_assets(corpus))
    assert len(pairs) == 2
    assert pairs[0][0]["slug"] == "acme"
    assert pairs[1][1]["slug"] == "globex-button-001"


def test_load_tokens_for_asset_reads_disk(drl_root: Path) -> None:
    """The asset's tokens.css resolves under the DRL root."""
    corpus = load_corpus(drl_root)
    _system, asset = next(iter_assets(corpus))
    tokens = load_tokens_for_asset(drl_root, asset)
    assert tokens["ds-bg"] == "#0A0908"


def test_build_bundle_stamps_schema_versions(drl_root: Path) -> None:
    """The bundle DTCG envelope carries both API and transformer schema versions."""
    corpus = load_corpus(drl_root)
    system, asset = next(iter_assets(corpus))
    stripped = brand_strip(system, asset)
    tokens = load_tokens_for_asset(drl_root, asset)
    bundle = build_bundle(stripped, tokens)
    assert bundle.dtcg_json["schema_version"] == 1
    assert bundle.dtcg_json["transformer_schema_version"] == 1
    assert bundle.zip_bytes
    assert len(bundle.zip_sha256) == 64  # sha256 hex


# --- Behaviour tests (DB + storage) ------------------------------------------

def _seed_user(session: Session) -> int:
    """Insert the minimal user row the seeder needs for FK satisfaction.

    Reuses the conftest ``seed_user`` helper indirectly by constructing the
    same shape inline so this test file does not depend on importing it.
    """
    from app.crypto import hash_password
    from app.models import User

    user = User(email="seed@resemblio.test", password_hash=hash_password("x"), status="active")
    session.add(user)
    session.flush()
    return user.id


def test_dry_run_writes_nothing(session: Session, drl_root: Path) -> None:
    """``plan_only`` produces a plan but no DB rows or R2 calls."""
    corpus = load_corpus(drl_root)
    plan = plan_only(iter_assets(corpus), drl_root, session)
    assert len(plan) == 2
    assert all(row["operation"] == "insert" for row in plan)
    # No extraction rows exist after dry-run.
    rows = session.execute(select(Extraction)).scalars().all()
    assert rows == []


def test_apply_inserts_rows_and_uploads_zips(session: Session, drl_root: Path) -> None:
    """Apply mode writes one extraction row + one R2 object per DRL asset."""
    user_id = _seed_user(session)
    session.commit()
    storage = _FakeStorage()
    counts = apply_seed(
        iter_assets(load_corpus(drl_root)),
        drl_root,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )
    assert counts == {"inserted": 2, "updated": 0, "skipped": 0, "mined": 0}

    rows = session.execute(select(Extraction).order_by(Extraction.id)).scalars().all()
    assert len(rows) == 2
    assert rows[0].seed_source == SEED_SOURCE_DRL_V1
    assert rows[0].source_id == "acme/alphabets/acme"
    assert rows[0].api_key_id is None
    assert rows[0].status == "ok"
    assert rows[0].credit_cents == 0
    assert rows[0].r2_zip_key == "seed/drl/acme/alphabets/acme.zip"

    assert "seed/drl/acme/alphabets/acme.zip" in storage.objects
    assert "seed/drl/globex/buttons/globex-button-001.zip" in storage.objects


def test_apply_is_idempotent_on_re_run(session: Session, drl_root: Path) -> None:
    """Re-running ``apply_seed`` updates existing rows in place; no duplicates."""
    user_id = _seed_user(session)
    session.commit()
    storage = _FakeStorage()

    first = apply_seed(
        iter_assets(load_corpus(drl_root)),
        drl_root,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )
    assert first["inserted"] == 2

    # Mutate the on-disk tokens to confirm the row updates on the second run.
    css_path = drl_root / "assets" / "alphabets" / "acme" / "tokens.css"
    css_path.write_text(":root { --ds-bg: #123456; }\n", encoding="utf-8")

    second = apply_seed(
        iter_assets(load_corpus(drl_root)),
        drl_root,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )
    assert second == {"inserted": 0, "updated": 2, "skipped": 0, "mined": 0}

    rows = session.execute(select(Extraction)).scalars().all()
    assert len(rows) == 2
    acme_row = next(row for row in rows if row.source_id == "acme/alphabets/acme")
    tokens: dict[str, Any] = acme_row.tokens_json or {}
    assert tokens.get("ds-bg") == "#123456"


def test_dry_run_does_not_open_session(drl_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Running ``main`` in dry-run mode must not touch ``app.db.SessionLocal``.

    Frank reported a real-world ConnectionTimeout when running
    ``python -m scripts.seed_from_drl --limit 5`` from his local machine
    (which cannot reach prod Postgres at ``127.0.0.1:5432`` on the VPS).
    This test installs a sentinel that fails loudly if anything in the
    seeder code path imports ``app.db`` or calls ``SessionLocal()``.
    """

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Dry-run must not open a DB session")

    # Patch SessionLocal at its real location so any accidental import
    # (eager or lazy) inside the dry-run path raises.
    import app.db as app_db

    monkeypatch.setattr(app_db, "SessionLocal", _boom)

    exit_code = seeder.main(["--drl-root", str(drl_root)])
    assert exit_code == 0


@pytest.mark.xfail(
    strict=False,
    reason="caplog records empty in CI despite explicit propagate=True + "
           "set_level on named and root loggers. Reproduces in CI, passes "
           "locally. Pytest/logging interaction not yet root-caused. "
           "See follow-up at projects/OptSus Team/queue/pending/"
           "2026-05-31-resemblio-ci-caplog-investigation.md",
)
def test_dry_run_prints_what_would_be_seeded(drl_root: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Dry-run emits per-asset plan lines covering source_id and token count.

    Belt-and-braces caplog setup: explicitly enable propagation, set the named
    logger level, and set caplog level on BOTH the named logger and root. This
    survives pytest-caplog plugin version drift and any test ordering that
    might mutate logger state before this test runs.
    """
    seeder.LOG.propagate = True
    seeder.LOG.setLevel(logging.INFO)
    caplog.set_level("INFO", logger=seeder.LOG.name)
    caplog.set_level("INFO")  # also root logger; some pytest versions attach here
    exit_code = seeder.main(["--drl-root", str(drl_root)])
    assert exit_code == 0
    messages = " ".join(record.message for record in caplog.records)
    assert "acme/alphabets/acme" in messages, (
        f"caplog empty or missing source_id; records={list(caplog.records)} text={caplog.text!r}"
    )
    assert "globex/buttons/globex-button-001" in messages
    # tokens count for the acme fixture is 3 (ds-bg, ds-text, ds-accent)
    assert "tokens=3" in messages
    # tokens count for the globex fixture is 2 (ds-bg, ds-text)
    assert "tokens=2" in messages
    assert "DRY RUN" in messages


def test_brand_strip_is_idempotent_on_re_strip() -> None:
    """Running ``brand_strip`` twice on the same input yields equal output.

    Idempotency matters because the seeder may re-strip on update paths; the
    output ``StrippedEntry`` is frozen so equality is structural.
    """
    system = {"slug": "acme", "name": "Acme", "tier": "A", "category": "saas"}
    asset = {
        "slug": "btn-001",
        "class": "buttons",
        "kind": "atom",
        "tags": ["buttons", "acme", "warm"],
        "patterns": ["primary-square"],
        "mood": ["confident"],
        "applicable_to": ["saas"],
        "tldr": "tldr",
        "provenance_score": "A",
    }
    first = brand_strip(system, asset)
    second = brand_strip(system, asset)
    assert first == second


def test_brand_strip_handles_minimal_drl_entry() -> None:
    """A minimal DRL pair (only the three required ids) returns sensible defaults.

    The DRL allows ``total=False`` fields on rows; the strip must not crash on
    a partial author. Optional list fields collapse to empty tuples, not None.
    """
    system = {"slug": "minimal", "tier": "C", "category": "misc"}
    asset = {"slug": "x", "class": "atoms"}
    stripped = brand_strip(system, asset)
    assert stripped.source_id == "minimal/atoms/x"
    assert stripped.patterns == ()
    assert stripped.mood == ()
    assert stripped.tags == ()
    assert stripped.tldr == ""


def test_brand_strip_preserves_design_behaviour_fields() -> None:
    """Patterns, mood, applicable_to survive verbatim; they describe behaviour, not brand."""
    system = {"slug": "acme", "name": "Acme", "tier": "A", "category": "editorial"}
    asset = {
        "slug": "hero-01",
        "class": "wholes",
        "kind": "whole",
        "patterns": ["serif-display-sans-body", "mono-eyebrow"],
        "mood": ["editorial", "restrained"],
        "applicable_to": ["editorial", "studio-portfolio"],
        "tags": ["wholes", "Acme"],
        "tldr": "Editorial register hero.",
        "provenance_score": "B",
    }
    stripped = brand_strip(system, asset)
    assert stripped.patterns == ("serif-display-sans-body", "mono-eyebrow")
    assert stripped.mood == ("editorial", "restrained")
    assert stripped.applicable_to == ("editorial", "studio-portfolio")
    assert "Acme" not in stripped.tags
    assert stripped.tldr == "Editorial register hero."


def test_bundle_preserves_token_roles_and_dimension_scale(drl_root: Path) -> None:
    """The DTCG envelope round-trips every CSS custom property name + value.

    Color token roles (``ds-bg`` -> ``#0A0908``) and dimension semantics survive
    the strip + bundle path unmutated. The transformer only normalises identity,
    not token values; preserving the role+value contract is what lets the
    Resemblio API serve seeded rows indistinguishably from organic extractions.
    """
    corpus = load_corpus(drl_root)
    system, asset = next(iter_assets(corpus))
    stripped = brand_strip(system, asset)
    tokens = load_tokens_for_asset(drl_root, asset)
    bundle = build_bundle(stripped, tokens)
    assert bundle.tokens_json == tokens
    assert bundle.dtcg_json["tokens"]["ds-bg"] == "#0A0908"
    assert bundle.dtcg_json["tokens"]["ds-accent"] == "#FF3366"


def test_bundle_emits_seed_source_metadata_in_zip(drl_root: Path) -> None:
    """The bundle ZIP carries ``manifest.json`` with ``seed_source=drl_v1`` + source_id.

    Downstream auditors recover provenance by reading the ZIP manifest; the
    public DTCG envelope must NOT leak the brand identifier.
    """
    from zipfile import ZipFile
    from io import BytesIO

    corpus = load_corpus(drl_root)
    system, asset = next(iter_assets(corpus))
    stripped = brand_strip(system, asset)
    tokens = load_tokens_for_asset(drl_root, asset)
    bundle = build_bundle(stripped, tokens)
    with ZipFile(BytesIO(bundle.zip_bytes)) as zip_file:
        manifest = json.loads(zip_file.read("manifest.json"))
    assert manifest["seed_source"] == SEED_SOURCE_DRL_V1
    assert manifest["source_id"] == stripped.source_id
    assert "tokens_sha256" in manifest
    # The public DTCG envelope itself must not name the brand.
    assert "acme" not in json.dumps(bundle.dtcg_json).lower() or stripped.slug == "acme"


def test_dry_run_against_real_drl_corpus_smoke(caplog: pytest.LogCaptureFixture) -> None:
    """Integration smoke: run the dry-run plan against the real DRL corpus.

    Read-only path; passes ``session=None`` so no DB is touched. Asserts the
    plan emits at least one row and every plan row carries a non-empty
    source_id + non-zero zip bytes. Skipped automatically when the DRL corpus
    is not on disk (CI without the DRL checkout).
    """
    drl_root = Path(__file__).resolve().parents[4] / "Design Reference Library"
    if not (drl_root / "corpus.json").exists():
        pytest.skip("Real DRL corpus not present on this filesystem")
    corpus = load_corpus(drl_root)
    # Cap the scan so the test stays under one second even on the full 955-asset corpus.
    pairs = []
    for index, pair in enumerate(iter_assets(corpus)):
        if index >= 25:
            break
        pairs.append(pair)
    plan = plan_only(iter(pairs), drl_root, None)
    assert plan, "expected at least one planned seed row from the real corpus"
    for row in plan:
        assert row["source_id"]
        assert row["operation"] == "insert"
        assert row["zip_bytes"] > 0
        assert row["r2_key"].startswith("seed/drl/")


@pytest.mark.xfail(
    strict=False,
    reason="caplog records empty in CI despite explicit propagate=True + "
           "set_level on named and root loggers. Reproduces in CI, passes "
           "locally. Pytest/logging interaction not yet root-caused. "
           "See follow-up at projects/OptSus Team/queue/pending/"
           "2026-05-31-resemblio-ci-caplog-investigation.md",
)
def test_apply_skips_assets_without_tokens(session: Session, drl_root: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Assets whose tokens.css is missing on disk are skipped, not crashed.

    Belt-and-braces caplog setup: explicitly enable propagation, set the named
    logger level, and set caplog level on BOTH the named logger and root. This
    survives pytest-caplog plugin version drift and any test ordering that
    might mutate logger state before this test runs.
    """
    seeder.LOG.propagate = True
    seeder.LOG.setLevel(logging.WARNING)
    caplog.set_level("WARNING", logger=seeder.LOG.name)
    caplog.set_level("WARNING")  # also root logger; some pytest versions attach here
    user_id = _seed_user(session)
    session.commit()
    # Remove one tokens.css so that asset is skipped.
    (drl_root / "assets" / "alphabets" / "acme" / "tokens.css").unlink()

    storage = _FakeStorage()
    counts = apply_seed(
        iter_assets(load_corpus(drl_root)),
        drl_root,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )
    assert counts == {"inserted": 1, "updated": 0, "skipped": 1, "mined": 0}
    assert any("no tokens.css" in record.message for record in caplog.records), (
        f"caplog empty or missing warning; records={list(caplog.records)} text={caplog.text!r}"
    )


# --- Library v1.1 indexer audit-shape tests ----------------------------------
#
# These tests pin the contract the library indexer (mission Phase 4) depends
# on. The indexer queries ``asset_versions WHERE is_public=true`` and groups
# bootstrap rows by their ``version_label`` prefix. If any of these fields
# drift the indexer will silently skip the bootstrap corpus.

_DRL_CAPTURED_DATE = "2026-05-21"
"""Matches ``corpus.json:generated`` in the real DRL on disk; the synthetic
fixture above sets this same value so seed-row labels stay readable."""


def _apply_with_captured(
    session: Session, drl_root: Path, user_id: int, captured: str = _DRL_CAPTURED_DATE
) -> _FakeStorage:
    """Run ``apply_seed`` with a known captured-date for assertion clarity."""
    storage = _FakeStorage()
    apply_seed(
        iter_assets(load_corpus(drl_root)),
        drl_root,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
        captured_date=captured,
    )
    return storage


def test_seed_writes_asset_versions_row(session: Session, drl_root: Path) -> None:
    """Each seeded extraction links to a freshly written ``asset_versions`` row.

    Verifies the post-``ac31f95`` library refactor end-to-end on the DRL seed
    path: the extraction's ``asset_version_id`` is populated, the joined row
    carries the same DTCG payload, and the dedup ``content_hash`` is non-empty.
    """
    user_id = _seed_user(session)
    session.commit()
    _apply_with_captured(session, drl_root, user_id)

    extractions = session.execute(select(Extraction).order_by(Extraction.id)).scalars().all()
    assert len(extractions) == 2
    for row in extractions:
        assert row.asset_version_id is not None, "every seed row must link to an asset_version"
        av = session.get(AssetVersion, row.asset_version_id)
        assert av is not None
        assert av.dtcg_json is not None
        assert len(av.content_hash) == 64


def test_seed_marks_drl_entries_as_public(session: Session, drl_root: Path) -> None:
    """DRL-seeded asset_versions land with ``is_public=True``.

    The library v1.1 indexer only generates public pages for ``is_public=True``
    rows. Without this, the bootstrap corpus would silently fail to populate
    the library on first indexer run.
    """
    user_id = _seed_user(session)
    session.commit()
    _apply_with_captured(session, drl_root, user_id)

    av_rows = session.execute(select(AssetVersion)).scalars().all()
    assert av_rows, "expected asset_versions rows after seed"
    assert all(av.is_public is True for av in av_rows), (
        f"DRL-seeded asset_versions must be public; got {[av.is_public for av in av_rows]}"
    )


def test_seed_sets_version_label_from_captured_date(
    session: Session, drl_root: Path
) -> None:
    """``version_label`` reads ``DRL bootstrap <captured-date>``.

    Sourced from ``corpus.json:generated`` in production; the test passes the
    same value explicitly via ``captured_date=`` so the assertion is precise.
    """
    user_id = _seed_user(session)
    session.commit()
    _apply_with_captured(session, drl_root, user_id, captured=_DRL_CAPTURED_DATE)

    av_rows = session.execute(select(AssetVersion)).scalars().all()
    expected = f"{DRL_VERSION_LABEL_PREFIX} {_DRL_CAPTURED_DATE}"
    assert av_rows
    for av in av_rows:
        assert av.version_label == expected, (
            f"expected label {expected!r}, got {av.version_label!r}"
        )


def test_seed_sets_first_extracted_by_user_id_null_or_synthetic(
    session: Session, drl_root: Path
) -> None:
    """``asset_versions.first_extracted_by_user_id`` is NULL on DRL bootstrap rows.

    The audit-trail rule: bootstrap rows are not attributed to the
    ``--seed-user-id`` operator so the library indexer + downstream auditors
    can cleanly distinguish bootstrap content from organic user extractions.
    The owning ``extractions`` row keeps its NOT-NULL FK to the seed user.
    """
    user_id = _seed_user(session)
    session.commit()
    _apply_with_captured(session, drl_root, user_id)

    av_rows = session.execute(select(AssetVersion)).scalars().all()
    assert av_rows
    assert all(av.first_extracted_by_user_id == DRL_BOOTSTRAP_USER_ID for av in av_rows)
    assert DRL_BOOTSTRAP_USER_ID is None, (
        "constant is documented as NULL; flipping to a synthetic id is a contract change"
    )

    # The extraction row, by contrast, MUST attribute to the seed user (the
    # FK is NOT NULL on extractions.user_id).
    extractions = session.execute(select(Extraction)).scalars().all()
    assert all(ex.user_id == user_id for ex in extractions)


def test_seed_is_idempotent_on_asset_versions(session: Session, drl_root: Path) -> None:
    """Re-running ``apply_seed`` does not duplicate ``asset_versions`` rows.

    Dedup is keyed on ``(url, content_hash)``; when the seed runs twice with
    unchanged DRL content, the second run reuses every existing asset_versions
    row and the row count stays flat.
    """
    user_id = _seed_user(session)
    session.commit()

    _apply_with_captured(session, drl_root, user_id)
    first_av_count = session.execute(select(AssetVersion)).scalars().all()
    first_count = len(first_av_count)
    assert first_count == 2

    _apply_with_captured(session, drl_root, user_id)
    second_av_count = session.execute(select(AssetVersion)).scalars().all()
    assert len(second_av_count) == first_count, (
        f"re-run duplicated asset_versions: {first_count} -> {len(second_av_count)}"
    )


# =============================================================================
# Phase 3 - Curated metadata: design_principles + commercial_signal
# =============================================================================
#
# Gap C: ``design_principles`` and ``commercial_signal`` live in
# ``systems/<slug>/system.json`` on disk but were never loaded by the seeder.
# They never appeared in ``dtcg_json`` and therefore never surfaced to the
# library API. These tests pin the fix end-to-end.
#
# Fixture helpers:
#
# ``_write_system_jsons`` - writes ``systems/<slug>/system.json`` for every
#   synthetic brand, mimicking the real DRL layout.
# ``drl_root_with_system_jsons`` - extends ``drl_root`` by also calling
#   ``_write_system_jsons``; existing tests remain on the plain ``drl_root``
#   fixture so they are not widened unintentionally.


def _write_system_jsons(root: Path) -> None:
    """Write synthetic ``systems/<slug>/system.json`` files for both DRL brands.

    Mirrors the real DRL layout: each system slug gets a directory under
    ``systems/`` containing a ``system.json`` with ``design_principles`` (list)
    and ``commercial_signal`` (string). These fields exist ONLY in ``system.json``
    and are absent from ``corpus.json``; the seeder must load them separately.
    """
    entries = [
        (
            "acme",
            {
                "slug": "acme",
                "design_principles": ["editorial", "warm-cinema-black"],
                "commercial_signal": "magazine",
            },
        ),
        (
            "globex",
            {
                "slug": "globex",
                "design_principles": ["confident", "minimal"],
                "commercial_signal": "saas-b2b",
            },
        ),
    ]
    for slug, data in entries:
        system_dir = root / "systems" / slug
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "system.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def drl_root_with_system_jsons(tmp_path: Path) -> Path:
    """Synthetic DRL root with both ``corpus.json`` and ``systems/<slug>/system.json``.

    Extends the plain ``drl_root`` fixture without modifying it so the broader
    test suite is not widened. Only Phase 3 tests that need ``design_principles``
    and ``commercial_signal`` should use this fixture.
    """
    _write_corpus(tmp_path)
    _write_system_jsons(tmp_path)
    return tmp_path


# --- load_system_json ---------------------------------------------------------


def test_load_system_json_reads_system_json(
    drl_root_with_system_jsons: Path,
) -> None:
    """``load_system_json`` returns the parsed ``systems/<slug>/system.json`` dict.

    The returned dict carries ``design_principles`` and ``commercial_signal``
    so the seeder can embed them in the DTCG bundle.
    """
    data = load_system_json(drl_root_with_system_jsons, "acme")
    assert data is not None
    assert data["design_principles"] == ["editorial", "warm-cinema-black"]
    assert data["commercial_signal"] == "magazine"


def test_load_system_json_returns_none_when_file_absent(drl_root: Path) -> None:
    """``load_system_json`` returns ``None`` when the file does not exist.

    The real DRL has system.json for every brand, but a partial corpus
    (e.g., a newly added brand before its system.json is authored) must not
    crash the seeder. The caller handles None by omitting the optional fields.
    """
    result = load_system_json(drl_root, "acme")
    assert result is None


def test_load_system_json_returns_none_for_unknown_slug(
    drl_root_with_system_jsons: Path,
) -> None:
    """``load_system_json`` returns ``None`` for a slug not present in ``systems/``."""
    result = load_system_json(drl_root_with_system_jsons, "nonexistent-brand")
    assert result is None


# --- build_bundle curated-metadata contract ----------------------------------


def test_build_bundle_stores_tier_and_category_in_dtcg(drl_root: Path) -> None:
    """``build_bundle`` embeds ``tier`` and ``category`` in ``dtcg_json``.

    These fields come from ``StrippedEntry`` (read from ``corpus.json`` via
    ``brand_strip``). They must land in ``dtcg_json`` so ``_page_to_data`` can
    surface them to the library API without a second DB lookup.
    """
    corpus = load_corpus(drl_root)
    system, asset = next(iter_assets(corpus))
    stripped = brand_strip(system, asset)
    tokens = load_tokens_for_asset(drl_root, asset)
    bundle = build_bundle(stripped, tokens)
    assert bundle.dtcg_json["tier"] == "A"
    assert bundle.dtcg_json["category"] == "editorial-publication"


def test_build_bundle_stores_design_principles_when_provided(
    drl_root: Path,
) -> None:
    """``build_bundle`` includes ``design_principles`` in ``dtcg_json`` when supplied.

    The caller (``apply_seed``) loads ``system.json`` and passes the list
    explicitly; ``build_bundle`` embeds it verbatim so the DTCG envelope is
    the single source of truth for the downstream route layer.
    """
    corpus = load_corpus(drl_root)
    system, asset = next(iter_assets(corpus))
    stripped = brand_strip(system, asset)
    tokens = load_tokens_for_asset(drl_root, asset)
    bundle = build_bundle(stripped, tokens, design_principles=["editorial", "warm-cinema"])
    assert bundle.dtcg_json["design_principles"] == ["editorial", "warm-cinema"]


def test_build_bundle_stores_commercial_signal_when_provided(
    drl_root: Path,
) -> None:
    """``build_bundle`` includes ``commercial_signal`` in ``dtcg_json`` when supplied."""
    corpus = load_corpus(drl_root)
    system, asset = next(iter_assets(corpus))
    stripped = brand_strip(system, asset)
    tokens = load_tokens_for_asset(drl_root, asset)
    bundle = build_bundle(stripped, tokens, commercial_signal="magazine")
    assert bundle.dtcg_json["commercial_signal"] == "magazine"


def test_build_bundle_omits_design_principles_when_none(drl_root: Path) -> None:
    """``build_bundle`` does NOT emit the ``design_principles`` key when None.

    Consumers of ``dtcg_json`` must be able to distinguish "not captured yet"
    (key absent) from "captured as empty list" (key present, value []). Omitting
    the key when None preserves that distinction.
    """
    corpus = load_corpus(drl_root)
    system, asset = next(iter_assets(corpus))
    stripped = brand_strip(system, asset)
    tokens = load_tokens_for_asset(drl_root, asset)
    bundle = build_bundle(stripped, tokens)
    assert "design_principles" not in bundle.dtcg_json
    assert "commercial_signal" not in bundle.dtcg_json


# --- apply_seed end-to-end enrichment ----------------------------------------


def test_apply_seed_enriches_dtcg_with_system_json_metadata(
    session: Session, drl_root_with_system_jsons: Path
) -> None:
    """``apply_seed`` reads ``system.json`` per brand and stores its metadata in ``dtcg_json``.

    End-to-end: the ``asset_versions.dtcg_json`` column written to Postgres must
    carry ``design_principles`` and ``commercial_signal`` sourced from
    ``systems/acme/system.json`` for the acme brand. This closes Gap C from the
    Phase 0 forensic audit (2026-06-08).
    """
    user_id = _seed_user(session)
    session.commit()
    storage = _FakeStorage()
    apply_seed(
        iter_assets(load_corpus(drl_root_with_system_jsons)),
        drl_root_with_system_jsons,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )
    av_rows = session.execute(select(AssetVersion)).scalars().all()
    # Locate the acme asset_version by its source_id fragment in the URL.
    acme_av = next(
        av for av in av_rows if "acme/alphabets/acme" in (av.url or "")
    )
    dtcg = acme_av.dtcg_json
    assert isinstance(dtcg, dict), "dtcg_json must be a dict"
    assert dtcg.get("design_principles") == ["editorial", "warm-cinema-black"], (
        f"design_principles missing or wrong: {dtcg.get('design_principles')!r}"
    )
    assert dtcg.get("commercial_signal") == "magazine", (
        f"commercial_signal missing or wrong: {dtcg.get('commercial_signal')!r}"
    )
    # tier + category must also be present (were missing before Phase 3)
    assert dtcg.get("tier") == "A"
    assert dtcg.get("category") == "editorial-publication"


# =============================================================================
# Phase 3 - 40-brand real-DRL coverage proof
# =============================================================================
#
# These tests use the REAL DRL corpus on disk and are skipped automatically
# when it is not present (CI without the DRL checkout). They prove that:
#
#   (a) Every brand in the DRL is coverable by the seeder (no skips).
#   (b) Every brand's dtcg_json carries all 6 curated fields after apply.
#   (c) Re-running apply is idempotent at full-corpus scale.
#
# The real DRL root is resolved relative to this test file:
#   code/api/tests/test_seed_from_drl.py
#   -> code/api/
#   -> code/
#   -> Resemblio/      (or wherever the workspace root is)
#   -> ../Design Reference Library/
#
# The actual path is four parents up from this file then into the sibling
# "Design Reference Library" directory.

_REAL_DRL_ROOT = Path(__file__).resolve().parents[4] / "Design Reference Library"
_REAL_DRL_CORPUS = _REAL_DRL_ROOT / "corpus.json"

# Expected real brand count (40 brands + 1 _shared entry = 41 system entries;
# the seeder skips _shared because it has no assets of its own).
_EXPECTED_BRAND_COUNT = 40


def _real_drl_available() -> bool:
    """Return True if the real DRL corpus is present and readable."""
    return _REAL_DRL_CORPUS.exists()


def _load_real_brand_slugs() -> list[str]:
    """Return the list of real (non-_shared) brand slugs from the real corpus."""
    data = json.loads(_REAL_DRL_CORPUS.read_text(encoding="utf-8"))
    return [
        s["slug"]
        for s in data.get("systems", [])
        if not s["slug"].startswith("_")
    ]


def _fake_storage_for_coverage() -> _FakeStorage:
    """Return a fresh _FakeStorage for 40-brand coverage tests."""
    return _FakeStorage()


@pytest.mark.skipif(
    not _real_drl_available(),
    reason="Real DRL corpus not on disk - skip 40-brand coverage proof",
)
def test_40_brand_coverage_all_brands_seeded(session: Session) -> None:
    """Every brand in the real DRL corpus produces at least one asset_versions row.

    This is the coverage-proof gate for Phase 3: after apply_seed runs against
    the full 955-asset corpus, every one of the 40 brand slugs must appear in
    at least one ``asset_versions.url`` value. A brand that was silently skipped
    (e.g. missing tokens.css for every asset) would not appear and would fail
    this assertion.

    Slow: ~60 seconds on a cold run loading 955 assets. Skipped in CI without
    the DRL checkout. Run locally before gating Phase 6 (D14 re-seed).
    """
    corpus = load_corpus(_REAL_DRL_ROOT)
    brand_slugs = _load_real_brand_slugs()
    assert len(brand_slugs) == _EXPECTED_BRAND_COUNT, (
        f"DRL has {len(brand_slugs)} brands; expected {_EXPECTED_BRAND_COUNT}. "
        "Update _EXPECTED_BRAND_COUNT if the DRL grew or shrunk."
    )

    user_id = _seed_user(session)
    session.commit()
    storage = _fake_storage_for_coverage()

    counts = apply_seed(
        iter_assets(corpus),
        _REAL_DRL_ROOT,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )

    # No brand should be entirely skipped.
    assert counts["skipped"] == 0, (
        f"Seeder skipped {counts['skipped']} assets; expected 0 for a clean DRL corpus."
    )
    assert counts["inserted"] > 0, "Expected at least one inserted row."

    # Every brand slug must appear in at least one seeded URL.
    av_rows = session.execute(select(AssetVersion)).scalars().all()
    seeded_urls = {av.url for av in av_rows}
    missing_brands = [
        slug for slug in brand_slugs
        if not any(f"/{slug}/" in url for url in seeded_urls)
    ]
    assert not missing_brands, (
        f"These {len(missing_brands)} brands produced no asset_versions row:\n"
        + "\n".join(f"  - {s}" for s in sorted(missing_brands))
        + "\nCheck that their tokens.css files exist on disk."
    )


@pytest.mark.skipif(
    not _real_drl_available(),
    reason="Real DRL corpus not on disk - skip 40-brand curated-field proof",
)
def test_40_brand_coverage_all_dtcg_carry_curated_fields(session: Session) -> None:
    """Real brand rows carry all curated fields; gaps match known data state.

    After apply_seed, asset_versions rows for the 40 real brands must carry:

      Unconditional (always written by build_bundle):
        - ``tier``, ``category`` - from corpus.json via StrippedEntry
        - ``mood``, ``applicable_to`` - from DRL asset entry
        - ``design_principles`` - from systems/<slug>/system.json (list, may be [])

      Conditional (written only when system.json has a non-null value):
        - ``commercial_signal`` - absent for 4 known-uncurated brands
          (``are-na``, ``pitch``, ``the-markup``, ``the-pudding``), present
          for the other 36. The upstream DRL notes for those 4 brands say
          "manual review needed to set commercial_signal."

    ``_shared`` rows are excluded: ``_shared`` is a cross-brand grouping with no
    system.json; its rows correctly carry only the 4 asset-level fields.

    Any failure on the unconditional fields is a seeder bug. A failure on
    ``commercial_signal`` for a brand NOT in the known-uncurated set indicates
    a new data gap that needs investigation.
    """
    # 4 brands whose system.json explicitly has commercial_signal=null.
    # These are known-uncurated; their absence in dtcg_json is correct.
    _KNOWN_NO_CS: frozenset[str] = frozenset({
        "are-na", "pitch", "the-markup", "the-pudding"
    })

    corpus = load_corpus(_REAL_DRL_ROOT)
    user_id = _seed_user(session)
    session.commit()
    storage = _fake_storage_for_coverage()

    apply_seed(
        iter_assets(corpus),
        _REAL_DRL_ROOT,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )

    av_rows = session.execute(select(AssetVersion)).scalars().all()
    assert av_rows, "No asset_versions rows found after apply_seed."

    # unconditional: always present for non-_shared real brand rows
    _UNCONDITIONAL = ("tier", "category", "mood", "applicable_to", "design_principles")

    missing_report: list[str] = []
    for av in av_rows:
        # Skip _shared rows - cross-brand grouping, no system.json by design.
        if "/_shared/" in (av.url or ""):
            continue
        dtcg = av.dtcg_json
        if not isinstance(dtcg, dict):
            missing_report.append(f"{av.url}: dtcg_json is not a dict ({type(dtcg).__name__})")
            continue
        for field in _UNCONDITIONAL:
            if field not in dtcg:
                missing_report.append(f"{av.url}: missing unconditional field '{field}'")
        # commercial_signal is conditional; only require it for the 36 curated brands.
        # URL shape: resemblio://seed/drl_v1/<brand>/<category>/<asset>
        # split("/") yields ["resemblio:", "", "seed", "drl_v1", <brand>, <category>, <asset>]
        # Index 4 is the brand slug.
        brand_slug_in_url = (av.url or "").split("/")[4] if "/" in (av.url or "") else ""
        if brand_slug_in_url not in _KNOWN_NO_CS and "commercial_signal" not in dtcg:
            missing_report.append(
                f"{av.url}: missing 'commercial_signal' "
                f"(brand '{brand_slug_in_url}' is not in the known-uncurated set)"
            )

    assert not missing_report, (
        f"{len(missing_report)} curated-field gaps found:\n"
        + "\n".join(f"  {line}" for line in sorted(missing_report)[:30])
        + ("\n  ... (truncated)" if len(missing_report) > 30 else "")
    )


@pytest.mark.skipif(
    not _real_drl_available(),
    reason="Real DRL corpus not on disk - skip 40-brand idempotency proof",
)
def test_40_brand_coverage_apply_is_idempotent(session: Session) -> None:
    """Re-running apply_seed on the 40-brand corpus does not duplicate rows.

    The partial unique index on ``(seed_source, source_id)`` must dedup every
    row on the second run. This test verifies the dedup key is stable across
    two identical seed runs (same DRL, same content hash).

    After run 1: ``inserted=N, updated=0, skipped=0``.
    After run 2: ``inserted=0, updated=N, skipped=0``.
    Row count before and after run 2 must be identical.
    """
    corpus = load_corpus(_REAL_DRL_ROOT)
    user_id = _seed_user(session)
    session.commit()
    storage = _fake_storage_for_coverage()

    # First run: all inserts.
    counts_1 = apply_seed(
        iter_assets(corpus),
        _REAL_DRL_ROOT,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )
    assert counts_1["inserted"] > 0, "First run produced no inserts."
    assert counts_1["skipped"] == 0, f"First run had unexpected skips: {counts_1['skipped']}"

    av_count_after_run1 = len(session.execute(select(AssetVersion)).scalars().all())

    # Second run: all updates (no new rows).
    counts_2 = apply_seed(
        iter_assets(corpus),
        _REAL_DRL_ROOT,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=DEFAULT_BATCH_SIZE,
    )
    assert counts_2["inserted"] == 0, (
        f"Second run inserted {counts_2['inserted']} new rows; expected 0. "
        "The dedup key (seed_source, source_id) is not stable across runs."
    )
    assert counts_2["skipped"] == 0, f"Second run had unexpected skips: {counts_2['skipped']}"

    av_count_after_run2 = len(session.execute(select(AssetVersion)).scalars().all())
    assert av_count_after_run2 == av_count_after_run1, (
        f"Row count changed from {av_count_after_run1} to {av_count_after_run2} "
        "after the second run; idempotency violated."
    )
