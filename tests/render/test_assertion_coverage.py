"""Per-assertion structural guard and coverage completeness (Phase 10 + 11).

Phase 10.1 RED: test_every_spec_assertion_is_exercised asserted that the number
of exercised assertions equals the total across all specs. At RED, _ASSERTION_PARAMS
was an empty placeholder (0 cases). 0 != 110 -> FAIL.

Phase 10.2 GREEN: _ASSERTION_PARAMS is derived from the corpus (all 110 assertions)
and test_assertion_structural_guard provides one parametrized case per assertion.
exercised == total -> PASS.

Phase 11.1 GREEN guard: test_every_no_leak_assertion_parses_to_nonempty_tokens
verifies that every corpus forbidden.every assertion can be parsed to a non-empty
token list. At introduction (2026-06-14) all 17 no-leak assertions use exact spacing
and the guard is GREEN. It is a regression guard, not a TDD-RED test - see Phase 11
PRD for rationale.

Assertion shapes handled by evaluate_assertion_against_live_html:
  - text_content (40 assertions): expected_text substring check
  - evaluate:includes (47 assertions): .includes('token') presence check
  - evaluate:forbidden_every (17 assertions): no-wordmark-logo-leak family;
    all forbidden tokens must be absent
  - evaluate:unrecognized (6 assertions): avatars-photo-stripped; complex DOM
    querySelectorAll evaluator that requires browser execution. Evaluator
    returns False (conservative). Guard verifies the conservative behavior.

Design constraint: _ASSERTION_PARAMS resolved at import time from the vendored
in-repo corpus (CORPUS_ROOT / reference_captures / specs). On a standalone CI
checkout without the workspace _verification/ tree the corpus is still present
(Phase 8 deliverable), so all 110 cases RUN rather than self-skip.

Self-skip semantics: both test_every_spec_assertion_is_exercised and
test_assertion_structural_guard self-skip when SPECS_DIR is absent (corpus not
vendored - should not occur since Phase 8 delivered the in-repo mirror).

Schema: phase11_assertion_coverage_v1
"""
from __future__ import annotations

import json
import pathlib
from typing import Dict, List, Optional, Tuple

import pytest

from .assertion_eval import (
    evaluate_assertion_against_live_html,
    expected_token_from_assertion,
    forbidden_tokens_from_evaluator,
)
from .conftest import CORPUS_ROOT
from .assertion_eval import NO_LEAK_ID_MARKER

SPECS_DIR: pathlib.Path = CORPUS_ROOT / "reference_captures" / "specs"

# Recognized evaluator shapes; anything else is "unrecognized".
_SHAPE_TEXT_CONTENT = "text_content"
_SHAPE_INCLUDES = "includes"
_SHAPE_FORBIDDEN_EVERY = "forbidden_every"
_SHAPE_UNRECOGNIZED = "unrecognized"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _assertion_shape(assertion: Dict) -> str:
    """Return the evaluator shape for one assertion dict.

    Returns one of the four _SHAPE_* constants. Pure function; no network.
    """
    kind = assertion.get("kind")
    if kind == "text_content":
        return _SHAPE_TEXT_CONTENT
    evaluator = assertion.get("evaluate", "")
    if not isinstance(evaluator, str):
        return _SHAPE_UNRECOGNIZED
    if "forbidden.every" in evaluator:
        return _SHAPE_FORBIDDEN_EVERY
    if ".includes(" in evaluator:
        return _SHAPE_INCLUDES
    return _SHAPE_UNRECOGNIZED


def _build_synthetic_html(
    assertion: Dict, shape: str,
) -> Tuple[str, str]:
    """Build (positive_html, negative_html) for one assertion.

    positive_html: HTML where the assertion SHOULD pass (observed == expected).
    negative_html: HTML where the assertion SHOULD fail (observed != expected).

    Only called for recognized shapes (text_content, includes, forbidden_every).
    Conservative fallback for unrecognized shapes is handled by the guard itself.

    Pure function; no network.
    """
    if shape == _SHAPE_TEXT_CONTENT:
        text = str(assertion.get("expected_text", ""))
        positive = f"<html><body><aside>{text}</aside></body></html>"
        negative = "<html><body><aside>no matching disclosure</aside></body></html>"

    elif shape == _SHAPE_INCLUDES:
        token = expected_token_from_assertion(assertion) or "placeholder-token"
        positive = f"<html><body><aside>Uses {token} font.</aside></body></html>"
        negative = "<html><body><aside>Uses Times New Roman.</aside></body></html>"

    elif shape == _SHAPE_FORBIDDEN_EVERY:
        evaluator = str(assertion.get("evaluate", ""))
        tokens = forbidden_tokens_from_evaluator(evaluator)
        # positive: no forbidden tokens -> observed=True -> True==True -> passes
        positive = "<html><body><p>Clean content, no brand assets.</p></body></html>"
        # negative: first forbidden token present -> observed=False -> fails
        if tokens:
            negative = (
                f"<html><body>"
                f"<img src='{tokens[0]}'>"
                f"</body></html>"
            )
        else:
            negative = "<html><body><img src='/brand/logo.png'></body></html>"

    else:
        # Should not reach here; guard handles unrecognized before calling this.
        positive = "<html><body></body></html>"
        negative = "<html><body></body></html>"

    return positive, negative


