"""The ExtractionRecord contract — the deterministic boundary between LLM
extraction agents and Python asset assembly.

## The architectural shift

Before this module: every sub-agent did extraction + assembly in one prompt.
The agent read pages, identified design tokens, wrote four files (asset.html,
tokens.css, meta.json, README.md), and reported back. ~17 agents per system
wave × ~100K tokens = ~1.7M tokens, ~80% of it spent on assembly templating.

After this module: agents do extraction ONLY. They return one structured
`ExtractionRecord` per source. Python (in `_scripts/compose.py`) reads the
record and emits the asset corpus from templates. The same wave drops to
~3 agents and ~150K tokens.

## What lives in an ExtractionRecord

- `tokens`: TokenSet — the source's complete `--ds-*` contract values,
  pulled from devtools or carefully cross-checked WebFetch inspection.
- `sections`: dict[class_name, SectionOutline] — one entry per applicable
  class (hero, navigation, footer, feature-grid, etc.). Each entry records
  the variant, the verified content samples, and the source URLs.
- `design_principles`: list[str] — the 5-7 named recurring choices distilled
  from observation.
- `skips`: list[SkipReason] — classes the agent honestly couldn't extract,
  with structured rationale (paths tried, error category) so Python can
  auto-flip the completeness_checklist.

## How agents produce a record

1. The slate prompts (see `_scripts/slate.py`) tell each agent to return
   their extraction as JSON conforming to one of: `TokenSet`, `SectionOutline`,
   or `SkipReason`. (For now, agents still produce the assembled asset
   files; the migration to extraction-only is incremental.)
2. The orchestrator collects N agent JSON returns and merges them into
   one `ExtractionRecord` at `_extractions/<slug>/extraction.json`.
3. `_scripts/compose.py` reads the record and emits the on-disk corpus.

## Why TypedDict, not dataclass

The record is serialized to JSON and round-trips between Python and the
agent's text output. TypedDict is structural (the JSON dict IS the record),
no marshalling needed. `validate_extraction` confirms the shape at the
trust boundary.

## Run command

    python -m _scripts.test_extraction       # unit tests
    python -c "from _scripts.extraction import validate_extraction; ..."

Throwaway: no. Quality floor applies.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

SCHEMA_VERSION = 1
"""Bump on incompatible shape changes to ExtractionRecord."""

VALID_PROVENANCE_SCORES = frozenset({"A", "B", "C", "D", "F"})
"""Per PROVENANCE_RUBRIC.md."""

VALID_EXTRACTION_METHODS = frozenset({
    "devtools_computed_style",
    "webfetch_html_inspection",
    "screenshot_inspection",
    "wayback_capture",
    "composed_from_atoms",   # for alphabet/library composition from prior A-grade atoms
})
"""Per PROVENANCE_RUBRIC.md plus the documented composition path."""

VALID_SKIP_CATEGORIES = frozenset({
    "chrome_mcp_blocked",
    "webfetch_css_stripped",
    "wayback_unavailable",
    "site_outage",
    "pattern_not_applicable",     # genuinely absent on the source
    "post_acquisition_delegated", # e.g., Loom→Atlassian
    "client_side_rendered",       # content not in SSR HTML
    "other",
})
"""Why an extraction skipped. Determines auto-flip rule for checklist."""


# ----------------------------------------------------------------------
# Type contracts
# ----------------------------------------------------------------------


class TokenSet(TypedDict, total=False):
    """The source's --ds-* contract values as a flat dict.

    Keys follow TOKEN_CONTRACT.md exactly (drop the `--ds-` prefix and
    convert to snake_case for JSON-friendliness). Required keys are
    enforced by `validate_token_set`; optional keys carry sensible
    defaults.

    Colors (15):
    """
    # Colors (15)
    bg: str
    surface: str
    surface_2: str
    text: str
    text_muted: str
    text_strong: str
    border: str
    hairline: str
    accent: str
    accent_2: str
    success: str
    warning: str
    error: str
    info: str
    focus_ring: str
    # Type families (4)
    font_display: str
    font_body: str
    font_mono: str
    font_accent: str
    # Type sizes (12) — strings so px/em/rem all serialize
    text_2xs: str
    text_xs: str
    text_sm: str
    text_base: str
    text_lg: str
    text_xl: str
    text_2xl: str
    text_3xl: str
    text_4xl: str
    text_5xl: str
    text_6xl: str
    text_7xl: str
    # Line heights (5)
    leading_tight: str
    leading_snug: str
    leading_normal: str
    leading_relaxed: str
    leading_loose: str
    # Tracking (4)
    tracking_tight: str
    tracking_normal: str
    tracking_wide: str
    tracking_wider: str
    # Spacing (12)
    space_0: str
    space_1: str
    space_2: str
    space_3: str
    space_4: str
    space_5: str
    space_6: str
    space_8: str
    space_10: str
    space_12: str
    space_16: str
    space_32: str
    # Radii (6)
    radius_none: str
    radius_xs: str
    radius_sm: str
    radius_md: str
    radius_lg: str
    radius_full: str
    # Shadows (6)
    shadow_none: str
    shadow_xs: str
    shadow_sm: str
    shadow_md: str
    shadow_lg: str
    shadow_2xl: str
    # Motion (8)
    ease_standard: str
    ease_emphasize: str
    ease_decelerate: str
    ease_accelerate: str
    duration_instant: str
    duration_fast: str
    duration_normal: str
    duration_slow: str


# Required color slots — the validator refuses a TokenSet missing any of these.
REQUIRED_TOKEN_KEYS: tuple[str, ...] = (
    "bg", "surface", "text", "text_muted", "border", "hairline",
    "accent", "font_display", "font_body", "font_mono",
    "text_base", "text_lg", "text_xl", "text_2xl", "text_3xl", "text_4xl",
    "leading_tight", "leading_normal",
    "space_1", "space_2", "space_3", "space_4", "space_5", "space_6",
    "radius_sm", "radius_md",
)
"""Subset of TokenSet keys the validator requires. Other keys carry sensible
defaults at compose time, but these must be supplied by extraction."""


class InspiredByEntry(TypedDict, total=False):
    """One provenance row for a section. Mirrors meta.json inspired_by entry."""
    site: str
    url: str
    captured: str               # ISO date
    archive_url: str | None
    element: str                # what was inspected (selector or human description)
    extraction_method: str      # VALID_EXTRACTION_METHODS
    provenance_score: str       # VALID_PROVENANCE_SCORES


class SectionOutline(TypedDict, total=False):
    """One section/component class extracted from the source.

    The agent's job is to fill these fields verifiably. compose.py renders
    the template using `variant` + `content_samples` against the system's
    tokens.
    """
    class_name: str             # e.g. "hero", "navigation", "marketing-page", "buttons"
    kind: str                   # "atom" | "alphabet" | "library" | "layout" | "whole"
    variant: str                # named pattern from TAXONOMY (e.g. "dual-cta-hero")
    pattern_tags: list[str]     # other patterns this section uses
    mood: list[str]             # TAXONOMY mood terms
    applicable_to: list[str]    # TAXONOMY applicable_to terms
    section_sequence: list[str] # for layouts: ordered list of section types
    content_samples: dict[str, str]  # lorem-friendly named slots (headline, dek, kicker, etc.)
    tldr: str                   # one-line summary, ≤ 200 chars
    notes: list[str]            # design-intent notes; rendered into asset.html header + README
    inspired_by: list[InspiredByEntry]
    provenance_score: str       # min across inspired_by
    composition_atoms: list[str]  # for wholes: which atoms this composes


class SkipReason(TypedDict):
    """Structured skip log entry. compose.py uses these to auto-flip
    `missing → not-applicable` in the completeness_checklist when the
    `category` is `pattern_not_applicable`.
    """
    class_name: str
    category: str               # VALID_SKIP_CATEGORIES
    paths_tried: list[str]      # URLs probed, MCP attempts, etc.
    rationale: str              # human-readable explanation
    suggested_action: str       # "flip-not-applicable" | "retry-later" | "screenshot-intake"


class ExtractionRecord(TypedDict):
    """The full extraction artifact for one source.

    Lives at `_extractions/<system_slug>/extraction.json`.
    """
    schema_version: int
    system_slug: str
    captured: str               # ISO date
    tokens: TokenSet
    sections: dict[str, SectionOutline]   # keyed by class_name
    design_principles: list[str]
    skips: list[SkipReason]


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


class ExtractionValidationError(ValueError):
    """Raised when an ExtractionRecord violates the schema contract."""


def validate_token_set(tokens: dict, *, source: str = "tokens") -> None:
    """Confirm a TokenSet has every required key.

    Raises ExtractionValidationError on the first missing key. `source` is
    used in the error message to identify which record failed.
    """
    if not isinstance(tokens, dict):
        raise ExtractionValidationError(
            f"{source}: TokenSet must be a dict, got {type(tokens).__name__}"
        )
    missing = [k for k in REQUIRED_TOKEN_KEYS if k not in tokens]
    if missing:
        raise ExtractionValidationError(
            f"{source}: TokenSet missing required keys: {missing}"
        )
    # Every value must be a non-empty string.
    for key in REQUIRED_TOKEN_KEYS:
        v = tokens[key]
        if not isinstance(v, str) or not v.strip():
            raise ExtractionValidationError(
                f"{source}.{key}: must be a non-empty string, got {v!r}"
            )


def validate_inspired_by(entry: dict, *, source: str = "inspired_by") -> None:
    """Confirm an inspired_by entry has the four anchor fields."""
    if not isinstance(entry, dict):
        raise ExtractionValidationError(
            f"{source}: must be a dict, got {type(entry).__name__}"
        )
    for field in ("site", "url", "extraction_method", "provenance_score"):
        if not entry.get(field):
            raise ExtractionValidationError(
                f"{source}: missing required field '{field}'"
            )
    method = entry.get("extraction_method")
    if method not in VALID_EXTRACTION_METHODS:
        raise ExtractionValidationError(
            f"{source}.extraction_method: '{method}' not in "
            f"{sorted(VALID_EXTRACTION_METHODS)}"
        )
    score = entry.get("provenance_score")
    if score not in VALID_PROVENANCE_SCORES:
        raise ExtractionValidationError(
            f"{source}.provenance_score: '{score}' not in "
            f"{sorted(VALID_PROVENANCE_SCORES)}"
        )


def validate_section_outline(section: dict, *, source: str = "section") -> None:
    """Confirm a SectionOutline has the required fields + valid sub-entries."""
    if not isinstance(section, dict):
        raise ExtractionValidationError(
            f"{source}: must be a dict, got {type(section).__name__}"
        )
    for field in ("class_name", "kind", "variant", "tldr", "inspired_by"):
        if field not in section:
            raise ExtractionValidationError(
                f"{source}: missing required field '{field}'"
            )
    if section["kind"] not in ("atom", "alphabet", "library", "layout", "whole"):
        raise ExtractionValidationError(
            f"{source}.kind: invalid value '{section['kind']}'"
        )
    if not isinstance(section.get("tldr", ""), str) or len(section["tldr"]) > 200:
        raise ExtractionValidationError(
            f"{source}.tldr: must be a string ≤ 200 chars"
        )
    ib = section.get("inspired_by")
    if not isinstance(ib, list) or not ib:
        raise ExtractionValidationError(
            f"{source}.inspired_by: must be a non-empty list"
        )
    for i, entry in enumerate(ib):
        validate_inspired_by(entry, source=f"{source}.inspired_by[{i}]")


def validate_skip_reason(skip: dict, *, source: str = "skip") -> None:
    """Confirm a SkipReason has required fields + valid category."""
    if not isinstance(skip, dict):
        raise ExtractionValidationError(
            f"{source}: must be a dict, got {type(skip).__name__}"
        )
    for field in ("class_name", "category", "rationale", "suggested_action"):
        if field not in skip:
            raise ExtractionValidationError(
                f"{source}: missing required field '{field}'"
            )
    if skip["category"] not in VALID_SKIP_CATEGORIES:
        raise ExtractionValidationError(
            f"{source}.category: '{skip['category']}' not in "
            f"{sorted(VALID_SKIP_CATEGORIES)}"
        )


def validate_extraction(record: dict) -> None:
    """Confirm an ExtractionRecord conforms to the schema.

    Raises ExtractionValidationError with a path-prefixed message on the
    first violation. Successful return = the record is safe to feed to
    compose.py.
    """
    if not isinstance(record, dict):
        raise ExtractionValidationError(
            f"record: must be a dict, got {type(record).__name__}"
        )
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ExtractionValidationError(
            f"record.schema_version: must be {SCHEMA_VERSION}, "
            f"got {record.get('schema_version')!r}"
        )
    if not record.get("system_slug"):
        raise ExtractionValidationError("record.system_slug: required")
    if not record.get("captured"):
        raise ExtractionValidationError("record.captured: required (ISO date)")

    validate_token_set(record.get("tokens") or {}, source="record.tokens")

    sections = record.get("sections") or {}
    if not isinstance(sections, dict):
        raise ExtractionValidationError(
            f"record.sections: must be a dict, got {type(sections).__name__}"
        )
    for key, section in sections.items():
        validate_section_outline(section, source=f"record.sections[{key!r}]")
        if section.get("class_name") != key:
            raise ExtractionValidationError(
                f"record.sections[{key!r}].class_name: must equal key "
                f"'{key}', got {section.get('class_name')!r}"
            )

    principles = record.get("design_principles")
    if not isinstance(principles, list):
        raise ExtractionValidationError(
            "record.design_principles: must be a list"
        )

    skips = record.get("skips") or []
    if not isinstance(skips, list):
        raise ExtractionValidationError(
            "record.skips: must be a list (may be empty)"
        )
    for i, skip in enumerate(skips):
        validate_skip_reason(skip, source=f"record.skips[{i}]")


# ----------------------------------------------------------------------
# Read / write helpers
# ----------------------------------------------------------------------


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXTRACTIONS_ROOT = PROJECT_ROOT / "_extractions"
"""Where ExtractionRecord JSON lives. One subdir per system slug."""


def read_extraction(slug: str) -> ExtractionRecord:
    """Load + validate `_extractions/<slug>/extraction.json`.

    Raises FileNotFoundError if missing, ExtractionValidationError if invalid.
    """
    path = EXTRACTIONS_ROOT / slug / "extraction.json"
    if not path.exists():
        raise FileNotFoundError(
            f"extraction.json missing for {slug}: expected at {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ExtractionValidationError(
            f"extraction.json for {slug} unparseable: {e}"
        )
    validate_extraction(data)
    return data  # type: ignore[return-value]


def write_extraction(slug: str, record: dict) -> Path:
    """Validate + write an ExtractionRecord to disk. Returns the output path."""
    validate_extraction(record)
    out_dir = EXTRACTIONS_ROOT / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "extraction.json"
    out_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return out_path
