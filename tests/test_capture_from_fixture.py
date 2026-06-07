"""Tests for the fixture-capture registry and the capture-from-fixture script.

Phases:

Phase 1 (registry) - these tests assert structural correctness of
``extractor.fixture_capture_registry``: each brand entry carries the
required fields, fixture files resolve on disk, and no brand is listed
in BOTH the fixture registry (capturable from saved markup) and
DOCUMENTED_SKIP_BRANDS (no real markup anywhere).

Phase 2 (script) - dep-free unit tests using an injected ``capture_fn``
cover the envelope/provenance builder, the write path, and the < 4-field
gate. An opt-in real-render test (``RESEMBLIO_RUN_REAL_BROWSER=1``)
runs the full write path with live Playwright against the openai fixture.

Quality floor: docstrings on every public function, TypedDict shapes,
named constants, no bare dicts, no magic strings, no Playwright in
dep-free tests.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Cross-test imports (same pattern as test_button_selector_fixtures.py)
# ---------------------------------------------------------------------------

from tests.test_button_corpus_coverage import (
    DEFAULT_PLACEHOLDER_VALUES,
    DOCUMENTED_SKIP_BRANDS,
    OPENAI_REQUIRED_NON_DEFAULT_FIELDS,
    TRACKED_BUTTON_FIELDS,
    _brand_has_real_button_styles,
    _load_cta_properties,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURE_DIR: Path = Path(__file__).parent / "fixtures" / "button_capture"
"""Directory holding the pinned real-markup HTML fixtures (git-tracked)."""

_OPENAI_FIXTURE: Path = _FIXTURE_DIR / "openai_homepage.html"

OPENAI_CANONICAL_URL: str = "https://openai.com"
"""Expected canonical URL for openai in the fixture registry."""

# Minimum non-default fields a valid captured snapshot must carry.
# Mirrors OPENAI_REQUIRED_NON_DEFAULT_FIELDS from the corpus test.
_FIELD_FLOOR: int = OPENAI_REQUIRED_NON_DEFAULT_FIELDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_synthetic_cta_report(non_default_count: int = 5) -> dict[str, Any]:
    """Build a synthetic ``ComputedStyleReport`` with a cta slot.

    ``non_default_count`` controls how many of TRACKED_BUTTON_FIELDS carry
    a non-default value in the cta properties dict. Values are real-looking
    CSS strings, not DEFAULT_PLACEHOLDER_VALUES sentinels.

    Returns a plain dict matching the ComputedStyleReport TypedDict shape.
    """
    # Build cta properties: first N fields get a real value, rest get default.
    cta_properties: dict[str, str] = {}
    real_values = {
        "border-radius": "9999px",
        "padding": "12px 24px",
        "font-family": "'Söhne', sans-serif",
        "background-color": "rgb(16, 163, 127)",
        "color": "rgb(255, 255, 255)",
        "border": "1px solid rgb(16, 163, 127)",
    }
    fields = list(TRACKED_BUTTON_FIELDS)
    for i, field_name in enumerate(fields):
        if i < non_default_count:
            cta_properties[field_name] = real_values.get(field_name, f"real-value-{i}")
        else:
            cta_properties[field_name] = "0px"  # a DEFAULT_PLACEHOLDER_VALUES sentinel

    return {
        "status": "ok",
        "signals": [
            {"slot": "root", "selector": "html", "properties": {"color": "rgb(0, 0, 0)"}},
            {"slot": "cta", "selector": "a[href^='https://chatgpt.com']", "properties": cta_properties},
        ],
        "error": None,
        "schema_version": 1,
    }


def _count_non_default_fields_in_report(report: dict[str, Any]) -> int:
    """Count non-default TRACKED_BUTTON_FIELDS in the cta slot of a report dict."""
    signals = report.get("signals", [])
    for signal in signals:
        if signal.get("slot") == "cta":
            props = signal.get("properties", {})
            count = 0
            for field_name in TRACKED_BUTTON_FIELDS:
                value = props.get(field_name, "").strip()
                if value and value not in DEFAULT_PLACEHOLDER_VALUES:
                    count += 1
            return count
    return 0


# ===========================================================================
# Phase 1 - Fixture-capture registry
# ===========================================================================


class TestFixtureCaptureRegistry:
    """Structural correctness of ``extractor.fixture_capture_registry``."""

    def test_openai_entry_exists(self) -> None:
        """FIXTURE_CAPTURE_BRANDS must contain an entry for 'openai'."""
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        assert "openai" in FIXTURE_CAPTURE_BRANDS, (
            "FIXTURE_CAPTURE_BRANDS must contain an 'openai' entry. "
            "openai is fixture-capturable (real 419 KB SSR fixture) but "
            "live-blocked by Cloudflare Turnstile (2026-06-06)."
        )

    def test_openai_fixture_file_resolves(self) -> None:
        """openai entry's fixture_filename must resolve to an existing file."""
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS, fixture_path  # type: ignore[import]

        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        assert spec["fixture_filename"], "fixture_filename must be non-empty"
        path = fixture_path("openai")
        assert path.exists(), (
            f"fixture_path('openai') -> {path} does not exist. "
            "The openai fixture must be present at "
            "tests/fixtures/button_capture/<fixture_filename>."
        )

    def test_openai_canonical_url(self) -> None:
        """openai entry's canonical_url must be the openai homepage."""
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        url = spec["canonical_url"]
        assert url, "canonical_url must be non-empty"
        assert "openai.com" in url, (
            f"canonical_url {url!r} must reference openai.com. "
            "This is the live URL the fixture was captured from."
        )

    def test_openai_fixture_captured_at_is_iso_date(self) -> None:
        """openai entry's fixture_captured_at must be a parseable ISO 8601 date."""
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        raw = spec["fixture_captured_at"]
        assert raw, "fixture_captured_at must be non-empty"
        try:
            parsed = datetime.date.fromisoformat(raw[:10])  # accept YYYY-MM-DD prefix
        except ValueError as exc:
            raise AssertionError(
                f"fixture_captured_at {raw!r} is not a valid ISO date: {exc}"
            ) from exc
        # Sanity: the fixture cannot have been captured before Resemblio existed
        assert parsed >= datetime.date(2026, 1, 1), (
            f"fixture_captured_at {raw!r} predates Resemblio. Check the value."
        )

    def test_openai_capture_reason_nonempty(self) -> None:
        """openai entry's capture_reason must explain why live capture is unavailable."""
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        reason = spec["capture_reason"]
        assert isinstance(reason, str) and len(reason) > 10, (
            f"capture_reason must be a descriptive string; got {reason!r}. "
            "It must explain WHY live capture is unavailable so the next "
            "developer understands the condition without reading the ADR."
        )

    def test_no_brand_in_both_registries(self) -> None:
        """No brand must appear in BOTH FIXTURE_CAPTURE_BRANDS and DOCUMENTED_SKIP_BRANDS.

        A brand is either fixture-capturable (real markup exists, can derive
        real styles) or has no capturable markup (permanent skip). The two
        categories are mutually exclusive. Overlap would mean a brand is
        treated as both "capture from fixture" and "skip permanently,"
        producing an ambiguous and untestable state.
        """
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        overlap = set(FIXTURE_CAPTURE_BRANDS.keys()) & DOCUMENTED_SKIP_BRANDS
        assert not overlap, (
            f"Brands in BOTH FIXTURE_CAPTURE_BRANDS and DOCUMENTED_SKIP_BRANDS: {overlap!r}. "
            "These are mutually exclusive registries. "
            "Remove from one or the other."
        )

    def test_fixture_path_resolves_via_fixture_dir_override(self) -> None:
        """fixture_path() must accept a custom fixture_dir argument for testability."""
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS, fixture_path  # type: ignore[import]

        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        # Using the real fixture dir explicitly - must still resolve.
        path = fixture_path("openai", fixture_dir=_FIXTURE_DIR)
        expected = _FIXTURE_DIR / spec["fixture_filename"]
        assert path == expected, (
            f"fixture_path('openai', fixture_dir={_FIXTURE_DIR!r}) -> {path} "
            f"but expected {expected}"
        )

    def test_all_registry_entries_have_required_fields(self) -> None:
        """Every entry in FIXTURE_CAPTURE_BRANDS must carry all FixtureCaptureSpec fields."""
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        required = {"fixture_filename", "canonical_url", "fixture_captured_at", "capture_reason"}
        for slug, spec in FIXTURE_CAPTURE_BRANDS.items():
            missing = required - set(spec.keys())
            assert not missing, (
                f"FIXTURE_CAPTURE_BRANDS[{slug!r}] is missing fields: {missing!r}. "
                "All FixtureCaptureSpec fields are mandatory."
            )
            for field in required:
                assert spec[field], (  # type: ignore[literal-required]
                    f"FIXTURE_CAPTURE_BRANDS[{slug!r}][{field!r}] must be non-empty."
                )


