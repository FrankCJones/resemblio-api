# DTCG compiler subsystem

`dtcg_compiler.py` converts the shared Design Reference Library CSS token contract into a strict, deterministic DTCG 2025.10 document. It owns parsing, normalization, validation, canonical serialization, and content identity.

`scripts/compile_library_tokens.py` is the file boundary. It reads a source stylesheet and writes identical versioned projections to one or more repositories. Source files remain read-only.

`tests/test_dtcg_compiler.py` covers deterministic bytes, source identity, leading, tracking, aliases, cycles, units, composites, schema identity, and unsafe fonts.

The generated DTCG document is the customer product. Projection metadata is internal build evidence and never wraps the downloaded document.
