"""Tests for the DRL library bootstrap orchestrator + verify harness.

Covers:

- dry-run discovery + reporting (no DB writes)
- ``--single`` filter
- ``--limit`` cap
- ``--verify-only`` is read-only
- verify harness output schema + Markdown rendering

Uses a synthetic DRL fixture (``corpus.json`` + ``_extractions/<brand>/`` +
per-asset ``tokens.css`` files). No dependency on the real DRL on disk.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AssetVersion, Extraction
from scripts import bootstrap_drl_library as orch
from scripts import verify_drl_bootstrap as verifier
from scripts.bootstrap_drl_library import (
    BootstrapArgs,
    aggregate_report,
    discover_brand_dirs,
    normalize_library_slug,
    parse_args,
    process_brand_apply,
    process_brand_dry_run,
    select_brands,
)
from scripts.seed_from_drl import load_corpus
from scripts.verify_drl_bootstrap import collect_state, render_report


# --- Synthetic DRL fixture ---------------------------------------------------

_TOKENS_CSS = """
:root {
  --ds-bg: #FFFFFF;
  --ds-text: #111111;
  --ds-accent: #FF3366;
}
"""


_BRAND_SLUGS = ["aeon", "aesop", "airtable", "apple", "cloudflare"]
"""Five brands; lets us exercise --limit=3 and >1 brand counts."""


def _write_synthetic_drl(root: Path) -> None:
    """Write a corpus.json + per-brand _extractions/<slug>/ + tokens.css tree."""
    systems = []
    for slug in _BRAND_SLUGS:
        systems.append(
            {
                "slug": slug,
                "name": slug.title(),
                "tier": "A",
                "category": "editorial",
                "asset_count": 1,
                "assets": [
                    {
                        "slug": f"{slug}-asset-001",
                        "class": "buttons",
                        "kind": "atom",
                        "path": f"assets/atoms/buttons/{slug}",
                        "tokens_path": f"assets/atoms/buttons/{slug}/tokens.css",
                        "tldr": "primary",
                        "patterns": ["primary-square"],
                        "mood": ["confident"],
                        "applicable_to": ["saas"],
                        "tags": ["buttons"],
                        "provenance_score": "A",
                    }
                ],
            }
        )
    corpus = {
        "schema_version": 1,
        "generated": "2026-05-21",
        "asset_count": len(_BRAND_SLUGS),
        "system_count": len(_BRAND_SLUGS),
        "systems": systems,
    }
    (root / "corpus.json").write_text(json.dumps(corpus), encoding="utf-8")
    extractions_root = root / "_extractions"
    extractions_root.mkdir(parents=True, exist_ok=True)
    for slug in _BRAND_SLUGS:
        brand_dir = extractions_root / slug
        brand_dir.mkdir(parents=True, exist_ok=True)
        (brand_dir / "extraction.json").write_text("{}", encoding="utf-8")
        css_path = root / "assets" / "atoms" / "buttons" / slug / "tokens.css"
        css_path.parent.mkdir(parents=True, exist_ok=True)
        css_path.write_text(_TOKENS_CSS, encoding="utf-8")
    # Also drop a hidden + underscore-prefixed dir; discover_brand_dirs must skip both.
    (extractions_root / "_INBOX").mkdir(parents=True, exist_ok=True)
    (extractions_root / ".cache").mkdir(parents=True, exist_ok=True)


class _FakeStorage:
    """In-memory ``StorageClient`` for apply-mode tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object_at_key(self, key: str, body: bytes, content_type: str) -> None:
        assert content_type == "application/zip"
        self.objects[key] = body


@pytest.fixture
def drl_root(tmp_path: Path) -> Path:
    """Return a populated synthetic DRL root with 5 brands."""
    _write_synthetic_drl(tmp_path)
    return tmp_path


def _seed_user(session: Session) -> int:
    """Insert the minimal user row the seeder needs for FK satisfaction."""
    from app.crypto import hash_password
    from app.models import User

    user = User(email="seed@resemblio.test", password_hash=hash_password("x"), status="active")
    session.add(user)
    session.flush()
    return user.id


# --- Unit tests --------------------------------------------------------------

def test_normalize_library_slug_lowercases_and_replaces_underscores() -> None:
    """The library URL slug normalisation rule is documented + reversible."""
    assert normalize_library_slug("aeon") == "aeon"
    assert normalize_library_slug("Daring-Fireball") == "daring-fireball"
    assert normalize_library_slug("my_brand") == "my-brand"
    assert normalize_library_slug("  Padded ") == "padded"


