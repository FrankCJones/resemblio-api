"""Shared artifact dataclass + format registry for the exporters.

Keeps the HTTP-response shape (Content-Type + filename) co-located with
the bytes a converter produces so the route handler is a one-liner per
format. Centralized so all four converters report a consistent
``schema_version``.
"""
from __future__ import annotations

from dataclasses import dataclass

EXPORTER_SCHEMA_VERSION: int = 1
"""Wire-contract version for every emitted artifact.

Bump only with a coordinated client rollout; the byte-for-byte shape of
every exporter output is part of the contract.
"""

FORMAT_DTCG: str = "dtcg"
FORMAT_CSS: str = "css"
FORMAT_TAILWIND: str = "tailwind"
FORMAT_ZIP: str = "zip"

SUPPORTED_FORMATS: frozenset[str] = frozenset(
    {FORMAT_DTCG, FORMAT_CSS, FORMAT_TAILWIND, FORMAT_ZIP}
)
"""Set of format slugs the export endpoints accept.

Style Dictionary and Figma Tokens are deferred to v1.1 per the Stage O7
brief; they are intentionally absent.
"""


@dataclass(frozen=True)
class ExporterArtifact:
    """One export-format output ready to attach to an HTTP response.

    Intent: every converter returns this so the route handler does not
    need format-specific branching to set Content-Type / filename
    headers. ``filename`` is bare (no path) and must include the
    extension a browser will save it under.
    """

    bytes: bytes
    content_type: str
    filename: str
    schema_version: int = EXPORTER_SCHEMA_VERSION


def filename_for(extraction_id: int, fmt: str) -> str:
    """Return the canonical filename for an extraction's export artifact.

    Centralized so the ZIP bundle's internal names match the standalone
    download names (a user who downloaded ``resemblio-42-tokens.json``
    and later opens the ZIP sees the same file inside).

    Args:
        extraction_id: The extraction primary key.
        fmt: One of the ``FORMAT_*`` constants.

    Raises:
        ValueError: If ``fmt`` is not a supported format slug.
    """
    if fmt == FORMAT_DTCG:
        return f"resemblio-{extraction_id}-tokens.json"
    if fmt == FORMAT_CSS:
        return f"resemblio-{extraction_id}-tokens.css"
    if fmt == FORMAT_TAILWIND:
        return f"resemblio-{extraction_id}-tailwind.css"
    if fmt == FORMAT_ZIP:
        return f"resemblio-{extraction_id}-bundle.zip"
    raise ValueError(f"unsupported export format: {fmt!r}")
