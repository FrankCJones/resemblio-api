"""ZIP bundle export combining every format into one downloadable archive.

The bundle is what a designer downloads when they want everything in one
shot: the DTCG manifest (source of truth), the CSS custom-properties
file, the Tailwind v4 ``@theme`` block, and a README explaining what
each file is. The original extraction screenshot is included when the
caller provides one (anonymous-flow callers do not have one yet; that
is acceptable - the bundle is still valid).

ZIP layout::

    resemblio-<id>-bundle.zip
    +-- README.md
    +-- tokens.json             (canonical DTCG payload, pretty-printed)
    +-- tokens.css              (CSS :root custom properties)
    +-- tailwind.css            (Tailwind v4 @theme block)
    +-- screenshot.png          (optional; only when screenshot bytes provided)

Determinism note: ZipFile by default records the current timestamp on
every entry, which would make two consecutive bundle requests for the
same extraction return different bytes. We pin a fixed
``date_time`` on every ZipInfo so a content-hash check across requests
is stable.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from app.exporters.artifact import (
    EXPORTER_SCHEMA_VERSION,
    FORMAT_ZIP,
    ExporterArtifact,
    filename_for,
)
from app.exporters.css import dtcg_to_css
from app.exporters.dtcg import dtcg_to_canonical_bytes
from app.exporters.tailwind import dtcg_to_tailwind_theme

CONTENT_TYPE_ZIP: str = "application/zip"

# Fixed ZIP entry timestamp for determinism. 2026-01-01 chosen as a
# stable sentinel; the real extraction timestamp lives in the DTCG
# payload itself (the extractor records `extracted_at`).
_FIXED_ZIP_TIMESTAMP: tuple[int, int, int, int, int, int] = (2026, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class ZipBundleInputs:
    """Inputs the ZIP bundler needs beyond the DTCG payload itself.

    Keeping this as a dataclass (vs many positional args) makes it
    safe to add fields later (e.g. a brand-color preview PNG) without
    breaking callers.
    """

    extraction_id: int
    source_url: str
    screenshot_bytes: bytes | None = None


def _readme_text(inputs: ZipBundleInputs) -> str:
    """Compose the bundle README explaining each file in the archive.

    Kept pure (no I/O) so the same text is testable as a string.
    """
    has_screenshot = inputs.screenshot_bytes is not None
    lines = [
        "# Resemblio export bundle",
        "",
        f"Source URL: {inputs.source_url}",
        f"Extraction ID: {inputs.extraction_id}",
        f"Bundle schema_version: {EXPORTER_SCHEMA_VERSION}",
        "",
        "## Files",
        "",
        "- tokens.json - DTCG-conformant design tokens. The canonical source",
        "  of truth; every other file in this bundle is derived from it.",
        "- tokens.css - CSS custom properties under :root. Drop this into any",
        "  stylesheet via @import or <link> for an immediate build-free wiring.",
        "- tailwind.css - Tailwind v4 @theme {} block. Place inside your main",
        "  Tailwind CSS file so the utility classes pick up the brand tokens.",
    ]
    if has_screenshot:
        lines.append(
            "- screenshot.png - Reference screenshot of the source page at the"
        )
        lines.append("  time of extraction.")
    lines.extend(
        [
            "",
            "## Not in this bundle (yet)",
            "",
            "- Style Dictionary native format (v1.1)",
            "- Figma Tokens plugin format (v1.1)",
            "",
            "Track v1.1 progress at https://resemblio.com/changelog",
            "",
        ]
    )
    return "\n".join(lines)


def _write_entry(zf: ZipFile, name: str, payload: bytes) -> None:
    """Append one ZIP entry with a fixed timestamp for byte-determinism."""
    info = ZipInfo(filename=name, date_time=_FIXED_ZIP_TIMESTAMP)
    info.compress_type = ZIP_DEFLATED
    zf.writestr(info, payload)


def dtcg_to_zip_bundle(dtcg: dict[str, Any], inputs: ZipBundleInputs) -> bytes:
    """Build the multi-format ZIP archive bytes for one extraction.

    Args:
        dtcg: The canonical DTCG payload (top-level groups: color,
            fontFamily, dimension, shadow, etc.).
        inputs: Extraction-scoped context (id, URL, optional screenshot).

    Returns:
        ZIP bytes ready to attach to an HTTP response. Same input -> same
        output bytes (deterministic timestamp).
    """
    buffer = BytesIO()
    with ZipFile(buffer, "w") as zf:
        _write_entry(zf, "README.md", _readme_text(inputs).encode("utf-8"))
        _write_entry(zf, "tokens.json", dtcg_to_canonical_bytes(dtcg))
        _write_entry(zf, "tokens.css", dtcg_to_css(dtcg).encode("utf-8"))
        _write_entry(
            zf, "tailwind.css", dtcg_to_tailwind_theme(dtcg).encode("utf-8")
        )
        if inputs.screenshot_bytes is not None:
            _write_entry(zf, "screenshot.png", inputs.screenshot_bytes)
    return buffer.getvalue()


def zip_artifact(
    dtcg: dict[str, Any], inputs: ZipBundleInputs
) -> ExporterArtifact:
    """Wrap the ZIP bytes in an ExporterArtifact for the route handler."""
    return ExporterArtifact(
        bytes=dtcg_to_zip_bundle(dtcg, inputs),
        content_type=CONTENT_TYPE_ZIP,
        filename=filename_for(inputs.extraction_id, FORMAT_ZIP),
        schema_version=EXPORTER_SCHEMA_VERSION,
    )
