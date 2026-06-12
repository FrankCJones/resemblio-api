"""Tests for the reconcile_library_reports CLI I/O boundary.

Purpose
-------
Phase B (RED-then-GREEN): proves the CLI reads two saved assertion-report JSON
files, calls the reconciliation engine, writes schema-versioned JSON + Markdown
output, and exits with the correct code.

All tests use ``tmp_path`` for file I/O: no network, no DB, no SSH.  The engine
itself is tested exhaustively in ``test_library_reseed_verification.py``; these
tests cover only the CLI shell (argument parsing, file I/O, exit codes).

Run command (from ``code/api/``):
    python -m pytest tests/test_reconcile_library_reports_cli.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# RED on import until scripts/reconcile_library_reports.py is created.
from scripts.reconcile_library_reports import main  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers - build minimal synthetic report fixtures on disk
# ---------------------------------------------------------------------------

_SCHEMA_V1 = "library_assertion_report_v1"


def _write_report(path: Path, brands: list[tuple[str, str]]) -> Path:
    """Write a minimal LibraryAssertionReport JSON file at ``path``.

    Parameters
    ----------
    path:
        Full file path to write (including filename).
    brands:
        Each entry is ``(brand_slug, verdict)``.  Verdict must be one of
        ``panel_faithful``, ``panel_cleanly_absent``, ``page_broken``.

    Returns
    -------
    Path
        The written file path (same as ``path``).
    """
    assertions = [
        {
            "brand_slug": slug,
            "verdict": verdict,
            "present_curated_fields": [],
            "missing_curated_fields": [],
            "v3_chip_gating": "intact",
            "notes": "",
        }
        for slug, verdict in brands
    ]
    report = {
        "schema_version": _SCHEMA_V1,
        "generated_at": "2026-06-11T00:00:00+00:00",
        "source": "fixture",
        "brand_count": len(brands),
        "verdict_counts": {"panel_faithful": 0, "panel_cleanly_absent": 0, "page_broken": 0},
        "assertions": assertions,
        "all_pass": all(v != "page_broken" for _, v in brands),
    }
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests - exit codes + output files
# ---------------------------------------------------------------------------


class TestReconciledPair:
    """Two matching reports -> exit 0 + reconciliation.json with reconciled=true."""

    def test_exit_0(self, tmp_path: Path):
        predicted = _write_report(tmp_path / "predicted.json", [("stripe", "panel_faithful")])
        actual = _write_report(tmp_path / "actual.json", [("stripe", "panel_faithful")])
        out_dir = tmp_path / "out"
        rc = main(["--predicted", str(predicted), "--actual", str(actual), "--out-dir", str(out_dir)])
        assert rc == 0

    def test_reconciliation_json_written(self, tmp_path: Path):
        predicted = _write_report(tmp_path / "predicted.json", [("stripe", "panel_faithful")])
        actual = _write_report(tmp_path / "actual.json", [("stripe", "panel_faithful")])
        out_dir = tmp_path / "out"
        main(["--predicted", str(predicted), "--actual", str(actual), "--out-dir", str(out_dir)])
        result_json = out_dir / "reconciliation.json"
        assert result_json.exists()
        data = json.loads(result_json.read_text(encoding="utf-8"))
        assert data["reconciled"] is True

    def test_schema_version_in_output(self, tmp_path: Path):
        predicted = _write_report(tmp_path / "predicted.json", [("stripe", "panel_faithful")])
        actual = _write_report(tmp_path / "actual.json", [("stripe", "panel_faithful")])
        out_dir = tmp_path / "out"
        main(["--predicted", str(predicted), "--actual", str(actual), "--out-dir", str(out_dir)])
        data = json.loads((out_dir / "reconciliation.json").read_text(encoding="utf-8"))
        assert data["schema_version"] == "library_reconciliation_v1"


class TestDivergentPair:
    """Predicted has a brand that actual dropped -> exit 1 + divergence in JSON."""

    def test_exit_1(self, tmp_path: Path):
        predicted = _write_report(
            tmp_path / "predicted.json",
            [("stripe", "panel_faithful"), ("figma", "panel_faithful")],
        )
        actual = _write_report(tmp_path / "actual.json", [("stripe", "panel_faithful")])
        out_dir = tmp_path / "out"
        rc = main(["--predicted", str(predicted), "--actual", str(actual), "--out-dir", str(out_dir)])
        assert rc == 1

    def test_missing_brand_in_output(self, tmp_path: Path):
        predicted = _write_report(
            tmp_path / "predicted.json",
            [("stripe", "panel_faithful"), ("figma", "panel_faithful")],
        )
        actual = _write_report(tmp_path / "actual.json", [("stripe", "panel_faithful")])
        out_dir = tmp_path / "out"
        main(["--predicted", str(predicted), "--actual", str(actual), "--out-dir", str(out_dir)])
        data = json.loads((out_dir / "reconciliation.json").read_text(encoding="utf-8"))
        assert data["reconciled"] is False
        assert "figma" in data["missing_in_actual"]


class TestMissingInputFile:
    """Pointing --actual at a nonexistent path -> exit 2."""

    def test_exit_2_on_missing_file(self, tmp_path: Path):
        predicted = _write_report(tmp_path / "predicted.json", [("stripe", "panel_faithful")])
        nonexistent = tmp_path / "does_not_exist.json"
        out_dir = tmp_path / "out"
        rc = main(["--predicted", str(predicted), "--actual", str(nonexistent), "--out-dir", str(out_dir)])
        assert rc == 2


class TestMalformedJson:
    """A non-JSON file as input -> exit 2, no crash."""

    def test_exit_2_on_bad_json(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text("this is not json", encoding="utf-8")
        predicted = _write_report(tmp_path / "predicted.json", [("stripe", "panel_faithful")])
        out_dir = tmp_path / "out"
        rc = main(["--predicted", str(predicted), "--actual", str(bad), "--out-dir", str(out_dir)])
        assert rc == 2


class TestMarkdownOutput:
    """reconciliation.md is written and contains key identifiers."""

    def test_markdown_file_written(self, tmp_path: Path):
        predicted = _write_report(tmp_path / "predicted.json", [("stripe", "panel_faithful")])
        actual = _write_report(tmp_path / "actual.json", [("stripe", "panel_faithful")])
        out_dir = tmp_path / "out"
        main(["--predicted", str(predicted), "--actual", str(actual), "--out-dir", str(out_dir)])
        assert (out_dir / "reconciliation.md").exists()

    def test_markdown_contains_verdict(self, tmp_path: Path):
        predicted = _write_report(tmp_path / "predicted.json", [("stripe", "panel_faithful")])
        actual = _write_report(tmp_path / "actual.json", [("stripe", "panel_faithful")])
        out_dir = tmp_path / "out"
        main(["--predicted", str(predicted), "--actual", str(actual), "--out-dir", str(out_dir)])
        md = (out_dir / "reconciliation.md").read_text(encoding="utf-8")
        assert "reconciled" in md.lower()

    def test_markdown_contains_brand_on_divergence(self, tmp_path: Path):
        predicted = _write_report(
            tmp_path / "predicted.json",
            [("stripe", "panel_faithful"), ("figma", "panel_faithful")],
        )
        actual = _write_report(tmp_path / "actual.json", [("stripe", "panel_faithful")])
        out_dir = tmp_path / "out"
        main(["--predicted", str(predicted), "--actual", str(actual), "--out-dir", str(out_dir)])
        md = (out_dir / "reconciliation.md").read_text(encoding="utf-8")
        assert "figma" in md


class TestMalformedShapeInput:
    """Wrong-shape --actual file (valid JSON, not a LibraryAssertionReport) -> exit 2.

    Engine-level guard emits a note starting with 'malformed_report:'; the CLI maps
    that to exit 2 (IO-class problem) rather than exit 1 (genuine divergence).
    This prevents a confusing traceback when an operator points --actual at a
    reconciliation.json or ceremony.json by mistake during the ceremony.

    RED until the CLI adds the malformed_report note -> exit 2 mapping.
    """

    def test_exit_2_on_missing_assertions_key(self, tmp_path: Path):
        """Valid JSON with no assertions key (e.g. ceremony.json) -> exit 2, not 1."""
        predicted = _write_report(tmp_path / "predicted.json", [("stripe", "panel_faithful")])
        # A ceremony-gate JSON has no assertions key - wrong shape
        wrong_shape = tmp_path / "ceremony.json"
        wrong_shape.write_text(
            json.dumps({
                "schema_version": "library_assertion_report_v1",
                "brand_count": 0,
            }),
            encoding="utf-8",
        )
        out_dir = tmp_path / "out"
        rc = main(["--predicted", str(predicted), "--actual", str(wrong_shape), "--out-dir", str(out_dir)])
        assert rc == 2

    def test_exit_2_on_non_list_assertions(self, tmp_path: Path):
        """Valid JSON with assertions as a dict (wrong shape) -> exit 2."""
        predicted = _write_report(tmp_path / "predicted.json", [("stripe", "panel_faithful")])
        wrong_shape = tmp_path / "wrong.json"
        wrong_shape.write_text(
            json.dumps({
                "schema_version": "library_assertion_report_v1",
                "brand_count": 0,
                "assertions": {"brand_slug": "stripe", "verdict": "panel_faithful"},
            }),
            encoding="utf-8",
        )
        out_dir = tmp_path / "out"
        rc = main(["--predicted", str(predicted), "--actual", str(wrong_shape), "--out-dir", str(out_dir)])
        assert rc == 2

    def test_no_crash_on_wrong_shape(self, tmp_path: Path):
        """main() returns an int (2), does not raise, on a wrong-shape file."""
        predicted = _write_report(tmp_path / "predicted.json", [("stripe", "panel_faithful")])
        wrong_shape = tmp_path / "wrong.json"
        wrong_shape.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        out_dir = tmp_path / "out"
        # Should not raise any exception
        result = main(["--predicted", str(predicted), "--actual", str(wrong_shape), "--out-dir", str(out_dir)])
        assert isinstance(result, int)

    def test_well_formed_pair_still_exits_0(self, tmp_path: Path):
        """Regression guard: the new CLI mapping must not affect clean reconciliation."""
        predicted = _write_report(tmp_path / "predicted.json", [("stripe", "panel_faithful")])
        actual = _write_report(tmp_path / "actual.json", [("stripe", "panel_faithful")])
        out_dir = tmp_path / "out"
        rc = main(["--predicted", str(predicted), "--actual", str(actual), "--out-dir", str(out_dir)])
        assert rc == 0
