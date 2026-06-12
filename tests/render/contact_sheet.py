"""Contact sheet manifest builder for the visual harness.

Turns a capture plan + a dict of captured files into a typed manifest
grouping the four per-brand images (landing/specimen x desktop/mobile),
flagging missing captures, and generating a Markdown index for human review.

The Markdown index is what Frank and Opus sign off on before Phase 1 begins.
The JSON manifest is what the Phase 5 "after" sweep diffs against.

Decision reference: D16 in
projects/OptSus Team/missions/resemblio-library-public-view-readiness-tdd-plan-v5.md

Schema: contact_sheet_manifest_v1
"""
from __future__ import annotations

import dataclasses
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from tests.render.capture_plan import CaptureTarget

SCHEMA_VERSION = "contact_sheet_manifest_v1"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class ContactSheetEntry:
    """Per-brand entry in the contact sheet.

    Attributes:
        brand_slug:   Brand identifier (e.g. "stripe").
        captures:     Filenames of images that were successfully captured.
                      Sorted for deterministic diffs.
        missing:      Filenames of images that were expected but not captured.
                      Sorted for deterministic diffs.
        capture_paths: Path objects for captured files (not serialised to JSON
                      via asdict without path-to-str conversion, handled by
                      render_contact_sheet_markdown).
    """

    brand_slug: str
    captures: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # Stored as strings so dataclasses.asdict produces JSON-serialisable output.
    capture_paths: list[str] = field(default_factory=list, repr=False)

    def is_complete(self) -> bool:
        """Return True when no captures are missing for this brand."""
        return len(self.missing) == 0

    def __post_init__(self) -> None:
        self.captures = sorted(self.captures)
        self.missing = sorted(self.missing)


@dataclass
class ContactSheetManifest:
    """Aggregate contact sheet manifest.

    Attributes:
        schema_version:  Always "contact_sheet_manifest_v1".
        generated_at:    ISO-8601 UTC timestamp of when the manifest was built.
        brand_count:     Number of distinct brands in the plan.
        total_captured:  Total number of captured image files across all brands.
        total_missing:   Total number of expected but absent image files.
        entries:         Per-brand entries, one per distinct brand_slug in the plan.
    """

    schema_version: str
    generated_at: str
    brand_count: int
    total_captured: int
    total_missing: int
    entries: list[ContactSheetEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_contact_sheet_manifest(
    *,
    plan: list[CaptureTarget],
    captured_files: dict[str, pathlib.Path],
) -> ContactSheetManifest:
    """Build a contact sheet manifest from a capture plan and available files.

    Args:
        plan:           Full capture plan (output of build_capture_plan).
                        Defines the complete set of expected filenames.
        captured_files: Mapping of output_filename -> path for images that
                        were actually captured. Files not in this dict are
                        listed as missing in the manifest. Empty dict is valid
                        (all targets will be missing).

    Returns:
        ContactSheetManifest with one entry per brand, each listing captures
        and missing files. The manifest never silently drops missing files -
        they always surface in entry.missing and manifest.total_missing.
    """
    # Group plan targets by brand_slug.
    by_brand: dict[str, list[CaptureTarget]] = {}
    for target in plan:
        by_brand.setdefault(target.brand_slug, []).append(target)

    entries: list[ContactSheetEntry] = []
    total_captured = 0
    total_missing = 0

    for brand_slug, targets in by_brand.items():
        captured: list[str] = []
        missing: list[str] = []
        capture_paths: list[pathlib.Path] = []

        for target in targets:
            if target.output_filename in captured_files:
                captured.append(target.output_filename)
                capture_paths.append(str(captured_files[target.output_filename]))
            else:
                missing.append(target.output_filename)

        total_captured += len(captured)
        total_missing += len(missing)
        entries.append(
            ContactSheetEntry(
                brand_slug=brand_slug,
                captures=captured,
                missing=missing,
                capture_paths=capture_paths,
            )
        )

    # Sort entries by brand_slug for deterministic output.
    entries.sort(key=lambda e: e.brand_slug)

    return ContactSheetManifest(
        schema_version=SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(),
        brand_count=len(by_brand),
        total_captured=total_captured,
        total_missing=total_missing,
        entries=entries,
    )


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def render_contact_sheet_markdown(manifest: ContactSheetManifest) -> str:
    """Render the contact sheet manifest as a human-readable Markdown document.

    Produces:
      - A header with summary statistics (brands, captures, missing).
      - One H2 section per brand with the 4 image references (if captured)
        and a missing-captures notice (if any are absent).

    Args:
        manifest: Built by build_contact_sheet_manifest.

    Returns:
        A Markdown string. Does not write to disk; the caller controls output.
    """
    lines: list[str] = []
    lines.append("# Resemblio Library Visual Contact Sheet")
    lines.append("")
    lines.append(f"- Schema: `{manifest.schema_version}`")
    lines.append(f"- Generated (UTC): {manifest.generated_at}")
    lines.append(f"- Brands: {manifest.brand_count}")
    lines.append(f"- Captured: {manifest.total_captured}")
    lines.append(f"- Missing: {manifest.total_missing}")
    lines.append("")

    if manifest.total_missing > 0:
        lines.append(
            f"> **{manifest.total_missing} capture(s) missing** - "
            "see per-brand sections for details."
        )
        lines.append("")

    for entry in manifest.entries:
        lines.append(f"## {entry.brand_slug}")
        lines.append("")

        if entry.captures:
            for filename in entry.captures:
                lines.append(f"![]({filename})")
            lines.append("")

        if entry.missing:
            lines.append(
                f"**Missing ({len(entry.missing)}):** "
                + ", ".join(f"`{f}`" for f in entry.missing)
            )
            lines.append("")

    return "\n".join(lines) + "\n"
