"""Loader + assertion runner for real-URL ground-truth fixtures.

This module is the shared implementation behind
``test_ground_truth_fixtures.py`` (the production harness) and
``test_ground_truth_harness_meta.py`` (the meta-tests proving the
harness itself catches authoring bugs and rubric drift).

The fixtures it loads are described in
``tests/fixtures/ground_truth/README.md``. Each fixture asserts that
extracting a real URL produces a TokenSet within tolerance of a
human-authored ground truth. Two modes:

- **Snapshot mode** (default; CI-safe): assertions run against the
  fixture's ``extracted_payload_snapshot`` block. If absent the harness
  raises ``SkipFixture`` and the calling test skips.
- **Live mode** (opt-in): the test calls ``extractor.codex_extractor``
  against ``source_url`` and asserts on the live payload. The harness
  itself never reaches the network; it returns the loaded fixture and
  the comparison verdict.

Source dispatch: Jim Builder dispatch 2026-06-04 (R3-downstream cycle #1
ground-truth fixture set + assertion harness).

Throwaway: NO. Quality floor applies. Tested by
``tests/test_ground_truth_harness_meta.py``.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, TypedDict

import yaml

# ---------------------------------------------------------------------------
# Schema and tolerance constants
# ---------------------------------------------------------------------------

FIXTURE_SCHEMA_VERSION = "resemblio_ground_truth_v1"
"""On-disk fixture schema marker. Bump only on shape changes."""

DEFAULT_COLOR_DISTANCE_MAX = 8.0
"""Default Euclidean RGB distance tolerance for ``must_include_colors``.

Matches ``extractor.screenshot_palette.COLOR_SIMILARITY_THRESHOLD`` so a
color the screenshot-cross-check would treat as 'the same' is treated
as a match by ground-truth assertions too.
"""

DEFAULT_FONT_MATCH_MODE = "fuzzy"
"""Default font-family match mode.

- ``"fuzzy"`` accepts case-insensitive substring of the head segment.
  Matches "Inter" against "Inter, -apple-system, sans-serif".
- ``"exact"`` requires the canonical form (case- and whitespace-
  insensitive) to be identical.
