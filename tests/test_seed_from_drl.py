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
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

# The ``transformer`` package lives at ``code/transformer/`` (workspace-level),
# one directory above the API root. Tests run from ``code/api/`` so we
# explicitly extend ``sys.path`` before importing it.
_CODE_ROOT = Path(__file__).resolve().parents[2]
if str(_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CODE_ROOT))

from app.models import Extraction
from scripts import seed_from_drl as seeder
from scripts.seed_from_drl import (
    DEFAULT_BATCH_SIZE,
    SEED_SOURCE_DRL_V1,
    apply_seed,
    build_bundle,
    iter_assets,
    load_corpus,
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
    assert counts == {"inserted": 2, "updated": 0, "skipped": 0}

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
    assert second == {"inserted": 0, "updated": 2, "skipped": 0}

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


def test_dry_run_prints_what_would_be_seeded(drl_root: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Dry-run emits per-asset plan lines covering source_id and token count."""
    with caplog.at_level("INFO", logger=seeder.LOG.name):
        exit_code = seeder.main(["--drl-root", str(drl_root)])
    assert exit_code == 0
    messages = " ".join(record.message for record in caplog.records)
    assert "acme/alphabets/acme" in messages
    assert "globex/buttons/globex-button-001" in messages
    # tokens count for the acme fixture is 3 (ds-bg, ds-text, ds-accent)
    assert "tokens=3" in messages
    # tokens count for the globex fixture is 2 (ds-bg, ds-text)
    assert "tokens=2" in messages
    assert "DRY RUN" in messages


def test_apply_skips_assets_without_tokens(session: Session, drl_root: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Assets whose tokens.css is missing on disk are skipped, not crashed."""
    user_id = _seed_user(session)
    session.commit()
    # Remove one tokens.css so that asset is skipped.
    (drl_root / "assets" / "alphabets" / "acme" / "tokens.css").unlink()

    storage = _FakeStorage()
    with caplog.at_level("WARNING", logger=seeder.LOG.name):
        counts = apply_seed(
            iter_assets(load_corpus(drl_root)),
            drl_root,
            session,
            storage,
            seed_user_id=user_id,
            batch_size=DEFAULT_BATCH_SIZE,
        )
    assert counts == {"inserted": 1, "updated": 0, "skipped": 1}
    assert any("no tokens.css" in record.message for record in caplog.records)
