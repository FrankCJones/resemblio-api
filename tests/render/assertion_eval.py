"""Assertion evaluator for Resemblio fidelity-spec assertions.

Evaluates whether one fidelity-spec assertion holds against a blob of live HTML.
All evaluation is case-insensitive substring search against the lowercased HTML.

Assertion kinds (fidelity_spec_v2):
  - text_content: expected_text is a substring of live_html.
  - evaluate (includes shape): a .includes('token') JS assertion; token must be
    present in the HTML for the observation to be True.
  - evaluate (forbidden.every shape): forbidden.every(s => !html.includes(s));
    all forbidden tokens must be ABSENT from the HTML for the observation to be True.
  - evaluate (unrecognized shape): conservative False; the evaluator requires
    browser DOM execution and cannot be approximated by string analysis.

Polarity: every assertion's ``expected`` field (default ``True``) is the desired
boolean result. The evaluator computes an ``observed`` boolean and returns
``observed == expected``.

Conservative on parse failure: returns ``False`` (never silently passes) when the
evaluator shape is unrecognized or a token cannot be parsed. This prevents silent
false-passes - e.g. the *-no-wordmark-logo-leak family, where returning ``True``
incorrectly would be a trademark-safety regression.

Pure - no network, no os.environ access in core logic.
Schema: assertion_eval_v1
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers moved from test_visual_fidelity_gate.py (Phase 10 refactor)
# Back-compat re-exports keep test_spec_coverage.py imports working.
# ---------------------------------------------------------------------------


def font_family_assertion_from_spec(
    spec_dir: pathlib.Path, brand: str, category: str,
) -> Optional[Dict[str, object]]:
    """Read the per-(brand, category) spec and return the first font assertion.

    "First" is the first assertion whose ``id`` lowercases to contain
    ``font`` or ``family``. Returns the raw assertion dict (the runner
    knows how to evaluate it). Returns None when the spec file is
    missing or contains no font assertion; callers treat that as "font
    dimension not checkable, do not penalize this tuple".

    Pure function (no os.environ / __file__ access in core logic) so it is
    unit-testable with an injected path.
    """
    spec_path = spec_dir / f"{brand}_{category}.json"
    if not spec_path.exists():
        return None
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for assertion in spec.get("assertions", []) or []:
        aid = (assertion.get("id") or "").lower()
        if "font" in aid or "family" in aid:
            return assertion
    return None


def expected_token_from_assertion(assertion: Dict[str, object]) -> Optional[str]:
    """Extract the font-family token an assertion checks for.

    Pure function (no os.environ / __file__ access) so it is unit-testable
    with synthetic assertion dicts. Returns the literal token string (NOT
    lowercased) that the assertion verifies, or None when the kind is
    unrecognized or the token cannot be parsed. The caller is responsible
    for case-folding when doing a case-insensitive HTML substring check.

    For ``text_content`` kind: returns ``assertion["expected_text"]``.
    For ``evaluate`` kind (JS evaluator): extracts the first ``.includes("...")``
    argument from the evaluator string. Both single and double-quoted forms are
    supported. Returns None when no ``.includes(`` marker is found or the
    quoted argument cannot be parsed.

    This is the single token-extraction code path shared by
    ``evaluate_font_family_against_live_html`` (the live-gate evaluator) and
    the Phase 9.2 parametrized structural guard (DRY + testability).

    Phase 9.2 refactor (2026-06-13): extracted from test_visual_fidelity_gate.py.
    Phase 10 refactor (2026-06-13): moved to assertion_eval.py; re-exported from
    test_visual_fidelity_gate.py for backward compatibility.
    """
    kind = assertion.get("kind")
    if kind == "text_content":
        text = assertion.get("expected_text")
        return str(text) if text is not None else None
    evaluator = assertion.get("evaluate")
    if isinstance(evaluator, str):
        marker = ".includes("
        idx = evaluator.find(marker)
        if idx == -1:
            return None
        tail = evaluator[idx + len(marker):]
        for quote in ('"', "'"):
            q_start = tail.find(quote)
            if q_start == -1:
                continue
            q_end = tail.find(quote, q_start + 1)
            if q_end == -1:
                continue
            token = tail[q_start + 1:q_end]
            if token:
                return token
        return None
    return None


def evaluate_font_family_against_live_html(
    assertion: Dict[str, object], live_html: str,
) -> bool:
    """Evaluate a font-family structural assertion against live HTML.

    The Phase-5 specs use two assertion kinds:

      - JavaScript evaluator ("evaluate" field): we cannot run a JS
        engine here; we approximate by extracting the font-family name
        the evaluator checks for (the substring inside ``includes(...)``)
        and checking it appears in the live HTML (case-insensitive). A
        case-insensitive substring of the rendered HTML is sufficient
        because the library page surfaces the free-alternative font
        name in the disclosure aside and in inline ``font-family``
        declarations on the rendered element.

      - text_content kind ("kind": "text_content", "expected_text"): we
        check the expected text appears in the live HTML.

    Token extraction is delegated to ``expected_token_from_assertion`` so
    there is exactly one parsing code path for both this evaluator and the
    Phase 9.2 parametrized structural guard (DRY + testability).

    Returns True when the assertion is satisfied. Conservative on parse
    failures: returns False rather than True.

    NOTE: this evaluator handles only the text_content and evaluate:includes
    shapes. For the evaluate:forbidden.every (no-wordmark-logo-leak) shape and
    unrecognized shapes, use ``evaluate_assertion_against_live_html`` instead,
    which dispatches correctly for all known kinds.

    Phase 10 refactor (2026-06-13): moved to assertion_eval.py; re-exported from
    test_visual_fidelity_gate.py for backward compatibility.
    """
    haystack = live_html.lower()
    token = expected_token_from_assertion(assertion)
    if token is None:
        return False
    return bool(token) and token.lower() in haystack


# ---------------------------------------------------------------------------
# Phase 10 additions: kind-aware, polarity-aware evaluator
# ---------------------------------------------------------------------------


def forbidden_tokens_from_evaluator(evaluator: str) -> List[str]:
    """Extract the forbidden token array from a forbidden.every evaluator string.

    Parses the pattern: ``const forbidden = ['tok1', 'tok2', ...]``.
    Tolerates arbitrary whitespace between 'const', 'forbidden', '=', and '[':
    compact forms ('const forbidden=[...]'), extra-space forms, and
    newline-before-bracket forms are all handled correctly. Supports both
    single-quoted and double-quoted token strings within the array.

    Returns an empty list on genuine parse failure (malformed or absent array)
    rather than raising, so callers can apply the conservative-False policy.
    An empty list is also returned when the array itself is empty ('const
    forbidden = []'); callers should treat [] as "no tokens to check" and
    apply conservative-False rather than silently passing.

    Unit-tested for: empty array, single token, multiple tokens, double-quoted
    tokens, mixed quote styles, malformed input, and whitespace variants
    (compact, extra-space, newline-before-bracket - Phase 11.2).

    Pure function; no network, no os.environ access.
    Phase 11.2: regex hardened from literal-space to whitespace-tolerant form.
    """
    m = re.search(r"const\s+forbidden\s*=\s*\[(.*?)\]", evaluator, re.DOTALL)
    if not m:
        return []
    array_content = m.group(1)
    # Match both 'token' and "token" quoted forms within the array content.
    matches = re.findall(r"'([^']*)'|\"([^\"]*)\"", array_content)
    # Each match is (single_quoted_capture, double_quoted_capture); one is empty.
    return [sq if sq else dq for sq, dq in matches if sq or dq]


def evaluate_assertion_against_live_html(
    assertion: Dict[str, object], live_html: str,
) -> bool:
    """Evaluate one fidelity-spec assertion against a blob of live HTML.

    Semantics are case-insensitive: ``live_html`` is lowercased once before
    dispatch so callers do not need to pre-fold. The ``expected`` field
    (default ``True`` when absent) is the desired boolean outcome. Returns
    ``observed == expected``.

    Dispatch by kind and evaluator shape:

    1. ``text_content`` kind:
       ``observed = expected_text.lower() in haystack``

    2. ``evaluate`` with ``forbidden.every(s => !html.includes(s))`` shape:
       Extract the forbidden token array via ``forbidden_tokens_from_evaluator``.
       ``observed = all(tok.lower() not in haystack for tok in forbidden_tokens)``
       Passes (True) when NO forbidden token appears; fails (False) when any does.

    3. ``evaluate`` with a single positive ``.includes('x')`` shape:
       ``observed = token.lower() in haystack``
       (Delegates to ``expected_token_from_assertion``.)

    4. Unrecognized evaluator shape: return ``False`` (conservative; never silently
       passes). Documented as the safe fallback for shapes that require real browser
       DOM execution (e.g. ``querySelectorAll``-based assertions).

    Returns ``False`` when a token or array cannot be parsed (parse failure is
    treated as conservative non-pass, not an error).

    Pure: no network, no os.environ access in core logic.
    """
    haystack = live_html.lower()
    expected: bool = bool(assertion.get("expected", True))
    kind = assertion.get("kind")

    # Shape 1: text_content
    if kind == "text_content":
        expected_text = assertion.get("expected_text")
        if not isinstance(expected_text, str):
            return False  # conservative: missing or wrong-type expected_text
        observed = expected_text.lower() in haystack
        return observed == expected

    evaluator = assertion.get("evaluate")
    if not isinstance(evaluator, str):
        return False  # conservative: no evaluator or wrong type

    # Shape 2: forbidden.every (no-wordmark-logo-leak family)
    if "forbidden.every" in evaluator:
        tokens = forbidden_tokens_from_evaluator(evaluator)
        if not tokens:
            return False  # conservative: could not parse the forbidden array
        observed = all(tok.lower() not in haystack for tok in tokens)
        return observed == expected

    # Shape 3: single positive .includes('token') (font-family / brand-name families)
    if ".includes(" in evaluator:
        token = expected_token_from_assertion(assertion)
        if token is None:
            return False  # conservative: could not extract the token
        observed = token.lower() in haystack
        return observed == expected

    # Shape 4: unrecognized evaluator (e.g. querySelectorAll DOM queries).
    # Requires real browser execution; string analysis cannot produce a
    # meaningful result. Return False conservatively rather than guessing.
    return False


# ---------------------------------------------------------------------------
# Phase 11 additions: batch sweep helper and result type
# ---------------------------------------------------------------------------

#: Substring present in the id of every no-wordmark-logo-leak assertion.
#: Used to classify the no-leak family without hardcoding brand names.
NO_LEAK_ID_MARKER = "no-wordmark-logo-leak"


@dataclass
class AssertionSweepResult:
    """Result of evaluating all string-evaluable fidelity-spec assertions.

    ``passed``          - assertion ids that PASS (observed == expected).
    ``failed``          - assertion ids that FAIL (observed != expected).
    ``browser_required``- assertion ids whose evaluator is an unrecognized
                          shape (querySelectorAll / getComputedStyle) that
                          requires real browser DOM execution. evaluate_
                          assertion_against_live_html returns conservative
                          False for these; they are NOT counted as failed -
                          they are deferred to Phase 12, where a
                          page.evaluate() path will be added to
                          capture_live_render. Surfaced here so the caller
                          can make the gap VISIBLE in the report instead of
                          silently ignoring it.
    ``wordmark_leak``   - True when any assertion whose id contains
                          NO_LEAK_ID_MARKER is in ``failed``. This is the
                          trademark-safety signal: the live render leaks a
                          brand logo or wordmark. A live-gate tuple with
                          wordmark_leak=True is a HARD FAIL regardless of
                          color or font verdicts.
    ``schema_version``  - "assertion_sweep_v1".

    Pure: no network, no os.environ access.
    Schema: assertion_sweep_v1
    """

    passed: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    browser_required: List[str] = field(default_factory=list)
    wordmark_leak: bool = False
    schema_version: str = "assertion_sweep_v1"


def evaluate_all_assertions_against_live_html(
    assertions: List[Dict],
    live_html: str,
) -> AssertionSweepResult:
    """Evaluate all fidelity-spec assertions against a blob of live HTML.

    Iterates every assertion in the provided list and dispatches each to
    ``evaluate_assertion_against_live_html``. Classifies results into three
    buckets:

    ``passed``           - ids where ``evaluate_assertion_against_live_html``
                           returned True (observed == expected).
    ``failed``           - ids where it returned False AND the assertion is NOT
                           of an unrecognized evaluator shape. For the
                           forbidden.every (no-leak) family, a False here means
                           a forbidden token was found in the HTML - a trademark
                           violation.
    ``browser_required`` - ids whose evaluator is an unrecognized shape
                           (querySelectorAll / getComputedStyle without
                           .includes). ``evaluate_assertion_against_live_html``
                           conservatively returns False for these, but they are
                           NOT classified as failures: they require real browser
                           DOM execution via page.evaluate() (deferred to Phase
                           12). Callers surface these in the report for
                           visibility; they do NOT gate the tuple verdict in
                           Phase 11.

    ``wordmark_leak``    - True when any id in ``failed`` contains
                           ``NO_LEAK_ID_MARKER`` ("no-wordmark-logo-leak").
                           This is the trademark-safety signal. A live-gate
                           tuple with wordmark_leak=True is a HARD FAIL
                           regardless of color or font verdicts.

    Unrecognized-shape detection mirrors the logic in test_assertion_coverage:
    an evaluator string that lacks both ".includes(" and "forbidden.every",
    and whose assertion kind is not "text_content", is treated as
    browser-required.

    Conservative on every parse failure: relies on
    ``evaluate_assertion_against_live_html``'s own conservative-False policy;
    only classifies as browser_required when the evaluator shape is genuinely
    unrecognized.

    Pure: no network, no os.environ access in core logic.
    Schema: assertion_sweep_v1
    """
    passed: List[str] = []
    failed: List[str] = []
    browser_required: List[str] = []

    for assertion in assertions:
        aid: str = assertion.get("id") or ""
        kind = assertion.get("kind")
        evaluator = assertion.get("evaluate")

        # Determine whether this assertion needs real browser DOM execution.
        # Mirrors _assertion_shape() in test_assertion_coverage.py.
        is_browser_required = False
        if kind != "text_content":
            ev_str = evaluator if isinstance(evaluator, str) else ""
            if ev_str and "forbidden.every" not in ev_str and ".includes(" not in ev_str:
                is_browser_required = True

        if is_browser_required:
            # Unrecognized shape: deferred to Phase 12. Do NOT count as failed.
            browser_required.append(aid)
            continue

        result = evaluate_assertion_against_live_html(assertion, live_html)
        if result:
            passed.append(aid)
        else:
            failed.append(aid)

    wordmark_leak = any(NO_LEAK_ID_MARKER in aid for aid in failed)

    return AssertionSweepResult(
        passed=passed,
        failed=failed,
        browser_required=browser_required,
        wordmark_leak=wordmark_leak,
    )
