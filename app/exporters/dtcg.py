"""Canonical DTCG JSON byte serialization for the export endpoint.

The DTCG payload itself is already produced by ``extractor.drl_adapter``
and persisted on ``asset_versions.dtcg_json``. This module re-serializes
it deterministically (sort_keys, fixed separators, UTF-8) so two
downloads of the same extraction return byte-identical artifacts that
hash-match what we persist.

Why a separate module: the route handler must not call
``json.dumps`` with default arguments (which would inject whitespace
that drifts across Python versions). Centralizing here also lets the
ZIP bundle reuse the exact byte stream.
"""
from __future__ import annotations

import json
from typing import Any

from app.exporters.artifact import (
    EXPORTER_SCHEMA_VERSION,
    FORMAT_DTCG,
    ExporterArtifact,
    filename_for,
)

CONTENT_TYPE_DTCG: str = "application/json"


def dtcg_to_canonical_bytes(dtcg: dict[str, Any]) -> bytes:
    """Return the canonical UTF-8 byte serialization of a DTCG payload.

    Contract mirrors ``app.asset_versions.canonicalize_dtcg``: sorted
    keys, no whitespace, ``ensure_ascii=False`` so Spanish glyphs in a
    token name do not change the byte stream when default JSON behavior
    shifts. The on-the-wire artifact is human-pretty (indented) because
    end users open the file in editors; the canonical hash bytes stay
    minified.
    """
    return json.dumps(
        dtcg,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")


def dtcg_artifact(extraction_id: int, dtcg: dict[str, Any]) -> ExporterArtifact:
    """Wrap the DTCG bytes in an ExporterArtifact for the route handler."""
    return ExporterArtifact(
        bytes=dtcg_to_canonical_bytes(dtcg),
        content_type=CONTENT_TYPE_DTCG,
        filename=filename_for(extraction_id, FORMAT_DTCG),
        schema_version=EXPORTER_SCHEMA_VERSION,
    )