# ===========================================================================
# Phase 2 - Fixture-capture script: dep-free unit tests
# ===========================================================================


class TestBuildFixtureSnapshotEnvelope:
    """Dep-free tests for the provenance-envelope builder.

    Uses a synthetic ComputedStyleReport (no Playwright, no network)
    injected via the script's ``capture_fn`` parameter.
    """

    def test_envelope_carries_capture_source_fixture(self) -> None:
        """Envelope must include capture_source='fixture'."""
        from scripts.capture_button_snapshot_from_fixture import build_fixture_snapshot_envelope  # type: ignore[import]
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        report = _make_synthetic_cta_report(non_default_count=5)
        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        envelope = build_fixture_snapshot_envelope(report=report, brand_slug="openai", spec=spec)

        assert envelope["capture_source"] == "fixture", (
            f"envelope['capture_source'] must be 'fixture'; got {envelope.get('capture_source')!r}"
        )

    def test_envelope_carries_fixture_path_key(self) -> None:
        """Envelope must include fixture_path (repo-relative or absolute)."""
        from scripts.capture_button_snapshot_from_fixture import build_fixture_snapshot_envelope  # type: ignore[import]
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        report = _make_synthetic_cta_report(non_default_count=5)
        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        envelope = build_fixture_snapshot_envelope(report=report, brand_slug="openai", spec=spec)

        assert "fixture_path" in envelope and envelope["fixture_path"], (
            "envelope must include a non-empty 'fixture_path' key for provenance."
        )

    def test_envelope_carries_fixture_captured_at(self) -> None:
        """Envelope must include fixture_captured_at from the registry spec."""
        from scripts.capture_button_snapshot_from_fixture import build_fixture_snapshot_envelope  # type: ignore[import]
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        report = _make_synthetic_cta_report(non_default_count=5)
        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        envelope = build_fixture_snapshot_envelope(report=report, brand_slug="openai", spec=spec)

        assert envelope.get("fixture_captured_at") == spec["fixture_captured_at"], (
            "envelope['fixture_captured_at'] must match the registry spec value."
        )

    def test_envelope_carries_capture_reason(self) -> None:
        """Envelope must include capture_reason from the registry spec."""
        from scripts.capture_button_snapshot_from_fixture import build_fixture_snapshot_envelope  # type: ignore[import]
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        report = _make_synthetic_cta_report(non_default_count=5)
        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        envelope = build_fixture_snapshot_envelope(report=report, brand_slug="openai", spec=spec)

        assert envelope.get("capture_reason") == spec["capture_reason"], (
            "envelope['capture_reason'] must match the registry spec value."
        )

    def test_envelope_carries_captured_url(self) -> None:
        """Envelope must include captured_url matching the canonical_url."""
        from scripts.capture_button_snapshot_from_fixture import build_fixture_snapshot_envelope  # type: ignore[import]
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        report = _make_synthetic_cta_report(non_default_count=5)
        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        envelope = build_fixture_snapshot_envelope(report=report, brand_slug="openai", spec=spec)

        assert "captured_url" in envelope, "envelope must include 'captured_url'"
        assert spec["canonical_url"] in (envelope.get("captured_url") or ""), (
            f"envelope['captured_url'] must reference the canonical URL. "
            f"Got {envelope.get('captured_url')!r}, expected to include {spec['canonical_url']!r}."
        )

    def test_envelope_carries_envelope_schema_version(self) -> None:
        """Envelope must include envelope_schema_version (int)."""
        from scripts.capture_button_snapshot_from_fixture import build_fixture_snapshot_envelope  # type: ignore[import]
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        report = _make_synthetic_cta_report(non_default_count=5)
        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        envelope = build_fixture_snapshot_envelope(report=report, brand_slug="openai", spec=spec)

        assert isinstance(envelope.get("envelope_schema_version"), int), (
            "envelope['envelope_schema_version'] must be an int."
        )

    def test_envelope_preserves_report_body(self) -> None:
        """Envelope must preserve the ComputedStyleReport keys at the top level."""
        from scripts.capture_button_snapshot_from_fixture import build_fixture_snapshot_envelope  # type: ignore[import]
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        report = _make_synthetic_cta_report(non_default_count=5)
        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        envelope = build_fixture_snapshot_envelope(report=report, brand_slug="openai", spec=spec)

        # The ComputedStyleReport core keys must be preserved at top level
        # (same pattern as capture_all_button_snapshots.py's capture_one_brand).
        for key in ("status", "signals", "schema_version"):
            assert key in envelope, (
                f"envelope must include the ComputedStyleReport key '{key}' "
                "at top level so the snapshot loader can cast it directly."
            )
        assert envelope["status"] == report["status"]
        assert envelope["signals"] == report["signals"]

    def test_envelope_inspects_as_having_real_button_styles(self) -> None:
        """An envelope with >= 4 non-default fields must pass _brand_has_real_button_styles.

        This test uses a synthetic tmp runtime root to avoid touching the real
        seed tree. It proves that: (a) the envelope shape is compatible with
        _brand_has_real_button_styles; (b) a snapshot produced by the script
        with a good capture will self-heal the acceptance test.
        """
        from scripts.capture_button_snapshot_from_fixture import build_fixture_snapshot_envelope  # type: ignore[import]
        from extractor.fixture_capture_registry import FIXTURE_CAPTURE_BRANDS  # type: ignore[import]

        report = _make_synthetic_cta_report(non_default_count=5)
        spec = FIXTURE_CAPTURE_BRANDS["openai"]
        envelope = build_fixture_snapshot_envelope(report=report, brand_slug="openai", spec=spec)

        # Write to a tmp runtime root and point RESEMBLIO_RUNTIME_DATA_ROOT at it.
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_dir = Path(tmp) / "computed_styles"
            snapshot_dir.mkdir()
            (snapshot_dir / "openai.json").write_text(
                json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8"
            )
            old_env = os.environ.get("RESEMBLIO_RUNTIME_DATA_ROOT")
            try:
                os.environ["RESEMBLIO_RUNTIME_DATA_ROOT"] = tmp
                passed, reason = _brand_has_real_button_styles("openai")
            finally:
                if old_env is None:
                    os.environ.pop("RESEMBLIO_RUNTIME_DATA_ROOT", None)
                else:
                    os.environ["RESEMBLIO_RUNTIME_DATA_ROOT"] = old_env

        assert passed, (
            f"An envelope with 5 non-default fields must pass _brand_has_real_button_styles. "
            f"Reason: {reason}"
        )


