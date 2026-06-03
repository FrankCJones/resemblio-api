<!--
schema_version: subsystem_v1
purpose: Export-format converters for Stage O7 (URL-first respec).
last_verified: 2026-06-03
-->

# `app/exporters/` - DTCG export-format converters

Wraps the four URL-first export formats Frank approved on 2026-06-03
around the canonical DTCG payload produced by the extractor. Each
converter is pure-data: no network, no I/O beyond an in-memory ZIP
buffer. Same code runs from the authed endpoint, the anonymous
claim-token endpoint, and any future MCP / CLI client.

## File map

| File | Purpose |
|---|---|
| `__init__.py` | Subsystem re-exports |
| `artifact.py` | Shared `ExporterArtifact` dataclass, format slugs, `EXPORTER_SCHEMA_VERSION = 1` |
| `dtcg.py` | DTCG -> canonical UTF-8 JSON bytes (pretty-printed, sort_keys) |
| `css.py` | DTCG -> CSS `:root { --token: value; }` block |
| `tailwind.py` | DTCG -> Tailwind v4 `@theme {}` block |
| `zip_bundle.py` | DTCG -> ZIP with all three formats + README + optional screenshot |

## Data flow

```
asset_versions.dtcg_json (canonical) ----+
                                         |
            +----------------------------+
            v
  app/exporters/{format}.py  ->  ExporterArtifact { bytes, content_type, filename }
            v
  app/routes/extractions.py (authed export endpoint)
  app/routes/extractions_anonymous.py (claim-token export endpoint)
```

## Contracts

- Every converter accepts a DTCG dict shaped per
  `extractor/drl_adapter.py::to_dtcg_json` (top-level groups `color`,
  `fontFamily`, `dimension`, `shadow`, ...; each leaf carries `$value`
  and usually `$type`).
- Every converter returns an `ExporterArtifact`. The route handler
  uses `content_type` for the response header and `filename` for the
  `Content-Disposition: attachment; filename="..."` header.
- `schema_version = 1` is fixed. Bumping requires a coordinated
  rollout; the byte-for-byte shape is part of the contract.
- ZIP bundle uses a fixed timestamp on every entry so two requests
  for the same extraction return byte-identical ZIPs.

## Deliberately omitted (v1.1 backlog)

- Style Dictionary native format
- Figma Tokens plugin format

Tracked in the Stage O7 mission brief; not in v1 per the
working-product principle (`projects/Resemblio/CLAUDE.md`).

## Adding a new format

1. New module `app/exporters/<format>.py` with a pure-data converter
   plus `<format>_artifact(extraction_id, dtcg) -> ExporterArtifact`.
2. Register the slug in `artifact.SUPPORTED_FORMATS` and add a
   filename rule in `artifact.filename_for`.
3. Add `tests/test_exporter_<format>.py` covering the primitive
   token types: color, spacing, font, shadow.
4. Wire the slug into the `/v1/extractions/{id}/export/{format}` route
   dispatch table.
5. Update this README's file map and `OPS.md`'s export-endpoint table.
