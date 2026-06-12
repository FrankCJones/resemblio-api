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