class TestFixtureCaptureScriptWrite:
    """Dep-free tests for the script's write path (injected capture_fn)."""

    def test_script_writes_openai_json_to_outdir(self) -> None:
        """Script must write <out_dir>/openai.json when capture_fn returns good report."""
        from scripts.capture_button_snapshot_from_fixture import run_fixture_capture  # type: ignore[import]

        good_report = _make_synthetic_cta_report(non_default_count=5)

        def fake_capture_fn(html: str | None, url: str | None, timeout_ms: int, brand_slug: str | None) -> dict[str, Any]:
            return good_report

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            run_fixture_capture("openai", out_dir=out_dir, capture_fn=fake_capture_fn)
            out_file = out_dir / "openai.json"
            assert out_file.exists(), (
                f"Script must write {out_file} when capture_fn returns a good report."
            )
            written = json.loads(out_file.read_text(encoding="utf-8"))
            assert written.get("capture_source") == "fixture", (
                "Written snapshot must carry capture_source='fixture'."
            )
            assert written.get("status") == "ok", (
                "Written snapshot must carry status='ok'."
            )

    def test_script_written_file_passes_corpus_helper(self) -> None:
        """Snapshot written by the script must pass _brand_has_real_button_styles.

        ``_candidate_snapshot_dirs`` resolves ``RESEMBLIO_RUNTIME_DATA_ROOT``
        + ``/computed_styles`` as the first candidate. So we set out_dir to
        ``tmp/computed_styles`` and point RESEMBLIO_RUNTIME_DATA_ROOT at
        ``tmp``, matching the production resolver's lookup order.
        """
        from scripts.capture_button_snapshot_from_fixture import run_fixture_capture  # type: ignore[import]

        good_report = _make_synthetic_cta_report(non_default_count=5)

        def fake_capture_fn(html: str | None, url: str | None, timeout_ms: int, brand_slug: str | None) -> dict[str, Any]:
            return good_report

        with tempfile.TemporaryDirectory() as tmp:
            # out_dir = computed_styles subdir; runtime root = tmp parent.
            out_dir = Path(tmp) / "computed_styles"
            run_fixture_capture("openai", out_dir=out_dir, capture_fn=fake_capture_fn)
            old_env = os.environ.get("RESEMBLIO_RUNTIME_DATA_ROOT")
            try:
                # Set runtime root to the parent so the resolver finds:
                # <tmp>/computed_styles/openai.json
                os.environ["RESEMBLIO_RUNTIME_DATA_ROOT"] = str(tmp)
                passed, reason = _brand_has_real_button_styles("openai")
            finally:
                if old_env is None:
                    os.environ.pop("RESEMBLIO_RUNTIME_DATA_ROOT", None)
                else:
                    os.environ["RESEMBLIO_RUNTIME_DATA_ROOT"] = old_env
            assert passed, (
                f"Snapshot written by run_fixture_capture must pass "
                f"_brand_has_real_button_styles. Reason: {reason}"
            )

    def test_script_refuses_weak_capture_fewer_than_4_fields(self) -> None:
        """Script must refuse to write (raise ValueError) when capture yields < 4 non-default fields.

        This is the D7 gate: if the fixture render cannot extract enough real
        styles (< OPENAI_REQUIRED_NON_DEFAULT_FIELDS), the script must STOP
        rather than commit a weak snapshot that would silently mislabel
        openai as captured.
        """
        from scripts.capture_button_snapshot_from_fixture import run_fixture_capture  # type: ignore[import]

        weak_report = _make_synthetic_cta_report(non_default_count=3)  # below floor

        def fake_weak_fn(html: str | None, url: str | None, timeout_ms: int, brand_slug: str | None) -> dict[str, Any]:
            return weak_report

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            with pytest.raises(ValueError, match="non-default"):
                run_fixture_capture("openai", out_dir=out_dir, capture_fn=fake_weak_fn)
            # Must NOT have written a file.
            assert not (out_dir / "openai.json").exists(), (
                "Script must not write a snapshot when the capture is too weak."
            )

    def test_script_refuses_unavailable_capture(self) -> None:
        """Script must refuse to write when capture_fn returns status='unavailable'."""
        from scripts.capture_button_snapshot_from_fixture import run_fixture_capture  # type: ignore[import]

        unavailable_report: dict[str, Any] = {
            "status": "unavailable",
            "signals": [],
            "error": "playwright is not installed in this runtime",
            "schema_version": 1,
        }

        def fake_unavailable_fn(html: str | None, url: str | None, timeout_ms: int, brand_slug: str | None) -> dict[str, Any]:
            return unavailable_report

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            with pytest.raises((ValueError, RuntimeError)):
                run_fixture_capture("openai", out_dir=out_dir, capture_fn=fake_unavailable_fn)