def test_discover_brand_dirs_lists_only_visible_subdirs(drl_root: Path) -> None:
    """Hidden + underscore-prefixed dirs are filtered out."""
    dirs = discover_brand_dirs(drl_root)
    names = [d.name for d in dirs]
    assert names == sorted(_BRAND_SLUGS)
    assert "_INBOX" not in names
    assert ".cache" not in names


def test_discover_brand_dirs_raises_when_missing(tmp_path: Path) -> None:
    """A DRL root without ``_extractions/`` is operator error and surfaces fast."""
    with pytest.raises(FileNotFoundError):
        discover_brand_dirs(tmp_path)


def test_select_brands_single(drl_root: Path) -> None:
    """``--single aeon`` returns exactly the aeon dir."""
    dirs = discover_brand_dirs(drl_root)
    selected = select_brands(dirs, single="aeon", limit=None)
    assert [p.name for p in selected] == ["aeon"]


def test_select_brands_single_unknown_raises(drl_root: Path) -> None:
    """A ``--single`` value that matches no dir surfaces with the available list."""
    dirs = discover_brand_dirs(drl_root)
    with pytest.raises(ValueError, match="matched no brand dir"):
        select_brands(dirs, single="not-a-brand", limit=None)


def test_select_brands_limit(drl_root: Path) -> None:
    """``--limit 3`` caps the selection to the first three brands."""
    dirs = discover_brand_dirs(drl_root)
    selected = select_brands(dirs, single=None, limit=3)
    assert len(selected) == 3
    assert [p.name for p in selected] == sorted(_BRAND_SLUGS)[:3]


def test_parse_args_defaults_to_dry_run() -> None:
    """Default invocation is dry-run, not apply."""
    args: BootstrapArgs = parse_args(["--drl-root", "/tmp/drl"])
    assert args.apply is False
    assert args.verify_only is False
    assert args.single is None
    assert args.limit is None


# --- Behaviour tests ---------------------------------------------------------

def test_bootstrap_dry_run_reports_brand_list(drl_root: Path) -> None:
    """Dry-run mode produces one outcome per brand and writes no DB rows."""
    exit_code = orch.main(["--drl-root", str(drl_root)])
    assert exit_code == 0


def test_bootstrap_dry_run_outcomes_have_planned_counts(drl_root: Path) -> None:
    """``process_brand_dry_run`` returns one outcome per asset planned."""
    corpus = load_corpus(drl_root)
    dirs = discover_brand_dirs(drl_root)
    outcomes = [process_brand_dry_run(d, drl_root, corpus) for d in dirs]
    assert len(outcomes) == len(_BRAND_SLUGS)
    assert all(o["status"] == "dry-run" for o in outcomes)
    assert all(o["asset_count_planned"] == 1 for o in outcomes)


def test_bootstrap_single_brand_only_processes_aeon(
    drl_root: Path, session: Session
) -> None:
    """``--single aeon`` writes rows only for the aeon brand."""
    user_id = _seed_user(session)
    session.commit()
    corpus = load_corpus(drl_root)
    storage = _FakeStorage()
    outcome = process_brand_apply(
        drl_root / "_extractions" / "aeon",
        drl_root,
        corpus,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=25,
    )
    assert outcome["status"] == "ok"
    assert outcome["inserted"] == 1
    # No other brand seeded.
    extractions = session.execute(select(Extraction)).scalars().all()
    assert len(extractions) == 1
    assert extractions[0].source_id.startswith("aeon/")


def test_bootstrap_limit_caps_processed_brands(
    drl_root: Path, session: Session
) -> None:
    """``--limit 3`` only seeds the first three brands."""
    user_id = _seed_user(session)
    session.commit()
    corpus = load_corpus(drl_root)
    storage = _FakeStorage()
    dirs = discover_brand_dirs(drl_root)
    selected = select_brands(dirs, single=None, limit=3)
    outcomes = [
        process_brand_apply(d, drl_root, corpus, session, storage, user_id, 25)
        for d in selected
    ]
    assert len(outcomes) == 3
    assert all(o["status"] == "ok" for o in outcomes)
    extractions = session.execute(select(Extraction)).scalars().all()
    assert len(extractions) == 3


