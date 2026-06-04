"""Compute per-fixture expected-vs-observed delta and emit the Markdown report.

Reads each fixture YAML at ../<slug>.yaml plus each captured payload at
./<slug>.json, normalizes the payload into the ExtractedPayloadSnapshot shape
the harness expects, runs the existing run_assertions(), and writes a
human-readable Markdown report at ./_delta_report.md.

Schema: resemblio_ground_truth_delta_v1.

Run command (from workspace root):
  python "projects/Resemblio/code/api/tests/fixtures/ground_truth/observed/_compute_delta.py"

Dependencies: PyYAML (already in the api venv); no network; no extractor import.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from pathlib import Path

_HEX_PATTERN = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

_HERE = Path(__file__).resolve().parent
_API_ROOT = _HERE.parents[3]
sys.path.insert(0, str(_API_ROOT))

from tests.ground_truth_harness import (  # noqa: E402
    AssertionResult,
    discover_fixtures,
    load_fixture,
    run_assertions,
)

REPORT_SCHEMA_VERSION = "resemblio_ground_truth_delta_v2"
"""v2 (cycle #1.5): payload shape changed from nested {color, font_family}
to FLAT {tokens: {...}} to mirror the real POST /v1/extractions response.
"""


def _coerce_payload(raw_api_response: dict) -> dict:
    """Map the API's extraction-response shape to ExtractedPayloadSnapshot v2.

    v2 (2026-06-04 cycle #1.5): the real ``POST /v1/extractions``
    response carries a top-level FLAT ``tokens`` dict (bg/text/accent
    plus font_body/font_display/font_mono plus dimension/duration tokens
    interleaved). Cycle #1 of _coerce_payload assumed a nested
    ``tokens.color`` / ``tokens.font_family`` shape that does not exist
    in any captured API response. v2 returns the flat tokens dict
    directly so the harness can walk it as designed.

    Error-shape responses (e.g. ``{"error": "insufficient_credit", ...}``
    on Susann) carry no tokens; this helper returns an empty tokens dict
    so the harness reports the brand as a wholesale failure rather than
    crashing.
    """
    if not isinstance(raw_api_response, dict):
        return {"tokens": {}, "palette_completeness_warning": None}
    tokens = raw_api_response.get("tokens")
    if not isinstance(tokens, dict):
        # Error envelope or unexpected shape: surface an empty palette
        # so run_assertions reports every must_include_colors as missing.
        return {"tokens": {}, "palette_completeness_warning": None}
    return {
        "tokens": tokens,
        "palette_completeness_warning": raw_api_response.get(
            "palette_completeness_warning"
        ),
    }


def _fmt_failure_lines(result: AssertionResult) -> list[str]:
    if result.passed:
        return ["- All assertions PASS."]
    return [f"- **{f.kind}**: {f.detail}" for f in result.failures]


def main() -> int:
    fixture_root = _HERE.parent
    fixtures = discover_fixtures(fixture_root)
    lines: list[str] = []
    lines.append("# Ground-truth observed-vs-expected delta report")
    lines.append("")
    lines.append(f"- schema_version: `{REPORT_SCHEMA_VERSION}`")
    lines.append(f"- generated_at: `{_dt.datetime.utcnow().isoformat()}Z`")
    lines.append(f"- fixture_count: {len(fixtures)}")
    lines.append("- harness: `tests/ground_truth_harness.py`")
    lines.append("- source dispatch: R3-downstream cycle #1.5 (observed-payload capture)")
    lines.append("")

    summary_rows: list[tuple[str, str, int, int]] = []
    per_fixture_blocks: list[str] = []

    for fixture_path in fixtures:
        fixture = load_fixture(fixture_path)
        slug = fixture["brand_slug"]
        observed_path = _HERE / f"{slug}.json"
        block: list[str] = []
        block.append(f"## {slug}")
        block.append("")
        block.append(f"- source_url: {fixture.get('source_url')}")
        block.append(f"- observed_payload: `tests/fixtures/ground_truth/observed/{slug}.json`")

        if not observed_path.exists():
            block.append(f"- status: **MISSING** (no captured payload at {observed_path.name})")
            block.append("")
            summary_rows.append((slug, "MISSING", 0, 0))
            per_fixture_blocks.append("\n".join(block))
            continue

        with observed_path.open("r", encoding="utf-8") as fh:
            raw_payload = json.load(fh)
        payload = _coerce_payload(raw_payload)

        expected_colors = (fixture.get("ground_truth") or {}).get("color") or {}
        expected_fonts = (fixture.get("ground_truth") or {}).get("font_family") or {}
        # v2 flat shape: split tokens for human-readable rendering.
        observed_tokens = payload.get("tokens") or {}
        observed_colors: dict[str, str] = {}
        observed_fonts: dict[str, str] = {}
        for k, v in observed_tokens.items():
            if not isinstance(v, str):
                continue
            if k.startswith("font_"):
                observed_fonts[k.removeprefix("font_")] = v
            elif _HEX_PATTERN.match(v):
                observed_colors[k] = v

        result = run_assertions(fixture, payload)
        verdict = "PASS" if result.passed else "FAIL"
        block.append(f"- verdict: **{verdict}** ({len(result.failures)} failure(s))")
        block.append("")
        block.append("### Expected colors")
        for slot, hexv in expected_colors.items():
            block.append(f"- `{slot}`: {hexv}")
        block.append("")
        block.append("### Observed colors")
        if not observed_colors:
            block.append("- (none extracted)")
        else:
            for slot, hexv in observed_colors.items():
                block.append(f"- `{slot}`: {hexv}")
        block.append("")
        block.append("### Expected fonts")
        if not expected_fonts:
            block.append("- (none asserted)")
        else:
            for slot, fam in expected_fonts.items():
                block.append(f"- `{slot}`: {fam}")
        block.append("")
        block.append("### Observed fonts")
        if not observed_fonts:
            block.append("- (none extracted)")
        else:
            for slot, fam in observed_fonts.items():
                block.append(f"- `{slot}`: {fam}")
        block.append("")
        block.append("### Assertion failures")
        block.extend(_fmt_failure_lines(result))
        block.append("")
        summary_rows.append((slug, verdict, len(expected_colors), len(observed_colors)))
        per_fixture_blocks.append("\n".join(block))

    lines.append("## Summary")
    lines.append("")
    lines.append("| brand | verdict | expected_color_slots | observed_color_slots |")
    lines.append("|---|---|---|---|")
    for slug, verdict, exp_n, obs_n in summary_rows:
        lines.append(f"| {slug} | {verdict} | {exp_n} | {obs_n} |")
    lines.append("")
    lines.extend(per_fixture_blocks)

    report_path = _HERE / "_delta_report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