# ===========================================================================
# Phase 2 - Opt-in real-render test (RESEMBLIO_RUN_REAL_BROWSER=1)
# ===========================================================================


@pytest.mark.skipif(
    not os.environ.get("RESEMBLIO_RUN_REAL_BROWSER"),
    reason=(
        "opt-in: set RESEMBLIO_RUN_REAL_BROWSER=1 to run the full "
        "Playwright fixture-capture write proof"
    ),
)
def test_fixture_capture_script_real_render_writes_valid_snapshot() -> None:
    """Full write-path proof: real chromium + openai fixture -> valid snapshot.

    Uses the real ``capture_computed_styles`` (no fake_fn). Writes to a
    tmp out_dir. Asserts the snapshot passes ``_brand_has_real_button_styles``.

    Skips cleanly when Playwright is not installed or chromium is absent.
    This mirrors test_openai_selector_captures_cta_via_set_content but
    exercises the full run_fixture_capture write path.
    """
    try:
        from extractor.computed_styles import capture_computed_styles  # noqa: F401
    except ImportError:
        pytest.skip("extractor.computed_styles not importable")

    from scripts.capture_button_snapshot_from_fixture import run_fixture_capture  # type: ignore[import]
    from extractor.computed_styles import capture_computed_styles  # type: ignore[import]

    def real_capture_fn(html: str | None, url: str | None, timeout_ms: int, brand_slug: str | None) -> dict[str, Any]:
        return capture_computed_styles(html=html, url=url, timeout_ms=timeout_ms, brand_slug=brand_slug)

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        try:
            run_fixture_capture("openai", out_dir=out_dir, capture_fn=real_capture_fn)
        except ValueError as exc:
            # D7 gate triggered - the real render yielded < 4 fields.
            # Fail loudly so this surfaces immediately rather than being silently skipped.
            pytest.fail(
                f"D7 gate triggered during real fixture render: {exc}. "
                "The openai fixture may have degraded or the selector override needs review."
            )

        out_file = out_dir / "openai.json"
        assert out_file.exists(), "run_fixture_capture must write openai.json"

        old_env = os.environ.get("RESEMBLIO_RUNTIME_DATA_ROOT")
        try:
            os.environ["RESEMBLIO_RUNTIME_DATA_ROOT"] = str(out_dir)
            passed, reason = _brand_has_real_button_styles("openai")
        finally:
            if old_env is None:
                os.environ.pop("RESEMBLIO_RUNTIME_DATA_ROOT", None)
            else:
                os.environ["RESEMBLIO_RUNTIME_DATA_ROOT"] = old_env

        assert passed, (
            f"Real fixture render must produce a snapshot that passes "
            f"_brand_has_real_button_styles. Reason: {reason}"
        )