def _discover_assertions(specs_dir: pathlib.Path) -> List[Tuple[str, str, str]]:
    """Return sorted (brand, category, assertion_id) triples from all vendored specs.

    Pure function (no os.environ / __file__ access). Returns an empty list when
    specs_dir is absent - the parametrized guard then has zero cases and
    test_every_spec_assertion_is_exercised self-skips.

    Derives (brand, category) by splitting the stem on the first underscore:
      ``aeon_about-team.json`` -> brand=``aeon``, category=``about-team``.
    assertion_id comes from each assertion's ``id`` field.
    """
    if not specs_dir.is_dir():
        return []
    params: List[Tuple[str, str, str]] = []
    for spec_file in sorted(specs_dir.glob("*.json")):
        parts = spec_file.stem.split("_", 1)
        if len(parts) != 2:
            continue
        brand, category = parts
        try:
            spec = json.loads(spec_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for assertion in spec.get("assertions", []) or []:
            aid = assertion.get("id")
            if isinstance(aid, str) and aid:
                params.append((brand, category, aid))
    return params


# ---------------------------------------------------------------------------
# Module-level assertion scan (Phase 10.2 GREEN: populated from corpus)
# ---------------------------------------------------------------------------

_ASSERTION_PARAMS: List[Tuple[str, str, str]] = _discover_assertions(SPECS_DIR)


# ---------------------------------------------------------------------------
# Unit tests for _discover_assertions (pure; no network)
# ---------------------------------------------------------------------------


def test_discover_assertions_absent_dir(tmp_path: pathlib.Path) -> None:
    """An absent directory yields an empty list (no exception)."""
    assert _discover_assertions(tmp_path / "missing") == []


def test_discover_assertions_skips_file_without_underscore(
    tmp_path: pathlib.Path,
) -> None:
    """Spec files without an underscore in the stem are skipped."""
    (tmp_path / "readme.json").write_text("{}", encoding="utf-8")
    assert _discover_assertions(tmp_path) == []


def test_discover_assertions_extracts_triples(tmp_path: pathlib.Path) -> None:
    """Returns (brand, category, assertion_id) triples for each assertion."""
    spec = {
        "schema_version": "fidelity_spec_v2",
        "assertions": [
            {"id": "aeon-font-uses-free-alt", "evaluate": "true"},
            {"id": "aeon-no-wordmark-logo-leak", "evaluate": "true"},
        ],
    }
    (tmp_path / "aeon_alphabet.json").write_text(
        json.dumps(spec), encoding="utf-8"
    )
    result = _discover_assertions(tmp_path)
    assert result == [
        ("aeon", "alphabet", "aeon-font-uses-free-alt"),
        ("aeon", "alphabet", "aeon-no-wordmark-logo-leak"),
    ]


def test_discover_assertions_skips_missing_id(tmp_path: pathlib.Path) -> None:
    """Assertions with no id field are silently skipped."""
    spec = {
        "assertions": [
            {"evaluate": "true"},  # missing id
            {"id": "aeon-valid", "evaluate": "true"},
        ]
    }
    (tmp_path / "aeon_alphabet.json").write_text(
        json.dumps(spec), encoding="utf-8"
    )
    result = _discover_assertions(tmp_path)
    assert result == [("aeon", "alphabet", "aeon-valid")]


def test_discover_assertions_sorted(tmp_path: pathlib.Path) -> None:
    """Results are sorted by spec filename (brand, category alphabetically)."""
    for fname, aid in [
        ("stripe_alphabet.json", "stripe-font"),
        ("aeon_buttons.json", "aeon-btn-font"),
        ("aeon_alphabet.json", "aeon-font"),
    ]:
        spec = {"assertions": [{"id": aid, "evaluate": "true"}]}
        (tmp_path / fname).write_text(json.dumps(spec), encoding="utf-8")
    result = _discover_assertions(tmp_path)
    assert result == [
        ("aeon", "alphabet", "aeon-font"),
        ("aeon", "buttons", "aeon-btn-font"),
        ("stripe", "alphabet", "stripe-font"),
    ]


def test_assertion_shape_text_content() -> None:
    """text_content kind is recognized correctly."""
    assert _assertion_shape({"kind": "text_content", "expected_text": "X"}) == _SHAPE_TEXT_CONTENT


def test_assertion_shape_includes() -> None:
    """evaluate with .includes( is recognized as includes shape."""
    assertion = {"evaluate": "return fam.includes('inter');"}
    assert _assertion_shape(assertion) == _SHAPE_INCLUDES


def test_assertion_shape_forbidden_every() -> None:
    """evaluate with forbidden.every is recognized as forbidden_every shape."""
    assertion = {"evaluate": "return forbidden.every(s => !html.includes(s));"}
    assert _assertion_shape(assertion) == _SHAPE_FORBIDDEN_EVERY


def test_assertion_shape_unrecognized() -> None:
    """Complex DOM evaluator (querySelectorAll) is classified as unrecognized."""
    assertion = {
        "evaluate": "const m = document.querySelectorAll('.cls'); return m.length > 0;"
    }
    assert _assertion_shape(assertion) == _SHAPE_UNRECOGNIZED


# ---------------------------------------------------------------------------
# Coverage completeness (Phase 10.1 RED -> Phase 10.2 GREEN)
# ---------------------------------------------------------------------------


def test_every_spec_assertion_is_exercised() -> None:
    """Every assertion in every spec must be a parametrized guard case.

    Phase 10.1 RED: _ASSERTION_PARAMS = [] -> exercised = 0 != 110 -> FAIL.
    Phase 10.2 GREEN: _ASSERTION_PARAMS derived from the corpus at module load.
    exercised == 110 -> PASS.

    Self-skips when SPECS_DIR is absent (corpus not vendored - should not
    happen since Phase 8 delivered the in-repo mirror).

    The count is derived from the parametrize list rather than a magic literal
    so that adding a brand's specs later automatically raises the bar.
    """
    if not SPECS_DIR.exists():
        pytest.skip(
            f"Specs dir absent at {SPECS_DIR}; "
            "vendor the corpus via scripts/sync_fidelity_corpus.py (Phase 8)."
        )

    total = 0
    for spec_file in sorted(SPECS_DIR.glob("*.json")):
        try:
            spec = json.loads(spec_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        total += len([
            a for a in spec.get("assertions", []) or []
            if isinstance(a.get("id"), str) and a["id"]
        ])

    exercised = len(_ASSERTION_PARAMS)
    assert exercised == total, (
        f"guard evaluates {exercised} of {total} assertions; "
        f"{total - exercised} unexercised. "
        "Add a per-assertion parametrized guard case for every assertion in "
        "every vendored spec (Phase 10 per "
        "_HANDOFF_2026-06-13_library-v5-phase10-full-assertion-guard.md). "
        "Populate _ASSERTION_PARAMS via _discover_assertions(SPECS_DIR)."
    )


# ---------------------------------------------------------------------------
# Per-assertion structural guard (Phase 10.2 GREEN)
# One case per (brand, category, assertion_id) across all 110 assertions.
# Runs on a standalone CI checkout (corpus vendored in-repo via Phase 8).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "brand,category,assertion_id",
    _ASSERTION_PARAMS,
    ids=[f"{b}__{c}__{aid}" for b, c, aid in _ASSERTION_PARAMS],
)
def test_assertion_structural_guard(
    brand: str, category: str, assertion_id: str,
) -> None:
    """Per-assertion guard: every assertion evaluates correctly against synthetic HTML.

    For each of the 110 assertions across 20 specs:

    Recognized shapes (text_content, includes, forbidden_every):
      - Builds synthetic positive HTML (assertion should pass) and asserts
        evaluate_assertion_against_live_html returns assertion's expected value.
      - Builds synthetic negative HTML (assertion should fail) and asserts
        the evaluator returns the opposite of expected.

    Unrecognized shapes (avatars-photo-stripped, 6 assertions):
      - These use complex DOM querySelectorAll evaluators that require real
        browser execution. evaluate_assertion_against_live_html conservatively
        returns False for any HTML. The guard verifies this conservative behavior
        and returns; no positive/negative HTML test is possible without a browser.

    Pure-data: no network, no Playwright, no PNGs. Runs on a standalone CI
    checkout via CORPUS_ROOT (the vendored in-repo copy, Phase 8 deliverable).
    Self-skips when SPECS_DIR is absent (should not occur post-Phase-8).
    """
    if not SPECS_DIR.exists():
        pytest.skip(
            f"SPECS_DIR absent at {SPECS_DIR}; structural guard cannot run. "
            "Vendor the corpus via scripts/sync_fidelity_corpus.py (Phase 8)."
        )

    spec_path = SPECS_DIR / f"{brand}_{category}.json"
    assert spec_path.exists(), (
        f"Spec file missing: {spec_path}. "
        "This (brand, category) was in _ASSERTION_PARAMS but the file is not on disk."
    )

    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    assertion: Optional[Dict] = None
    for a in raw.get("assertions", []) or []:
        if a.get("id") == assertion_id:
            assertion = a
            break

    assert assertion is not None, (
        f"Assertion {assertion_id!r} not found in {spec_path}. "
        "The assertion_id was derived from the same file at import time; "
        "this should not happen unless the corpus was modified between "
        "module load and test execution."
    )

    shape = _assertion_shape(assertion)
    expected_val: bool = bool(assertion.get("expected", True))

    if shape == _SHAPE_UNRECOGNIZED:
        # Complex DOM evaluator: string analysis cannot produce a meaningful
        # result. Verify conservative False is returned (never silently passes).
        # These 6 assertions (avatars-photo-stripped) require real browser DOM.
        result = evaluate_assertion_against_live_html(
            assertion, "<html><body></body></html>",
        )
        assert result is False, (
            f"{brand}__{category}__{assertion_id}: unrecognized evaluator shape "
            "must return False (conservative). "
            f"Got {result!r}. Evaluator requires browser DOM execution."
        )
        return

    positive_html, negative_html = _build_synthetic_html(assertion, shape)

    result_positive = evaluate_assertion_against_live_html(assertion, positive_html)
    assert result_positive == expected_val, (
        f"{brand}__{category}__{assertion_id}: "
        f"expected evaluator to return {expected_val!r} for positive HTML "
        f"(shape={shape!r}), got {result_positive!r}. "
        f"Assertion: {assertion!r}"
    )

    result_negative = evaluate_assertion_against_live_html(assertion, negative_html)
    assert result_negative != expected_val, (
        f"{brand}__{category}__{assertion_id}: "
        f"expected evaluator to return {not expected_val!r} for negative HTML "
        f"(shape={shape!r}), got {result_negative!r}. "
        f"Assertion: {assertion!r}"
    )


# ---------------------------------------------------------------------------
# Phase 11.1 GREEN guard: no-leak parse completeness
# Introduced as GREEN (all 17 current corpus assertions use exact spacing).
# This is a regression guard: if a future spec uses a whitespace variant that
# the old strict parser cannot handle, this test catches the silent gap before
# it reaches production.
# ---------------------------------------------------------------------------


def test_every_no_leak_assertion_parses_to_nonempty_tokens() -> None:
    """Every corpus no-wordmark-logo-leak assertion must parse to non-empty tokens.

    Guards against a future spec introducing a whitespace variant in
    'const forbidden = [...]' that the parser cannot handle, creating a
    silent trademark gap (the parser returns [] -> conservative False ->
    the no-leak check never actually verifies the HTML).

    Phase 11.1 status: GREEN at introduction (2026-06-14). All 17 no-leak
    assertions in the current corpus use exact spacing and parse correctly.
    This is a regression guard, not a TDD-RED test - see Phase 11 PRD for
    the whitespace-brittleness diagnosis and the parser hardening in Phase 11.2.

    Self-skips when SPECS_DIR is absent (should not occur post-Phase-8).
    """
    if not SPECS_DIR.exists():
        pytest.skip(
            f"SPECS_DIR absent at {SPECS_DIR}; "
            "no-leak parse guard cannot run without vendored corpus."
        )

    no_leak_found = 0
    problems: List[str] = []

    for spec_file in sorted(SPECS_DIR.glob("*.json")):
        try:
            spec = json.loads(spec_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for assertion in spec.get("assertions", []) or []:
            aid = assertion.get("id", "")
            evaluator = assertion.get("evaluate", "")
            if not isinstance(evaluator, str):
                continue
            if "forbidden.every" not in evaluator:
                continue
            no_leak_found += 1
            tokens = forbidden_tokens_from_evaluator(evaluator)
            if not tokens:
                problems.append(
                    f"{spec_file.stem}::{aid}: "
                    f"forbidden_tokens_from_evaluator returned [] "
                    f"(evaluator snippet: {evaluator[evaluator.find('const forbidden'):evaluator.find('const forbidden')+60]!r})"
                )

    assert no_leak_found > 0, (
        "No forbidden.every assertions found in any vendored spec. "
        "The corpus should contain 17 no-wordmark-logo-leak assertions. "
        "Check that SPECS_DIR is populated correctly."
    )
    assert not problems, (
        f"The following no-leak assertions parsed to empty token lists "
        f"(silent trademark gap):\n" + "\n".join(problems)
    )