"""

FONT_MATCH_MODES = frozenset({"fuzzy", "exact"})
"""Allowed values for ``tolerance.font_family_match``."""

# Hex pattern accepts #rgb, #rrggbb, #rrggbbaa. Alpha is preserved through
# parsing then dropped before distance compare (alpha changes do not
# correspond to brand-color identity for this layer of assertion).
_HEX_PATTERN = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


# ---------------------------------------------------------------------------
# Typed shapes (mirror the on-disk YAML; see fixtures/ground_truth/README.md)
# ---------------------------------------------------------------------------


class GroundTruthColor(TypedDict, total=False):
    """Named color slots a ground-truth fixture may declare.

    All slots are optional; a fixture asserts only the slots it can
    verify. Slot names match the DTCG flat TokenSet ``color`` shape used
    by ``extractor.drl_adapter.TokenSet`` where possible; novel names
    (``accent_primary``, ``accent_secondary``, ...) cover real-URL cases
    where the extractor's slot mapping is not 1:1 with the brand's role.
    """

    bg: str
    surface: str
    surface_2: str
    text: str
    text_strong: str
    text_muted: str
    accent: str
    accent_primary: str
    accent_secondary: str
    accent_tertiary: str
    accent_quaternary: str
    border: str
    hairline: str


class GroundTruthFontFamily(TypedDict, total=False):
    """Named font slots a fixture may declare."""

    body: str
    display: str
    mono: str


class GroundTruthBlock(TypedDict, total=False):
    """The ``ground_truth`` block of a fixture YAML."""

    color: GroundTruthColor
    font_family: GroundTruthFontFamily


class ToleranceBlock(TypedDict, total=False):
    """Per-fixture tolerance overrides. Defaults defined above."""

    color_distance_max: float
    font_family_match: Literal["fuzzy", "exact"]


class ExpectedBehavior(TypedDict, total=False):
    """The ``expected_extraction_behavior`` block of a fixture.

    Fields:
    - ``must_include_colors``: list of slot names from ``ground_truth.color``
      that MUST be represented in the extracted palette within tolerance.
      Slot names referenced here must exist in ``ground_truth.color``.
    - ``must_not_include_colors``: list of explicit hex strings that MUST
      NOT appear in the extracted palette. Catches known-wrong defaults.
    - ``must_emit_palette_completeness_warning``: when True the extracted
      payload must carry a truthy ``palette_completeness_warning`` field
      (R3 Option A signal). When False/absent no assertion runs on it.
    """

    must_include_colors: list[str]
    must_not_include_colors: list[str]
    must_emit_palette_completeness_warning: bool


class ExtractedPayloadSnapshot(TypedDict, total=False):
    """Observed extractor output captured for CI regression assertions.

    The shape mirrors what ``codex_extractor`` returns: a flat dict of
    color hex values keyed by slot, plus optional ``font_family`` and
    optional ``palette_completeness_warning``. Fixture authors paste in
    a payload captured from a real live-extraction run; the harness
    asserts against this in snapshot mode.
    """

    color: dict[str, str]
    font_family: dict[str, str]
    palette_completeness_warning: bool | str | None


class GroundTruthFixture(TypedDict, total=False):
    """The full on-disk fixture shape (see README in fixtures/ground_truth/)."""

    schema_version: str
    brand_slug: str
    source_url: str
    fixture_authored_at: str
    fixture_author: str
    provenance: str
    live_extraction_only: bool
    ground_truth: GroundTruthBlock
    tolerance: ToleranceBlock
    expected_extraction_behavior: ExpectedBehavior
    extracted_payload_snapshot: ExtractedPayloadSnapshot


# ---------------------------------------------------------------------------
# Exceptions + verdict shape
# ---------------------------------------------------------------------------


class FixtureShapeError(ValueError):
    """Raised when a fixture YAML fails shape validation.

    Carries the brand_slug (when parseable) and the first failing field
    so authoring drift is fast to diagnose. The harness intentionally
    fails on the FIRST shape problem rather than aggregating to keep the
    pytest report focused.
    """


class SkipFixture(Exception):
    """Raised when snapshot mode cannot run on a fixture.

    The calling test converts this into ``pytest.skip`` so missing
    snapshots are visible in the CI run output but do not fail the build.
    """


@dataclass(frozen=True)
class AssertionFailure:
    """One failed assertion within a fixture's behavior check.

    Multiple failures may accumulate; the harness returns the full list
    so the test report shows every problem at once (vs. fail-fast which
    hides the second and third defects behind the first).
    """

    kind: Literal[
        "color_missing",
        "color_forbidden_present",
        "palette_warning_mismatch",
        "font_mismatch",
    ]
    detail: str


@dataclass(frozen=True)
class AssertionResult:
    """Aggregate verdict of running expected_extraction_behavior."""

    fixture_slug: str
    failures: tuple[AssertionFailure, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


# ---------------------------------------------------------------------------
# Loader + shape validator
# ---------------------------------------------------------------------------


def load_fixture(path: Path) -> GroundTruthFixture:
    """Load + validate a fixture YAML.

    Raises ``FixtureShapeError`` on any shape problem. The validator is
    intentionally strict: missing schema_version, wrong schema_version,
    missing brand_slug, missing ground_truth, or any
    ``must_include_colors`` slot that doesn't exist in ``ground_truth.color``
    all fail at load time. This makes authoring drift loud at commit
    time instead of at test-run time.
    """
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise FixtureShapeError(
            f"{path.name}: top-level YAML must be a mapping, got {type(raw).__name__}"
        )

    slug = raw.get("brand_slug") or "<unknown>"

    if raw.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise FixtureShapeError(
            f"{slug}: schema_version must be {FIXTURE_SCHEMA_VERSION!r}, "
            f"got {raw.get('schema_version')!r}"
        )
    for required in ("brand_slug", "source_url", "fixture_authored_at", "fixture_author"):
        if not raw.get(required):
            raise FixtureShapeError(f"{slug}: required field {required!r} missing or empty")

    ground = raw.get("ground_truth") or {}
    if not isinstance(ground, dict) or not ground:
        raise FixtureShapeError(f"{slug}: ground_truth block missing or empty")
    colors = ground.get("color") or {}
    if not isinstance(colors, dict):
        raise FixtureShapeError(f"{slug}: ground_truth.color must be a mapping")
    for slot, hex_str in colors.items():
        if not _HEX_PATTERN.match(str(hex_str)):
            raise FixtureShapeError(
                f"{slug}: ground_truth.color.{slot} is not a valid hex: {hex_str!r}"
            )

    fonts = ground.get("font_family") or {}
    if fonts and not isinstance(fonts, dict):
        raise FixtureShapeError(f"{slug}: ground_truth.font_family must be a mapping")

    tolerance = raw.get("tolerance") or {}
    if tolerance:
        mode = tolerance.get("font_family_match", DEFAULT_FONT_MATCH_MODE)
        if mode not in FONT_MATCH_MODES:
            raise FixtureShapeError(
                f"{slug}: tolerance.font_family_match must be one of {sorted(FONT_MATCH_MODES)}, "
                f"got {mode!r}"
            )
        distance = tolerance.get("color_distance_max", DEFAULT_COLOR_DISTANCE_MAX)
        try:
            float(distance)
        except (TypeError, ValueError):
            raise FixtureShapeError(
                f"{slug}: tolerance.color_distance_max must be numeric, got {distance!r}"
            )

    expected = raw.get("expected_extraction_behavior") or {}
    if not isinstance(expected, dict):
        raise FixtureShapeError(
            f"{slug}: expected_extraction_behavior must be a mapping if present"
        )
    must_include = expected.get("must_include_colors") or []
    for slot in must_include:
        if slot not in colors:
            raise FixtureShapeError(
                f"{slug}: expected_extraction_behavior.must_include_colors references "
                f"slot {slot!r} which is not declared in ground_truth.color"
            )
    must_not = expected.get("must_not_include_colors") or []
    for hex_str in must_not:
        if not _HEX_PATTERN.match(str(hex_str)):
            raise FixtureShapeError(
                f"{slug}: expected_extraction_behavior.must_not_include_colors entry "
                f"is not a valid hex: {hex_str!r}"
            )

    return raw  # type: ignore[return-value]


def discover_fixtures(root: Path) -> list[Path]:
    """Return every top-level ``*.yaml`` under ``root``.

    Sub-directories (notably ``_meta/``) are NOT recursed; meta-test
    fixtures live in ``_meta/`` so they are loaded ONLY by the meta-test
    module via a direct path.
    """
    if not root.exists():
        return []
    return sorted(p for p in root.glob("*.yaml") if p.is_file())


# ---------------------------------------------------------------------------
# Color + font comparison primitives
# ---------------------------------------------------------------------------


def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Parse #rgb / #rrggbb / #rrggbbaa to an (r,g,b) tuple.

    Alpha is parsed but discarded; brand-color identity at this layer
    is about hue+saturation+value, not opacity. Caller is responsible
    for handing in a string already validated by ``_HEX_PATTERN``.
    """
    s = hex_str.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) == 8:
        s = s[:6]
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def rgb_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Euclidean RGB distance.

    Not perceptually uniform (Delta-E in Lab would be), but cheap, has
    no extra deps, and is consistent with the rest of the codebase's
    color-similarity reasoning (``screenshot_palette.rgb_distance``).
    The dispatch references this as 'Delta-E' colloquially; we are
    explicit here that the metric is sRGB Euclidean.
    """
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def color_present_in_palette(
    needle_hex: str,
    palette_hexes: Iterable[str],
    *,
    tolerance: float = DEFAULT_COLOR_DISTANCE_MAX,
) -> str | None:
    """Return the matched palette hex if ``needle_hex`` is within tolerance.

    Used by ``must_include_colors``: walks each palette entry and returns
    the first one whose RGB distance to the needle is <= tolerance. None
    means the needle is missing from the palette.
    """
    needle_rgb = hex_to_rgb(needle_hex)
    for entry in palette_hexes:
        if not _HEX_PATTERN.match(entry):
            continue
        if rgb_distance(needle_rgb, hex_to_rgb(entry)) <= tolerance:
            return entry
    return None


def font_matches(
    extracted: str,
    expected: str,
    *,
    mode: str = DEFAULT_FONT_MATCH_MODE,
) -> bool:
    """Compare extracted vs. expected font-family strings.

    Fuzzy mode (default) treats "Inter, -apple-system, sans-serif" as
    matching expected "Inter" because the first segment of the cascade
    is the brand intent and the rest is fallback. Exact mode normalizes
    whitespace and case but requires the whole string to match.
    """
    if mode == "exact":
        return _normalize_font(extracted) == _normalize_font(expected)
    head = extracted.split(",", 1)[0]
    return _normalize_font(expected) in _normalize_font(head) or (
        _normalize_font(extracted).startswith(_normalize_font(expected))
    )


def _normalize_font(s: str) -> str:
    """Lowercase, strip quotes and outer whitespace.

    Internal helper for ``font_matches``; not exposed publicly because
    the canonicalisation rules are tied to the matcher's contract and
    could change without notice if the matcher's semantics evolve.
    """
    return s.strip().strip("'\"").strip().lower()


# ---------------------------------------------------------------------------
# Assertion runner
# ---------------------------------------------------------------------------


def run_assertions(
    fixture: GroundTruthFixture,
    payload: ExtractedPayloadSnapshot,
) -> AssertionResult:
    """Run ``expected_extraction_behavior`` against an extracted payload.

    Pure-data function: takes the fixture and an extracted payload (live
    or snapshot), returns an ``AssertionResult`` with the full list of
    failures. The caller (a pytest test or a meta-test) decides whether
    to fail-or-skip based on the result and the run mode.

    Edge cases:
    - When ``expected_extraction_behavior`` is empty/absent the result
      is trivially passing. This is intentional: fixtures may carry only
      ground truth + tolerance during authoring, with the behavior
      block filled in later.
    - When the payload has no ``color`` block the
      ``must_include_colors`` check fails for every required slot. This
      is the modal Susann-class failure and must be loud.
    - When the fixture asserts ``must_emit_palette_completeness_warning``
      we check for a truthy value on the payload; both True and a
      populated string warning satisfy the contract.
    """
    failures: list[AssertionFailure] = []
    behavior: ExpectedBehavior = fixture.get("expected_extraction_behavior") or {}  # type: ignore[assignment]
    ground: GroundTruthBlock = fixture.get("ground_truth") or {}  # type: ignore[assignment]
    truth_colors: GroundTruthColor = ground.get("color") or {}  # type: ignore[assignment]
    tolerance: ToleranceBlock = fixture.get("tolerance") or {}  # type: ignore[assignment]
    distance_max = float(tolerance.get("color_distance_max", DEFAULT_COLOR_DISTANCE_MAX))
    font_mode = str(tolerance.get("font_family_match", DEFAULT_FONT_MATCH_MODE))

    extracted_colors = (payload.get("color") or {}) if isinstance(payload, dict) else {}
    palette_hexes = [v for v in extracted_colors.values() if isinstance(v, str)]

    for slot in behavior.get("must_include_colors", []) or []:
        expected_hex = truth_colors.get(slot)
        if not expected_hex:
            # Shape validation already prevents this at load time; the
            # guard exists for callers who construct fixtures in-memory
            # (meta-tests) and may not run load_fixture.
            failures.append(
                AssertionFailure(
                    "color_missing",
                    f"slot {slot!r}: ground_truth.color slot has no value",
                )
            )
            continue
        match = color_present_in_palette(
            expected_hex, palette_hexes, tolerance=distance_max
        )
        if match is None:
            failures.append(
                AssertionFailure(
                    "color_missing",
                    f"slot {slot!r}: expected {expected_hex} within {distance_max} of "
                    f"any of {palette_hexes!r}, no match",
                )
            )

    for forbidden in behavior.get("must_not_include_colors", []) or []:
        # Forbidden colors use a TIGHT tolerance (3.0) because a brand
        # might legitimately use a color near a forbidden default; the
        # forbidden check should only trip on values that are
        # functionally THE forbidden default.
        match = color_present_in_palette(forbidden, palette_hexes, tolerance=3.0)
        if match is not None:
            failures.append(
                AssertionFailure(
                    "color_forbidden_present",
                    f"forbidden {forbidden} present as {match} in extracted palette "
                    f"{palette_hexes!r}",
                )
            )

    expects_warning = bool(behavior.get("must_emit_palette_completeness_warning"))
    if expects_warning:
        warning = payload.get("palette_completeness_warning") if isinstance(payload, dict) else None
        if not warning:
            failures.append(
                AssertionFailure(
                    "palette_warning_mismatch",
                    "expected palette_completeness_warning to be truthy, "
                    f"got {warning!r}",
                )
            )

    truth_fonts: GroundTruthFontFamily = ground.get("font_family") or {}  # type: ignore[assignment]
    extracted_fonts = (payload.get("font_family") or {}) if isinstance(payload, dict) else {}
    for slot, expected_value in truth_fonts.items():
        if not expected_value:
            continue
        actual = extracted_fonts.get(slot)
        if not actual:
            # Font slot un-extracted is reported as a font_mismatch (not
            # color_missing) so the diagnostic class is clear.
            failures.append(
                AssertionFailure(
                    "font_mismatch",
                    f"font slot {slot!r}: expected {expected_value!r}, not extracted",
                )
            )
            continue
        if not font_matches(actual, expected_value, mode=font_mode):
            failures.append(
                AssertionFailure(
                    "font_mismatch",
                    f"font slot {slot!r}: expected {expected_value!r} "
                    f"(mode={font_mode}), got {actual!r}",
                )
            )

    return AssertionResult(
        fixture_slug=str(fixture.get("brand_slug", "<unknown>")),
        failures=tuple(failures),
    )


def resolve_payload_for_snapshot_mode(
    fixture: GroundTruthFixture,
) -> ExtractedPayloadSnapshot:
    """Return the snapshot payload, or raise SkipFixture.

    In snapshot mode we cannot reach the live extractor, so a fixture
    without a captured payload is genuinely un-testable in CI. The
    harness raises ``SkipFixture`` so the calling test can convert it
    into a pytest skip; this is preferable to silently passing.
    """
    snapshot = fixture.get("extracted_payload_snapshot")
    if not snapshot:
        if fixture.get("live_extraction_only"):
            raise SkipFixture(
                f"{fixture.get('brand_slug')}: live_extraction_only=true; "
                "snapshot mode cannot run"
            )
        raise SkipFixture(
            f"{fixture.get('brand_slug')}: extracted_payload_snapshot missing; "
            "capture via live-extraction run and paste in to enable CI regression"
        )
    return snapshot
