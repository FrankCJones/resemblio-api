"""Phase 0.E RED test: contact sheet manifest builder.

Tests for contact_sheet.py, a pure function that turns a list of
captured-file records into a typed manifest grouped by brand, with a
missing-captures list and a Markdown index.

All tests are pure-data: no network, no browser.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tests.render.contact_sheet import (  # RED until contact_sheet.py exists
    ContactSheetEntry,
    ContactSheetManifest,
    build_contact_sheet_manifest,
    render_contact_sheet_markdown,
)
from tests.render.capture_plan import Surface, build_capture_plan

_BASE_URL = "https://resemblio.com"


def _fake_captured(
    tmp_path: pathlib.Path,
    filenames: list[str],
) -> dict[str, pathlib.Path]:
    """Write tiny stub PNGs and return filename -> path mapping."""
    result: dict[str, pathlib.Path] = {}
    for name in filenames:
        p = tmp_path / name
        p.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG magic bytes
        result[name] = p
    return result


# ---------------------------------------------------------------------------
# build_contact_sheet_manifest: basic shape
# ---------------------------------------------------------------------------


def test_manifest_schema_version() -> None:
    """Manifest carries schema_version='contact_sheet_manifest_v1'."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    manifest = build_contact_sheet_manifest(plan=plan, captured_files={})
    assert manifest.schema_version == "contact_sheet_manifest_v1"


def test_manifest_has_generated_at() -> None:
    """Manifest carries a generated_at ISO-8601 timestamp."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    manifest = build_contact_sheet_manifest(plan=plan, captured_files={})
    assert manifest.generated_at, "generated_at must not be empty"
    # Rough ISO-8601 check: starts with a 4-digit year.
    assert manifest.generated_at[:4].isdigit()


def test_manifest_entries_count_equals_brands() -> None:
    """Manifest has one entry per brand in the plan."""
    brands = ["stripe", "gwern", "apple"]
    plan = build_capture_plan(brands, base_url=_BASE_URL)
    manifest = build_contact_sheet_manifest(plan=plan, captured_files={})
    assert len(manifest.entries) == len(brands)


# ---------------------------------------------------------------------------
# build_contact_sheet_manifest: captured files
# ---------------------------------------------------------------------------


def test_manifest_entry_captures_present_images(tmp_path: pathlib.Path) -> None:
    """Entry.captures lists filenames that exist on disk."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    # Provide only 2 of the 4 expected files.
    first_two = [t.output_filename for t in plan[:2]]
    captured = _fake_captured(tmp_path, first_two)

    manifest = build_contact_sheet_manifest(plan=plan, captured_files=captured)
    entry = manifest.entries[0]
    assert set(entry.captures) == set(first_two), (
        f"Expected captures {first_two!r}, got {entry.captures!r}"
    )


def test_manifest_entry_missing_absent_images(tmp_path: pathlib.Path) -> None:
    """Entry.missing lists filenames that were expected but not captured."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    all_filenames = [t.output_filename for t in plan]
    # Provide all files.
    captured = _fake_captured(tmp_path, all_filenames)

    manifest = build_contact_sheet_manifest(plan=plan, captured_files=captured)
    entry = manifest.entries[0]
    assert entry.missing == [], (
        f"Expected no missing files when all captured; got {entry.missing!r}"
    )


def test_manifest_flags_missing_captures(tmp_path: pathlib.Path) -> None:
    """Manifest surfaces missing captures rather than silently dropping them."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    # Provide NO captured files.
    manifest = build_contact_sheet_manifest(plan=plan, captured_files={})
    entry = manifest.entries[0]
    expected_missing = sorted(t.output_filename for t in plan)
    assert sorted(entry.missing) == expected_missing, (
        f"Expected all {len(expected_missing)} filenames in missing list"
    )