def test_bootstrap_verify_only_no_state_change(
    drl_root: Path, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--verify-only`` runs read-only queries and does not write."""
    user_id = _seed_user(session)
    session.commit()
    # Seed one brand so verify has something to report.
    corpus = load_corpus(drl_root)
    storage = _FakeStorage()
    process_brand_apply(
        drl_root / "_extractions" / "aeon",
        drl_root,
        corpus,
        session,
        storage,
        seed_user_id=user_id,
        batch_size=25,
    )
    session.commit()
    extractions_before = len(session.execute(select(Extraction)).scalars().all())
    av_before = len(session.execute(select(AssetVersion)).scalars().all())

    exit_code = orch.main(["--verify-only", "--drl-root", str(drl_root)])
    assert exit_code == 0

    # State unchanged.
    assert len(session.execute(select(Extraction)).scalars().all()) == extractions_before
    assert len(session.execute(select(AssetVersion)).scalars().all()) == av_before


def test_aggregate_report_rolls_totals() -> None:
    """``aggregate_report`` sums inserted/updated/skipped and tracks failures."""
    outcomes: list[orch.BrandOutcome] = [
        {
            "brand_dir": "aeon",
            "library_slug": "aeon",
            "corpus_system_slug": "aeon",
            "asset_count_planned": 2,
            "inserted": 2,
            "updated": 0,
            "skipped": 0,
            "status": "ok",
            "error": None,
        },
        {
            "brand_dir": "aesop",
            "library_slug": "aesop",
            "corpus_system_slug": "aesop",
            "asset_count_planned": 1,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "status": "failed",
            "error": "boom",
        },
    ]
    report = aggregate_report(Path("/x"), discovered=5, outcomes=outcomes)
    assert report.brands_discovered == 5
    assert report.brands_processed == 2
    assert report.totals_inserted == 2
    assert report.failed_brands == ["aesop"]


# --- Verify harness ---------------------------------------------------------

def test_verify_drl_bootstrap_reports_structure(
    drl_root: Path, session: Session, tmp_path: Path
) -> None:
    """Verifier output matches the documented schema + renders Markdown."""
    user_id = _seed_user(session)
    session.commit()
    corpus = load_corpus(drl_root)
    storage = _FakeStorage()
    # Seed all 5 brands so we have a populated state.
    for d in discover_brand_dirs(drl_root):
        process_brand_apply(d, drl_root, corpus, session, storage, user_id, 25)
    session.commit()

    result = collect_state(session)
    assert result["schema_version"] == 1
    assert result["extractions_drl"] == len(_BRAND_SLUGS)
    assert result["distinct_brand_slugs"] == len(_BRAND_SLUGS)
    assert set(result["jobs_by_status"].keys()) == {
        "pending",
        "running",
        "complete",
        "failed",
    }
    # 5 brands < expected floor (19); expectations should fail loudly.
    assert result["expectations_met"] is False
    assert any("below floor" in failure for failure in result["expectation_failures"])

    md = render_report(result)
    assert "# DRL bootstrap verification" in md
    assert "asset_versions (DRL-tagged)" in md
    assert "## Indexer jobs by status" in md


def test_verify_passes_when_brand_count_meets_floor(
    session: Session, monkeypatch: pytest.MonkeyPatch, drl_root: Path
) -> None:
    """When the expected-brand floor is patched low, verify passes cleanly."""
    user_id = _seed_user(session)
    session.commit()
    corpus = load_corpus(drl_root)
    storage = _FakeStorage()
    for d in discover_brand_dirs(drl_root):
        process_brand_apply(d, drl_root, corpus, session, storage, user_id, 25)
    session.commit()

    # Patch the floor down to 3 (our fixture has 5 brands).
    monkeypatch.setattr(verifier, "DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS", 3)
    result = collect_state(session)
    assert result["expectations_met"] is True
    assert result["expectation_failures"] == []


def test_verify_write_report_lands_dated_file(tmp_path: Path) -> None:
    """``write_report`` creates the dir and returns the written path."""
    fake_result: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": "2026-06-02T00:00:00+00:00",
        "asset_versions_drl": 0,
        "extractions_drl": 0,
        "distinct_brand_slugs": 0,
        "library_pages_total": 0,
        "library_pages_by_brand": {},
        "jobs_by_status": {"pending": 0, "running": 0, "complete": 0, "failed": 0},
        "quality_gate_eligible": 0,
        "quality_gate_filtered": 0,
        "expectations_met": False,
        "expectation_failures": ["test"],
    }
    md = render_report(fake_result)  # type: ignore[arg-type]
    out = verifier.write_report(md, tmp_path / "reports")
    assert out.exists()
    assert out.read_text(encoding="utf-8") == md
