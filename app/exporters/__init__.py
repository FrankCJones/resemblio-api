"""DTCG export-format converters for Stage O7.

This subsystem wraps the four URL-first export formats Frank approved on
2026-06-03 around the canonical DTCG JSON payload produced by the
extractor. Each converter is a pure-data function (no network, no I/O
beyond ZIP buffering) so the same code runs in-route for the authed
endpoint, in-route for the anonymous claim-token endpoint, and in any
future MCP / CLI client.

File map and data flow
----------------------

::

    DTCG payload (dict)
        |
        +--> dtcg.py        -> canonical DTCG JSON bytes
        +--> css.py         -> CSS :root custom-properties block (str)
        +--> tailwind.py    -> Tailwind v4 `@theme {}` block (str)
        +--> zip_bundle.py  -> ZIP combining all of the above + README

Contracts
---------

* Every converter accepts a DTCG dict shaped per
  ``extractor.drl_adapter.to_dtcg_json`` (top-level groups: ``color``,
  ``fontFamily``, ``dimension``, ``shadow``, etc.; each leaf carries
  ``$value`` and usually ``$type``).
* Every converter emits an ``ExporterArtifact`` carrying ``bytes``,
  ``content_type``, and the ``filename`` to attach to an HTTP response.
* ``schema_version`` is fixed at ``1`` for every artifact. Bumping the
  contract requires changing this number AND a coordinated client
  rollout; we do not silently change emitted text.

Backlog (NOT in v1; deferred to v1.1 per the URL-first respec)
--------------------------------------------------------------

* Style Dictionary native format
* Figma Tokens plugin format

Both are tracked in ``projects/Resemblio/CLAUDE.md`` (the export-format
matrix) and the Stage O7 mission brief.
"""
from __future__ import annotations

from app.exporters.artifact import EXPORTER_SCHEMA_VERSION, ExporterArtifact
from app.exporters.css import dtcg_to_css
from app.exporters.dtcg import dtcg_to_canonical_bytes
from app.exporters.tailwind import dtcg_to_tailwind_theme
from app.exporters.zip_bundle import dtcg_to_zip_bundle

__all__ = [
    "EXPORTER_SCHEMA_VERSION",
    "ExporterArtifact",
    "dtcg_to_canonical_bytes",
    "dtcg_to_css",
    "dtcg_to_tailwind_theme",
    "dtcg_to_zip_bundle",
]
