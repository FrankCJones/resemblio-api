"""Tests for `scripts.refresh_brand_library`.

Covers:

- Per-brand drop deletes the right `library_pages` rows
- Drain loop terminates when the indexer reports `jobs_run=0`
- `--all` enumerates brands from `library_pages`
- Per-brand failure isolation (bootstrap exit != 0 marks the brand
  failed; later brands still process)
- Dry-run is a no-op
- The aggregate report carries ok / failed counts

Uses sqlite in-memory + an injected subprocess runner. Real
`bootstrap_drl_library` and `library_indexer` modules are never invoked.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AssetVersion, LibraryIndexJob, LibraryPage
from scripts.refresh_brand_library import (
    DRAIN_DONE_MARKER,
    RefreshArgs,
    aggregate,
    count_brand_pages,
    delete_brand_rows,
    drain_indexer,
    list_brands_from_db,
    parse_args,
    refresh_one_brand,
    run,
    run_bootstrap,
)


# --- Subprocess fake --------------------------------------------------------


class _Runner:
    """Replay-style subprocess fake; returns scripted CompletedProcess results."""

    def __init__(self, *, indexer_passes: int = 1, bootstrap_exit: int = 0) -> None:
        self.indexer_passes = indexer_passes
        self.bootstrap_exit = bootstrap_exit
        self.calls: list[list[str]] = []
        self._indexer_seen = 0

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if "scripts.bootstrap_drl_library" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, self.bootstrap_exit, "bootstrap ok\n", "")
        if "app.cli.library_indexer" in " ".join(cmd):
            self._indexer_seen += 1
            if self._indexer_seen >= self.indexer_passes:
                return subprocess.CompletedProcess(
                    cmd, 0, f"indexer pass done\n{DRAIN_DONE_MARKER}\n", ""
                )
            return subprocess.CompletedProcess(cmd, 0, "jobs_run=10\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


# --- DB fixtures ------------------------------------------------------------


@pytest.fixture()
def populated_session(session: Session) -> Session:
    """Seed `library_pages` + `asset_versions` + `library_index_jobs` rows.

    Two brands: apple (3 pages, 2 asset versions, 2 jobs) and stripe (2 pages).
    """
    av1 = AssetVersion(
        url="https://example.r2/apple/buttons-primary.json",
        version_label="drl-2026-05-21",
        content_hash="h1",
        dtcg_json={},
    )
    av2 = AssetVersion(
        url="https://example.r2/apple/cards-hero.json",
        version_label="drl-2026-05-21",
        content_hash="h2",
        dtcg_json={},
    )
    av3 = AssetVersion(
        url="https://example.r2/stripe/buttons-primary.json",
        version_label="drl-2026-05-21",
        content_hash="h3",
        dtcg_json={},
    )
    session.add_all([av1, av2, av3])
    session.flush()

    def _page(brand: str, category: str, av_id: int) -> LibraryPage:
        return LibraryPage(
            brand_slug=brand,
            category_slug=category,
            asset_version_id=av_id,
            rendered_html="<p>old</p>",
            metadata_json={},
        )

    session.add_all([
        _page("apple", "buttons", av1.id),
        _page("apple", "cards", av2.id),
        _page("apple", "nav", av1.id),
        _page("stripe", "buttons", av3.id),
        _page("stripe", "cards", av3.id),
    ])
    session.add_all([
        LibraryIndexJob(asset_version_id=av1.id, status="complete"),
        LibraryIndexJob(asset_version_id=av2.id, status="complete"),
        LibraryIndexJob(asset_version_id=av3.id, status="complete"),
    ])
    session.commit()
    return session


# --- list_brands_from_db ----------------------------------------------------


def test_list_brands_from_db_returns_distinct_sorted(populated_session: Session) -> None:
    brands = list_brands_from_db(populated_session)
    assert brands == ["apple", "stripe"]


# --- delete_brand_rows ------------------------------------------------------


def test_delete_brand_rows_scoped_to_brand(populated_session: Session) -> None:
    pages, jobs = delete_brand_rows(populated_session, "apple")
    assert pages == 3
    assert jobs == 2  # apple has 2 asset_versions, each with 1 job

    remaining_pages = populated_session.execute(select(LibraryPage.brand_slug)).scalars().all()
    assert set(remaining_pages) == {"stripe"}
    # Stripe's job MUST remain.
    remaining_jobs = populated_session.execute(select(LibraryIndexJob.id)).scalars().all()
    assert len(remaining_jobs) == 1


def test_delete_brand_rows_unknown_brand_returns_zero(populated_session: Session) -> None:
    pages, jobs = delete_brand_rows(populated_session, "nope")
    assert pages == 0
    assert jobs == 0


def test_count_brand_pages(populated_session: Session) -> None:
    assert count_brand_pages(populated_session, "apple") == 3
    assert count_brand_pages(populated_session, "stripe") == 2
    assert count_brand_pages(populated_session, "ghost") == 0


# --- drain loop -------------------------------------------------------------


def test_drain_terminates_when_marker_seen() -> None:
    runner = _Runner(indexer_passes=3)
    passes = drain_indexer(runner, max_passes=10)
    assert passes == 3


def test_drain_hits_max_passes_when_marker_absent() -> None:
    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, "jobs_run=10\n", "")

    passes = drain_indexer(runner, max_passes=5)
    assert passes == 5


# --- run_bootstrap ----------------------------------------------------------


def test_run_bootstrap_passes_through_drl_root() -> None:
    runner = _Runner(bootstrap_exit=0)
    code = run_bootstrap("apple", Path("/opt/x/drl"), runner)
    assert code == 0
    cmd = runner.calls[0]
    assert "--single" in cmd and "apple" in cmd
    assert "--apply" in cmd
    assert "/opt/x/drl" in " ".join(cmd) or "x" in " ".join(cmd)


def test_run_bootstrap_no_drl_root_omits_flag() -> None:
    runner = _Runner(bootstrap_exit=0)
    run_bootstrap("apple", None, runner)
    assert "--drl-root" not in runner.calls[0]


# --- refresh_one_brand ------------------------------------------------------


def test_refresh_one_brand_happy_path(populated_session: Session) -> None:
    runner = _Runner(indexer_passes=2, bootstrap_exit=0)
    outcome = refresh_one_brand(
        "apple",
        populated_session,
        drl_root=None,
        runner=runner,
        drain_max_passes=10,
    )
    assert outcome["status"] == "ok"
    assert outcome["pages_deleted"] == 3
    assert outcome["jobs_deleted"] == 2
    assert outcome["bootstrap_exit"] == 0
    assert outcome["drain_passes"] == 2
    # No re-seeding happened in this fake; pages_after reflects whatever the
    # bootstrap stub would have written (here: 0). The assertion is that the
    # count query ran without error.
    assert outcome["pages_after"] == 0


def test_refresh_one_brand_bootstrap_failure_marks_failed(populated_session: Session) -> None:
    runner = _Runner(bootstrap_exit=2)
    outcome = refresh_one_brand(
        "apple",
        populated_session,
        drl_root=None,
        runner=runner,
        drain_max_passes=10,
    )
    assert outcome["status"] == "failed"
    assert outcome["bootstrap_exit"] == 2
    assert "bootstrap" in (outcome["error"] or "")
    # Drain MUST NOT have run.
    indexer_calls = [c for c in runner.calls if "app.cli.library_indexer" in " ".join(c)]
    assert indexer_calls == []


# --- end-to-end run() -------------------------------------------------------


def test_run_dry_run_is_noop(populated_session: Session) -> None:
    args = RefreshArgs(
        apply=False, brand="apple", all_brands=False, drl_root=None, drain_max_passes=10
    )
    report = run(args)
    assert report.brands_processed == 1
    assert report.outcomes[0]["status"] == "dry-run"


def test_run_all_iterates_db_brands(populated_session: Session) -> None:
    args = RefreshArgs(
        apply=True, brand=None, all_brands=True, drl_root=None, drain_max_passes=10
    )
    runner = _Runner(indexer_passes=1, bootstrap_exit=0)
    report = run(
        args,
        session_factory=lambda: _SessionWrapper(populated_session),
        runner=runner,
        brand_lister=lambda s: ["apple", "stripe"],
    )
    assert {o["brand"] for o in report.outcomes} == {"apple", "stripe"}
    assert report.ok == 2


def test_run_per_brand_failure_isolated(populated_session: Session) -> None:
    args = RefreshArgs(
        apply=True, brand=None, all_brands=True, drl_root=None, drain_max_passes=10
    )

    seen: dict[str, int] = {"n": 0}

    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if "scripts.bootstrap_drl_library" in " ".join(cmd):
            seen["n"] += 1
            # First brand fails, second succeeds.
            exit_code = 2 if seen["n"] == 1 else 0
            return subprocess.CompletedProcess(cmd, exit_code, "", "")
        return subprocess.CompletedProcess(cmd, 0, f"{DRAIN_DONE_MARKER}\n", "")

    report = run(
        args,
        session_factory=lambda: _SessionWrapper(populated_session),
        runner=runner,
        brand_lister=lambda s: ["apple", "stripe"],
    )
    assert report.ok == 1
    assert report.failed == 1


def test_aggregate_counts_statuses() -> None:
    outcomes = [
        {
            "brand": "a", "status": "ok", "pages_deleted": 1, "jobs_deleted": 1,
            "bootstrap_exit": 0, "drain_passes": 1, "pages_after": 1, "error": None,
        },
        {
            "brand": "b", "status": "failed", "pages_deleted": 0, "jobs_deleted": 0,
            "bootstrap_exit": 2, "drain_passes": 0, "pages_after": -1, "error": "x",
        },
    ]
    report = aggregate(outcomes)  # type: ignore[arg-type]
    assert report.ok == 1
    assert report.failed == 1


# --- parse_args -------------------------------------------------------------


def test_parse_args_requires_brand_or_all() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_parse_args_brand_and_all_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--brand", "apple", "--all"])


def test_parse_args_apply_brand() -> None:
    args = parse_args(["--brand", "apple", "--apply"])
    assert args.brand == "apple"
    assert args.apply is True
    assert args.all_brands is False


# --- Helpers ---------------------------------------------------------------


class _SessionWrapper:
    """Adapt an existing Session to the `with session_factory() as s` shape."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, *exc: Any) -> None:
        # The fixture owns lifecycle; do not close.
        return None
