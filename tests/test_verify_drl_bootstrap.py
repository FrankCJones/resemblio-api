"""Tests for scripts.verify_drl_bootstrap - DRL bootstrap verification harness.

These tests pin the threshold logic and report generation that make
``verify_drl_bootstrap`` the pass/fail oracle for the Phase 5 seed/drain
prod op. Without tests the threshold function is untestable production code;
a misconfigured constant or an off-by-one in the floor check would silently
produce a wrong pass/fail signal on prod.

TDD approach: the ``collect_state`` function requires a live DB session to
run queries, so we drive it through an in-memory SQLite session (the same
substrate the rest of the test suite uses via ``conftest.isolated_database``).
The ``render_report`` and ``write_report`` functions are pure-data; they get
tested directly against synthetic ``VerifyResult`` dicts.

The ``isolated_database`` autouse fixture in conftest.py wires up SQLite and
creates all tables before each test. Tests that need DB rows use the
``session`` fixture to insert them.

Authorization note: these tests run entirely offline. The gated prod ops
(``bootstrap_drl_library --apply``, indexer drain) are Frank/Jim YELLOW gates
and are not covered here.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.constants import (
    ASSET_VERSIONS_SEED_SOURCE_LABEL,
    DRL_BOOTSTRAP_EXPECTED_PAGES_PER_BRAND,
    DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS,
    DRL_BOOTSTRAP_REPORT_SCHEMA_VERSION,
)
from app.models import AssetVersion, Extraction, LibraryIndexJob, LibraryPage, User
from scripts.seed_from_drl import DRL_VERSION_LABEL_PREFIX
from scripts.verify_drl_bootstrap import (
    VerifyResult,
    collect_state,
    render_report,
    write_report,
)


# ---------------------------------------------------------------------------
# Helpers: seed rows into in-memory SQLite for collect_state tests
# ---------------------------------------------------------------------------

def _seed_drl_asset_version(session: Session, idx: int) -> AssetVersion:
    """Insert a DRL-tagged AssetVersion and return it.

    The ``version_label`` prefix ``DRL_VERSION_LABEL_PREFIX`` is what
    ``collect_state`` uses to distinguish DRL-seeded rows from organic
    customer extractions. Every DRL-tagged row needs a matching
    ``Extraction`` row to be counted in ``distinct_brand_slugs``.
    """
    av = AssetVersion(
        url=f"https://example-{idx}.test/",
        content_hash=f"sha256-{idx:032x}",
        dtcg_json={"color": {}},
        version_label=f"{DRL_VERSION_LABEL_PREFIX} 2026-06-07",
        manifest_schema_version=2,
    )
    session.add(av)
    session.flush()
    return av


def _seed_drl_extraction(
    session: Session,
    asset_version: AssetVersion,
    brand_slug: str,
    category_slug: str,
    user_id: int,
) -> Extraction:
    """Insert a DRL Extraction row for the given brand/category.

    The ``source_id`` format is ``<brand_slug>/<category_slug>/<asset_slug>``;
    ``collect_state`` splits on the first ``/`` to derive ``distinct_brand_slugs``.
    The ``seed_source`` value matches ``ASSET_VERSIONS_SEED_SOURCE_LABEL`` so
    ``collect_state`` counts it under DRL rows.
    """
    row = Extraction(
        user_id=user_id,
        url=asset_version.url,
        url_normalized=asset_version.url,
        status="ok",
        schema_version=2,
        seed_source=ASSET_VERSIONS_SEED_SOURCE_LABEL,
        source_id=f"{brand_slug}/{category_slug}/asset-{asset_version.id}",
        asset_version_id=asset_version.id,
    )
    session.add(row)
    session.flush()
    return row


def _seed_library_page(
    session: Session,
    asset_version: AssetVersion,
    brand_slug: str,
    category_slug: str,
) -> LibraryPage:
    """Insert a LibraryPage row for the given asset_version, brand, and category."""
    page = LibraryPage(
        asset_version_id=asset_version.id,
        category_slug=category_slug,
        brand_slug=brand_slug,
        metadata_json={"brand_slug": brand_slug},
        is_canonical=True,
    )
    session.add(page)
    session.flush()
    return page


def _seed_lib_index_job(
    session: Session,
    asset_version: AssetVersion,
    status: str = "complete",
) -> LibraryIndexJob:
    """Insert a LibraryIndexJob with the given status."""
    job = LibraryIndexJob(
        asset_version_id=asset_version.id,
        status=status,
    )
    session.add(job)
    session.flush()
    return job


def _seed_bootstrap_user(session: Session) -> User:
    """Return or create user id=1 as the DRL seed owner.

    The foreign-key constraint on ``extractions.user_id`` requires a user
    row to exist. We use a deterministic email so tests that call this
    helper multiple times within a single session do not collide.
    """
    from app.crypto import hash_password

    user = User(
        email="drl-seed@resemblio-test.internal",
        password_hash=hash_password("unused"),
        stripe_customer_id="cus_drl_seed_test",
        status="active",
    )
    session.add(user)
    session.flush()
    return user


# ---------------------------------------------------------------------------
# collect_state: happy path
# ---------------------------------------------------------------------------

class TestCollectStateHappyPath:
    """collect_state returns correct counts when the bootstrap floor is met."""

    def test_happy_path_expectations_met(self, session: Session) -> None:
        """When brand count >= floor and no failed jobs, expectations_met is True."""
        user = _seed_bootstrap_user(session)
        # Seed DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS distinct brands, each with
        # one asset_version + extraction + library_page.
        for i in range(DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS):
            av = _seed_drl_asset_version(session, i)
            _seed_drl_extraction(session, av, f"brand-{i}", "hero", user.id)
            _seed_library_page(session, av, f"brand-{i}", "hero")
            _seed_lib_index_job(session, av, status="complete")
        session.commit()

        result = collect_state(session)

        assert result["expectations_met"] is True
        assert result["expectation_failures"] == []
        assert result["distinct_brand_slugs"] >= DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS

    def test_schema_version_present(self, session: Session) -> None:
        """Result carries the expected schema_version constant."""
        _seed_bootstrap_user(session)
        session.commit()
        result = collect_state(session)
        assert result["schema_version"] == DRL_BOOTSTRAP_REPORT_SCHEMA_VERSION

    def test_generated_at_is_iso_string(self, session: Session) -> None:
        """generated_at is an ISO-8601 string (used in the Markdown report header)."""
        _seed_bootstrap_user(session)
        session.commit()
        result = collect_state(session)
        # Must be parseable as a datetime string; simple presence check.
        assert "T" in result["generated_at"] or "-" in result["generated_at"]


# ---------------------------------------------------------------------------
# collect_state: floor breach
# ---------------------------------------------------------------------------

class TestCollectStateFloorBreach:
    """collect_state fails when fewer than DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS
    distinct brand slugs are present in DRL-tagged extraction rows."""

    def test_zero_brands_fails(self, session: Session) -> None:
        """Empty DB -> distinct_brand_slugs=0 -> floor breach."""
        result = collect_state(session)
        assert result["expectations_met"] is False
        failure_text = " ".join(result["expectation_failures"])
        assert "distinct_brand_slugs" in failure_text or "floor" in failure_text

    def test_one_brand_below_floor_fails(self, session: Session) -> None:
        """One brand < floor -> expectations_met False."""
        user = _seed_bootstrap_user(session)
        av = _seed_drl_asset_version(session, 0)
        _seed_drl_extraction(session, av, "brand-only", "hero", user.id)
        session.commit()

        result = collect_state(session)

        assert result["expectations_met"] is False
        assert result["distinct_brand_slugs"] == 1

    def test_floor_minus_one_fails(self, session: Session) -> None:
        """Floor - 1 brands -> expectations_met False."""
        user = _seed_bootstrap_user(session)
        for i in range(DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS - 1):
            av = _seed_drl_asset_version(session, i)
            _seed_drl_extraction(session, av, f"brand-{i}", "hero", user.id)
        session.commit()

        result = collect_state(session)

        assert result["expectations_met"] is False

    def test_exact_floor_passes(self, session: Session) -> None:
        """Exactly floor brands -> expectations_met True (no failed jobs)."""
        user = _seed_bootstrap_user(session)
        for i in range(DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS):
            av = _seed_drl_asset_version(session, i)
            _seed_drl_extraction(session, av, f"brand-{i}", "hero", user.id)
        session.commit()

        result = collect_state(session)

        assert result["expectations_met"] is True

    def test_organic_extractions_not_counted(self, session: Session) -> None:
        """Organic extractions (seed_source=None) do NOT count toward DRL slugs.

        The DRL floor applies only to seed rows. A customer running 100
        organic extractions should not mask a missing DRL bootstrap.
        """
        user = _seed_bootstrap_user(session)
        # Seed an organic extraction (seed_source is None; not DRL).
        organic_av = AssetVersion(
            url="https://organic.example.com/",
            content_hash="organic-hash",
            dtcg_json={"color": {}},
            version_label=None,
            manifest_schema_version=2,
        )
        session.add(organic_av)
        session.flush()
        organic_ext = Extraction(
            user_id=user.id,
            url=organic_av.url,
            url_normalized=organic_av.url,
            status="ok",
            schema_version=2,
            seed_source=None,  # <-- NOT a DRL row
            source_id=None,
            asset_version_id=organic_av.id,
        )
        session.add(organic_ext)
        session.commit()

        result = collect_state(session)

        assert result["distinct_brand_slugs"] == 0
        assert result["expectations_met"] is False


# ---------------------------------------------------------------------------
# collect_state: failed jobs
# ---------------------------------------------------------------------------

class TestCollectStateFailedJobs:
    """collect_state fails when any library_index_jobs row is in 'failed' state."""

    def test_one_failed_job_fails_gate(self, session: Session) -> None:
        """Even with enough brands, one failed job flips expectations_met to False."""
        user = _seed_bootstrap_user(session)
        for i in range(DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS):
            av = _seed_drl_asset_version(session, i)
            _seed_drl_extraction(session, av, f"brand-{i}", "hero", user.id)
            _seed_lib_index_job(session, av, status="complete")
        # Add one failed job.
        failed_av = _seed_drl_asset_version(session, 999)
        _seed_drl_extraction(session, failed_av, "brand-failed", "buttons", user.id)
        _seed_lib_index_job(session, failed_av, status="failed")
        session.commit()

        result = collect_state(session)

        assert result["expectations_met"] is False
        failure_text = " ".join(result["expectation_failures"])
        assert "failed" in failure_text

    def test_jobs_by_status_counts_correctly(self, session: Session) -> None:
        """jobs_by_status dict has entries for all four statuses."""
        user = _seed_bootstrap_user(session)
        av1 = _seed_drl_asset_version(session, 0)
        _seed_drl_extraction(session, av1, "brand-0", "hero", user.id)
        _seed_lib_index_job(session, av1, status="complete")
        av2 = _seed_drl_asset_version(session, 1)
        _seed_drl_extraction(session, av2, "brand-1", "hero", user.id)
        _seed_lib_index_job(session, av2, status="pending")
        session.commit()

        result = collect_state(session)

        assert result["jobs_by_status"]["complete"] == 1
        assert result["jobs_by_status"]["pending"] == 1
        assert result["jobs_by_status"]["failed"] == 0
        assert result["jobs_by_status"]["running"] == 0

    def test_no_failed_jobs_with_floor_met_passes(self, session: Session) -> None:
        """Floor met + zero failed jobs -> expectations_met True."""
        user = _seed_bootstrap_user(session)
        for i in range(DRL_BOOTSTRAP_MIN_EXPECTED_BRANDS):
            av = _seed_drl_asset_version(session, i)
            _seed_drl_extraction(session, av, f"brand-{i}", "hero", user.id)
            _seed_lib_index_job(session, av, status="complete")
        session.commit()

        result = collect_state(session)

        assert result["expectations_met"] is True
        assert result["jobs_by_status"]["failed"] == 0


# ---------------------------------------------------------------------------
# collect_state: quality-gate arithmetic
# ---------------------------------------------------------------------------

class TestCollectStateQualityGate:
    """quality_gate_eligible / quality_gate_filtered arithmetic.

    An asset_version with a corresponding library_pages row is ``eligible``.
    One without any library_pages row is ``filtered``.
    """

    def test_all_av_have_pages_eligible_equals_total(self, session: Session) -> None:
        user = _seed_bootstrap_user(session)
        for i in range(3):
            av = _seed_drl_asset_version(session, i)
            _seed_drl_extraction(session, av, f"brand-{i}", "hero", user.id)
            _seed_library_page(session, av, f"brand-{i}", "hero")
        session.commit()

        result = collect_state(session)

        assert result["quality_gate_eligible"] == 3
        assert result["quality_gate_filtered"] == 0

    def test_av_without_page_is_filtered(self, session: Session) -> None:
        """An asset_version seeded but not yet indexed has no library_pages row."""
        user = _seed_bootstrap_user(session)
        av_with_page = _seed_drl_asset_version(session, 0)
        _seed_drl_extraction(session, av_with_page, "brand-0", "hero", user.id)
        _seed_library_page(session, av_with_page, "brand-0", "hero")

        av_no_page = _seed_drl_asset_version(session, 1)
        _seed_drl_extraction(session, av_no_page, "brand-1", "hero", user.id)
        # No library_page for av_no_page.
        session.commit()

        result = collect_state(session)

        assert result["quality_gate_eligible"] == 1
        assert result["quality_gate_filtered"] == 1

    def test_empty_db_has_zero_eligible_and_zero_filtered(
        self, session: Session
    ) -> None:
        result = collect_state(session)
        assert result["quality_gate_eligible"] == 0
        assert result["quality_gate_filtered"] == 0

    def test_library_pages_by_brand_populated(self, session: Session) -> None:
        """library_pages_by_brand maps brand_slug -> page count."""
        user = _seed_bootstrap_user(session)
        av1 = _seed_drl_asset_version(session, 0)
        _seed_drl_extraction(session, av1, "stripe", "hero", user.id)
        _seed_library_page(session, av1, "stripe", "hero")
        av2 = _seed_drl_asset_version(session, 1)
        _seed_drl_extraction(session, av2, "stripe", "buttons", user.id)
        _seed_library_page(session, av2, "stripe", "buttons")
        av3 = _seed_drl_asset_version(session, 2)
        _seed_drl_extraction(session, av3, "figma", "hero", user.id)
        _seed_library_page(session, av3, "figma", "hero")
        session.commit()

        result = collect_state(session)

        assert result["library_pages_by_brand"]["stripe"] == 2
        assert result["library_pages_by_brand"]["figma"] == 1
        assert result["library_pages_total"] == 3


# ---------------------------------------------------------------------------
# render_report: Markdown output shape
# ---------------------------------------------------------------------------

class TestRenderReport:
    """render_report produces well-formed Markdown from a VerifyResult."""

    def _make_result(
        self,
        *,
        brand_count: int = 20,
        failed_jobs: int = 0,
        expectations_met: bool = True,
        failures: list[str] | None = None,
    ) -> VerifyResult:
        return VerifyResult(
            schema_version=DRL_BOOTSTRAP_REPORT_SCHEMA_VERSION,
            generated_at="2026-06-07T19:39:00+00:00",
            asset_versions_drl=brand_count * 5,
            extractions_drl=brand_count * 5,
            distinct_brand_slugs=brand_count,
            library_pages_total=brand_count * 12,
            library_pages_by_brand={f"brand-{i}": 12 for i in range(brand_count)},
            jobs_by_status={
                "complete": brand_count * 5,
                "pending": 0,
                "running": 0,
                "failed": failed_jobs,
            },
            quality_gate_eligible=brand_count * 5,
            quality_gate_filtered=0,
            expectations_met=expectations_met,
            expectation_failures=failures or [],
        )

    def test_report_contains_schema_version(self) -> None:
        result = self._make_result()
        text = render_report(result)
        assert str(DRL_BOOTSTRAP_REPORT_SCHEMA_VERSION) in text

    def test_report_contains_expectations_met_true(self) -> None:
        result = self._make_result(expectations_met=True)
        text = render_report(result)
        assert "True" in text

    def test_report_contains_expectations_met_false(self) -> None:
        result = self._make_result(expectations_met=False, failures=["floor breach"])
        text = render_report(result)
        assert "False" in text

    def test_report_contains_failure_text_when_present(self) -> None:
        result = self._make_result(
            expectations_met=False, failures=["distinct_brand_slugs=1 below floor 19"]
        )
        text = render_report(result)
        assert "distinct_brand_slugs" in text

    def test_report_contains_brand_table(self) -> None:
        """Library pages section contains the per-brand table."""
        result = self._make_result(brand_count=2)
        text = render_report(result)
        assert "brand-0" in text
        assert "brand-1" in text

    def test_report_meets_floor_column(self) -> None:
        """Each brand's page count is compared against DRL_BOOTSTRAP_EXPECTED_PAGES_PER_BRAND."""
        result = self._make_result(brand_count=1)
        # With 12 pages and floor=10, it should say "yes".
        text = render_report(result)
        assert "yes" in text

    def test_empty_pages_shows_not_yet_indexed_message(self) -> None:
        """When library_pages_by_brand is empty, the report says so clearly."""
        result = VerifyResult(
            schema_version=DRL_BOOTSTRAP_REPORT_SCHEMA_VERSION,
            generated_at="2026-06-07T00:00:00+00:00",
            asset_versions_drl=0,
            extractions_drl=0,
            distinct_brand_slugs=0,
            library_pages_total=0,
            library_pages_by_brand={},
            jobs_by_status={"complete": 0, "pending": 0, "running": 0, "failed": 0},
            quality_gate_eligible=0,
            quality_gate_filtered=0,
            expectations_met=False,
            expectation_failures=["distinct_brand_slugs=0 below floor 19"],
        )
        text = render_report(result)
        assert "no library_pages" in text or "not yet" in text or "indexer" in text


# ---------------------------------------------------------------------------
# write_report: file creation
# ---------------------------------------------------------------------------

class TestWriteReport:
    """write_report creates a dated Markdown file in the target directory."""

    def test_write_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            report_text = "# DRL bootstrap verification\n\n- expectations_met: True\n"
            out_path = write_report(report_text, tmp_path)
            assert out_path.exists()
            assert out_path.suffix == ".md"
            assert out_path.read_text(encoding="utf-8") == report_text

    def test_write_filename_contains_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = write_report("# test\n", Path(tmp))
            # Filename should be <date>-drl-bootstrap-verify.md
            assert "drl-bootstrap-verify" in out_path.name

    def test_write_creates_parent_dir_if_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "reports" / "2026-06-07"
            assert not nested.exists()
            write_report("# test\n", nested)
            assert nested.exists()