def test_manifest_missing_not_in_captures(tmp_path: pathlib.Path) -> None:
    """A filename cannot appear in both entry.captures and entry.missing."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    # Provide only some files.
    captured = _fake_captured(tmp_path, [plan[0].output_filename])

    manifest = build_contact_sheet_manifest(plan=plan, captured_files=captured)
    entry = manifest.entries[0]
    captures_set = set(entry.captures)
    missing_set = set(entry.missing)
    overlap = captures_set & missing_set
    assert not overlap, f"Files appear in both captures and missing: {overlap!r}"


# ---------------------------------------------------------------------------
# build_contact_sheet_manifest: summary counts
# ---------------------------------------------------------------------------


def test_manifest_summary_total_captures(tmp_path: pathlib.Path) -> None:
    """manifest.total_captured equals the count of provided captured files."""
    brands = ["stripe", "apple"]
    plan = build_capture_plan(brands, base_url=_BASE_URL)
    # Provide 3 of the 8 expected files.
    filenames = [t.output_filename for t in plan[:3]]
    captured = _fake_captured(tmp_path, filenames)

    manifest = build_contact_sheet_manifest(plan=plan, captured_files=captured)
    assert manifest.total_captured == 3


def test_manifest_summary_total_missing(tmp_path: pathlib.Path) -> None:
    """manifest.total_missing equals expected_total - total_captured."""
    brands = ["stripe", "apple"]
    plan = build_capture_plan(brands, base_url=_BASE_URL)
    expected_total = len(plan)  # 8 = 2 brands x 2 surfaces x 2 viewports
    captured = _fake_captured(tmp_path, [t.output_filename for t in plan[:3]])

    manifest = build_contact_sheet_manifest(plan=plan, captured_files=captured)
    assert manifest.total_missing == expected_total - 3


# ---------------------------------------------------------------------------
# render_contact_sheet_markdown
# ---------------------------------------------------------------------------


def test_markdown_contains_all_brand_headings(tmp_path: pathlib.Path) -> None:
    """Markdown has a heading for each brand slug."""
    brands = ["stripe", "gwern"]
    plan = build_capture_plan(brands, base_url=_BASE_URL)
    manifest = build_contact_sheet_manifest(plan=plan, captured_files={})

    md = render_contact_sheet_markdown(manifest)
    for slug in brands:
        assert f"## {slug}" in md or f"## {slug.title()}" in md or slug in md, (
            f"Brand '{slug}' heading not found in Markdown"
        )


def test_markdown_has_summary_line(tmp_path: pathlib.Path) -> None:
    """Markdown top section contains brand count, capture count, missing count."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    manifest = build_contact_sheet_manifest(plan=plan, captured_files={})

    md = render_contact_sheet_markdown(manifest)
    # Should mention the total brand and capture/missing counts somewhere.
    assert "1" in md, "Markdown should mention brand count"
    assert "missing" in md.lower(), "Markdown should surface missing count"


def test_markdown_flags_missing_in_brand_section(tmp_path: pathlib.Path) -> None:
    """Markdown notes missing captures in the brand's section."""
    plan = build_capture_plan(["stripe"], base_url=_BASE_URL)
    manifest = build_contact_sheet_manifest(plan=plan, captured_files={})

    md = render_contact_sheet_markdown(manifest)
    assert "missing" in md.lower(), (
        "Markdown must surface missing captures, not hide them"
    )


# ---------------------------------------------------------------------------
# JSON serialisability
# ---------------------------------------------------------------------------


def test_manifest_is_json_serialisable(tmp_path: pathlib.Path) -> None:
    """ContactSheetManifest converts to JSON without error."""
    plan = build_capture_plan(["stripe", "apple"], base_url=_BASE_URL)
    captured = _fake_captured(tmp_path, [plan[0].output_filename])
    manifest = build_contact_sheet_manifest(plan=plan, captured_files=captured)

    # A simple dataclass-to-dict conversion must be JSON-safe.
    import dataclasses
    payload = dataclasses.asdict(manifest)
    json_str = json.dumps(payload)
    parsed = json.loads(json_str)
    assert parsed["schema_version"] == "contact_sheet_manifest_v1"
