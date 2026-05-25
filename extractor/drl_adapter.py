"""DRL adapter module for Resemblio's URL extractor.

Resemblio's user-facing extraction service uses the Gen-2 pipeline from the
Design Reference Library (DRL). This module is the clean import surface that
Codex (and future builders) consume - the adapter is the boundary, not DRL's
internals. If DRL refactors its `_scripts/` layout, only this file changes.

What this module re-exports from DRL:
- TokenSet, ExtractionRecord, SectionOutline, InspiredByEntry, SkipReason
- validate_token_set, validate_extraction, validate_section_outline
- ExtractionValidationError
- REQUIRED_TOKEN_KEYS, SCHEMA_VERSION
- recon_ping module (reachability probe)
- recon module (URL classifier / sitemap reader)
- fetch_html module (urllib + Chrome UA fallback fetcher)

What this module ADDS (Resemblio-specific glue):
- to_dtcg_json(token_set)        - converts a flat TokenSet into a DTCG-conformant
                                   token tree grouped by type (color / dimension /
                                   fontFamily / number / shadow / duration /
                                   cubicBezier). Output is ready to write to
                                   Postgres or return via API.
- to_postgres_row(...)           - prepares the row payload matching the
                                   extractions table schema. Pure dict; the
                                   caller does the actual SQL.
- ResemblioExtractor (Protocol)  - the contract Codex implements: a single
                                   `extract(url) -> tuple[TokenSet | None, str | None]`
                                   method.

The token-grouping convention follows the W3C Design Tokens Community Group
spec (DTCG): every leaf carries `$value` and `$type`. Group keys (`color`,
`dimension`, etc.) are the convention used downstream by Style Dictionary,
shadcn, Tailwind, and Figma Variables consumers.

Throwaway: NO. Quality floor applies. Tests in test_drl_adapter.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Protocol, TypedDict

# DRL is a sibling project, not a pip package. Resolve its root once at module
# load and prepend to sys.path so `from _scripts.extraction import ...` works.
# Path layout: this file is at projects/Resemblio/code/extractor/drl_adapter.py
# parents[3] is projects/, then "Design Reference Library" is the sibling.
_DRL_ROOT = Path(__file__).resolve().parents[3] / "Design Reference Library"
if str(_DRL_ROOT) not in sys.path:
    sys.path.insert(0, str(_DRL_ROOT))

# Re-exports from DRL. Codex imports from drl_adapter, NOT from _scripts.*.
from _scripts.extraction import (  # noqa: E402
    ExtractionRecord,
    ExtractionValidationError,
    InspiredByEntry,
    REQUIRED_TOKEN_KEYS,
    SCHEMA_VERSION,
    SectionOutline,
    SkipReason,
    TokenSet,
    validate_extraction,
    validate_section_outline,
    validate_token_set,
)
from _scripts import fetch_html, recon, recon_ping  # noqa: E402

__all__ = [
    # DRL contract re-exports
    "ExtractionRecord",
    "ExtractionValidationError",
    "InspiredByEntry",
    "REQUIRED_TOKEN_KEYS",
    "SCHEMA_VERSION",
    "SectionOutline",
    "SkipReason",
    "TokenSet",
    "validate_extraction",
    "validate_section_outline",
    "validate_token_set",
    "fetch_html",
    "recon",
    "recon_ping",
    # Resemblio-specific glue
    "to_dtcg_json",
    "to_postgres_row",
    "ResemblioExtractor",
    "PostgresRow",
    "DTCG_TYPE_BY_PREFIX",
    "DTCG_GROUP_BY_PREFIX",
]


# ----------------------------------------------------------------------
# DTCG conversion
# ----------------------------------------------------------------------

# DTCG type per token-name prefix. The flat TokenSet uses snake_case names
# like `text_lg`, `space_4`, `font_body`; the prefix determines the DTCG
# `$type`. Anything not matched falls through to the "other" group with
# no $type (DTCG allows missing $type when the parent group sets it; we
# stay explicit per slot for downstream tooling simplicity).
DTCG_TYPE_BY_PREFIX: dict[str, str] = {
    # Color slots - every keyed token under here is a color.
    "bg": "color",
    "surface": "color",
    "text": "color",          # bare "text" (the body color) - sizes use text_* below
    "border": "color",
    "hairline": "color",
    "accent": "color",
    "success": "color",
    "warning": "color",
    "error": "color",
    "info": "color",
    "focus": "color",         # focus_ring
    # Font family slots.
    "font": "fontFamily",
    # Dimensional scales (sizes, spacing, radii).
    "space": "dimension",
    "radius": "dimension",
    # Shadow slots are composite tokens in DTCG; we emit them as $type:"shadow"
    # with a string $value (the raw box-shadow declaration). Consumers can
    # parse if they need the structured form.
    "shadow": "shadow",
    # Motion.
    "duration": "duration",
    "ease": "cubicBezier",
    # Numeric line-heights and tracking.
    "leading": "number",
    "tracking": "dimension",
}
"""Map first underscore-segment of a TokenSet key to its DTCG `$type`."""

DTCG_GROUP_BY_PREFIX: dict[str, str] = {
    "bg": "color",
    "surface": "color",
    "text": "color",          # overridden below for text_* (size) keys
    "border": "color",
    "hairline": "color",
    "accent": "color",
    "success": "color",
    "warning": "color",
    "error": "color",
    "info": "color",
    "focus": "color",
    "font": "fontFamily",
    "space": "dimension",
    "radius": "dimension",
    "shadow": "shadow",
    "duration": "duration",
    "ease": "cubicBezier",
    "leading": "number",
    "tracking": "dimension",
}
"""Map first underscore-segment to the top-level DTCG group name."""

# Tokens whose name starts with "text_" are SIZES, not the body color. Only
# the bare "text" / "text_muted" / "text_strong" keys are colors.
_TEXT_COLOR_KEYS = frozenset({"text", "text_muted", "text_strong"})


def _classify(key: str) -> tuple[str, str]:
    """Return (group, dtcg_type) for a flat TokenSet key.

    The classification rules:
    - "text_muted" / "text_strong" -> color group, color type
    - "text_<size>" (text_lg, text_base, ...) -> dimension group, dimension type
    - All other keys: use the prefix table.
    - Unknown prefixes fall into the "other" group with no $type.

    Edge case: a renamed slot DRL adds later that we don't recognise will land
    in "other". That keeps the conversion total (no KeyError) while making
    drift visible to consumers.
    """
    if key in _TEXT_COLOR_KEYS:
        return "color", "color"
    prefix = key.split("_", 1)[0]
    if prefix == "text":
        # Type-size slot (text_base, text_lg, ...).
        return "dimension", "dimension"
    if prefix == "focus":
        return "color", "color"
    group = DTCG_GROUP_BY_PREFIX.get(prefix, "other")
    dtype = DTCG_TYPE_BY_PREFIX.get(prefix, "")
    return group, dtype


def _leaf_name(key: str) -> str:
    """Convert a flat snake_case key into a DTCG-style dash-separated leaf name.

    We preserve the full semantic key rather than stripping the group prefix.
    Reason: the prefix carries meaning the group name alone does not. A
    `color` group with leaves named "accent", "accent-2", "success", "warning"
    reads cleaner than a group with both "accent" and a bare "2". For
    `dimension`, having "text-lg" vs "space-4" vs "radius-md" preserves the
    semantic axis (type-scale vs spacing vs corner-radius) that consumers
    rely on when generating Tailwind, Style Dictionary, or CSS output.
    """
    return key.replace("_", "-")


def to_dtcg_json(token_set: TokenSet) -> dict[str, Any]:
    """Convert a flat TokenSet into a DTCG-spec-conformant token tree.

    Output shape:
        {
          "color":      {"bg": {"$value": "#fff", "$type": "color"}, ...},
          "fontFamily": {"display": {"$value": "...", "$type": "fontFamily"}, ...},
          "dimension":  {"space-4": {"$value": "16px", "$type": "dimension"}, ...},
          "shadow":     {"sm": {"$value": "0 1px 2px ...", "$type": "shadow"}, ...},
          ...
        }

    Intent: this is the canonical extraction payload Resemblio returns from
    its API and persists in Postgres (`dtcg_json` JSONB column). Downstream
    converters (Tailwind, Style Dictionary, shadcn) read this shape.

    Edge cases:
    - Unknown token keys fall into an "other" group with bare {"$value": ...}
      (no $type). Visible to consumers so naming drift surfaces fast.
    - Empty TokenSet returns {} (no required-keys check here; callers run
      validate_token_set first if they need that guarantee).
    """
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for key, value in token_set.items():
        group, dtype = _classify(key)
        leaf = _leaf_name(key)
        leaf_obj: dict[str, Any] = {"$value": value}
        if dtype:
            leaf_obj["$type"] = dtype
        out.setdefault(group, {})[leaf] = leaf_obj
    return out


# ----------------------------------------------------------------------
# Postgres row preparation
# ----------------------------------------------------------------------


class PostgresRow(TypedDict):
    """Row payload matching the `extractions` table schema.

    The caller (Codex's pipeline) takes this dict and executes the INSERT
    via SQLAlchemy or psycopg. This module deliberately does not own the
    DB connection - keeps the adapter pure-data and trivially testable.

    Fields:
    - url: the original URL as supplied
    - url_normalized: lowercased + stripped, used for dedupe lookups
    - status: "ok" | "failed" | "blocked" (caller's vocabulary)
    - dtcg_json: the DTCG token tree (Python dict, becomes JSONB in PG)
    - error_log: human-readable failure summary, or None on success
    - schema_version: SCHEMA_VERSION at write time; used for future migrations
    """
    url: str
    url_normalized: str
    status: str
    dtcg_json: dict[str, Any] | None
    error_log: str | None
    schema_version: int


def _normalize_url(url: str) -> str:
    """Lowercase and trim. Intentionally minimal: we do NOT collapse trailing
    slashes or strip queries here, because the user-supplied URL is the
    identifier and over-normalising would collapse distinct pages.

    Stronger normalisation (host vs path, scheme upgrade) belongs in a
    separate dedup layer the caller can opt into.
    """
    return url.strip().lower()


def to_postgres_row(
    url: str,
    token_set: TokenSet | None,
    status: str,
    error_log: str | None = None,
) -> PostgresRow:
    """Prepare the Postgres row payload for an extraction attempt.

    On success: pass the validated TokenSet; status="ok"; error_log=None.
    On failure: pass token_set=None; status="failed" (or "blocked");
    error_log=<message>. dtcg_json is None on failure.

    The function does NOT call validate_token_set - the caller is expected
    to have validated already. This keeps to_postgres_row a pure transform
    and lets the caller report validation errors through the same error_log
    channel.
    """
    dtcg = to_dtcg_json(token_set) if token_set is not None else None
    return PostgresRow(
        url=url,
        url_normalized=_normalize_url(url),
        status=status,
        dtcg_json=dtcg,
        error_log=error_log,
        schema_version=SCHEMA_VERSION,
    )


# ----------------------------------------------------------------------
# Extractor protocol
# ----------------------------------------------------------------------


class ResemblioExtractor(Protocol):
    """The contract Codex implements.

    A single method: given a URL, return either a validated TokenSet (success)
    or a human-readable error string (failure). Exactly one of the tuple
    slots is non-None.

    Implementations should:
    - Call recon_ping first; if reachability fails, return (None, "<reason>")
    - Call recon + fetch_html as needed to gather signal
    - Issue ONE Anthropic API call per URL (Sonnet 4.6)
    - Run validate_token_set on the model's output before returning
    - Never raise; surface every failure through the error string

    Why Protocol not ABC: Resemblio's CLI, web API, and MCP handler may each
    have a different extractor shape (sync, async, cached). The Protocol
    is structural so any of them satisfies the contract without inheritance.
    """

    def extract(self, url: str) -> tuple[TokenSet | None, str | None]:
        """Extract design tokens for a URL.

        Returns:
            (TokenSet, None) on success
            (None, error_message) on failure
        """
        ...
