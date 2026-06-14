"""Unit tests for the assertion evaluator (assertion_eval.py).

Phase 10.1 RED: test_no_wordmark_assertion_evaluates_correctly pinned the
inversion bug - the existing evaluate_font_family_against_live_html returned
False (conservative) for clean HTML where the no-wordmark-logo-leak assertion
SHOULD pass (return True). assert True failed -> RED.

Phase 10.2 GREEN: switched to evaluate_assertion_against_live_html (assertion_eval.py)
which handles the forbidden.every shape correctly. Unit tests for
forbidden_tokens_from_evaluator and evaluate_assertion_against_live_html added.

Phase 11.1 RED: test_forbidden_tokens_tolerates_whitespace_variants pins the
parser-robustness gap (compact/extra-space/newline forms return [] with the
strict regex). RED because the literal pattern 'const forbidden = [' does not
tolerate whitespace variants.

Phase 11.2 GREEN: hardened regex + AssertionSweepResult + sweep helper.

Schema: phase11_assertion_eval_tests_v1
"""
from __future__ import annotations

import pytest

from .assertion_eval import (
    AssertionSweepResult,
    evaluate_all_assertions_against_live_html,
    evaluate_assertion_against_live_html,
    evaluate_font_family_against_live_html,
    expected_token_from_assertion,
    forbidden_tokens_from_evaluator,
    NO_LEAK_ID_MARKER,
)

# ---------------------------------------------------------------------------
# Shared fixture assertions (real corpus copies for regression pins)
# ---------------------------------------------------------------------------

_AEON_NO_LEAK_ASSERTION = {
    "id": "aeon-about-team-no-wordmark-logo-leak",
    "evaluate": (
        "(() => { const html = document.documentElement.outerHTML.toLowerCase();"
        " const forbidden = ['aeon.co/logo', 'aeon-logo', 'aeon-wordmark',"
        " '/logo.svg', '/wordmark.svg', '/brand/logo'];"
        " return forbidden.every(s => !html.includes(s)); })()"
    ),
    "expected": True,
}

_LINEAR_FONT_ASSERTION = {
    "id": "linear-disclosure-aside-names-brand-font",
    "kind": "text_content",
    "expected_text": "Linear uses Inter.",
    "expected": True,
}

_AEON_FONT_ASSERTION = {
    "id": "aeon-alphabet-display-uses-free-alt",
    "evaluate": (
        "(() => { const el = document.querySelector('.a-h1');"
        " if (!el) return false;"
        " const fam = getComputedStyle(el).fontFamily;"
        " return fam.toLowerCase().includes('plus jakarta sans'); })()"
    ),
    "expected": True,
}


# ---------------------------------------------------------------------------
# Phase 10.1 RED -> Phase 10.2 GREEN: inversion bug pin
# ---------------------------------------------------------------------------


def test_no_wordmark_assertion_evaluates_correctly() -> None:
    """Pin: no-wordmark-logo-leak assertion evaluates correctly (not inverted).

    The no-wordmark-logo-leak assertion passes when NO forbidden token appears
    in the HTML (expected=True, observed=True -> True == True -> return True).
    It fails when a forbidden token IS present.

    Phase 10.1 RED: the existing evaluate_font_family_against_live_html could
    not parse the forbidden.every shape and returned False (conservative) for
    clean HTML. assert True failed -> RED.

    Phase 10.2 GREEN: evaluate_assertion_against_live_html handles the shape:
    - Clean HTML (no forbidden tokens) -> observed=True, expected=True -> True.
    - Leaking HTML (forbidden token present) -> observed=False, expected=True -> False.
    """
    clean_html = "<html><body><p>Aeon is a magazine about ideas.</p></body></html>"
    leaking_html = (
        "<html><body>"
        "<img src='https://aeon.co/logo.png'>"
        "</body></html>"
    )

    # Phase 10.2 GREEN: use evaluate_assertion_against_live_html.
    result_clean = evaluate_assertion_against_live_html(
        _AEON_NO_LEAK_ASSERTION, clean_html,
    )
    assert result_clean is True, (
        "No-wordmark-logo-leak assertion must return True for clean HTML "
        f"(no forbidden tokens = assertion passes). Got {result_clean!r}."
    )

    result_leaking = evaluate_assertion_against_live_html(
        _AEON_NO_LEAK_ASSERTION, leaking_html,
    )
    assert result_leaking is False, (
        "No-wordmark-logo-leak assertion must return False for leaking HTML "
        f"(forbidden token present = assertion fails). Got {result_leaking!r}."
    )


# ---------------------------------------------------------------------------
# Unit tests for forbidden_tokens_from_evaluator (pure)
# ---------------------------------------------------------------------------


def test_forbidden_tokens_empty_array() -> None:
    """An empty forbidden array yields an empty list."""
    evaluator = (
        "(() => { const forbidden = [];"
        " return forbidden.every(s => !html.includes(s)); })()"
    )
    assert forbidden_tokens_from_evaluator(evaluator) == []


def test_forbidden_tokens_single_token_single_quote() -> None:
    """A single-quoted token is extracted correctly."""
    evaluator = (
        "(() => { const forbidden = ['brand.com/logo'];"
        " return forbidden.every(s => !html.includes(s)); })()"
    )
    assert forbidden_tokens_from_evaluator(evaluator) == ["brand.com/logo"]


def test_forbidden_tokens_single_token_double_quote() -> None:
    """A double-quoted token is extracted correctly."""
    evaluator = (
        '(() => { const forbidden = ["brand.com/logo"];'
        " return forbidden.every(s => !html.includes(s)); })()"
    )
    assert forbidden_tokens_from_evaluator(evaluator) == ["brand.com/logo"]


def test_forbidden_tokens_multiple_tokens() -> None:
    """Multiple tokens across both quote styles are all extracted."""
    evaluator = (
        "(() => { const forbidden = ['aeon.co/logo', 'aeon-logo',"
        " 'aeon-wordmark', '/logo.svg', '/wordmark.svg', '/brand/logo'];"
        " return forbidden.every(s => !html.includes(s)); })()"
    )
    assert forbidden_tokens_from_evaluator(evaluator) == [
        "aeon.co/logo",
        "aeon-logo",
        "aeon-wordmark",
        "/logo.svg",
        "/wordmark.svg",
        "/brand/logo",
    ]


def test_forbidden_tokens_malformed_no_array() -> None:
    """An evaluator with no const forbidden = [...] pattern returns []."""
    evaluator = "(() => { return true; })()"
    assert forbidden_tokens_from_evaluator(evaluator) == []


def test_forbidden_tokens_from_real_aeon_no_leak_assertion() -> None:
    """Regression pin: real aeon no-leak assertion yields 6 expected tokens."""
    evaluator = _AEON_NO_LEAK_ASSERTION["evaluate"]
    assert isinstance(evaluator, str)
    tokens = forbidden_tokens_from_evaluator(evaluator)
    assert tokens == [
        "aeon.co/logo",
        "aeon-logo",
        "aeon-wordmark",
        "/logo.svg",
        "/wordmark.svg",
        "/brand/logo",
    ]


# ---------------------------------------------------------------------------
# Unit tests for evaluate_assertion_against_live_html (pure)
# ---------------------------------------------------------------------------


def test_evaluate_text_content_positive() -> None:
    """text_content assertion: returns True when expected_text is in live HTML."""
    html = "<aside>Linear uses Inter. Rendered here with Inter.</aside>"
    assert evaluate_assertion_against_live_html(_LINEAR_FONT_ASSERTION, html) is True


def test_evaluate_text_content_negative() -> None:
    """text_content assertion: returns False when expected_text is absent."""
    html = "<aside>Aeon uses Helvetica Neue.</aside>"
    assert evaluate_assertion_against_live_html(_LINEAR_FONT_ASSERTION, html) is False


def test_evaluate_text_content_case_insensitive() -> None:
    """text_content matching is case-insensitive against live_html."""
    html = "<aside>LINEAR USES INTER. Rendered here.</aside>"
    assert evaluate_assertion_against_live_html(_LINEAR_FONT_ASSERTION, html) is True


def test_evaluate_text_content_missing_expected_text_returns_false() -> None:
    """text_content with no expected_text key returns False (conservative)."""
    assertion: dict = {"id": "x", "kind": "text_content"}  # missing expected_text
    assert evaluate_assertion_against_live_html(assertion, "<html>test</html>") is False


def test_evaluate_includes_positive() -> None:
    """evaluate .includes shape: True when token is present in live HTML."""
    html = (
        "<aside>Aeon uses Plus Jakarta Sans. Rendered here with Plus Jakarta Sans.</aside>"
    )
    assert evaluate_assertion_against_live_html(_AEON_FONT_ASSERTION, html) is True


def test_evaluate_includes_negative() -> None:
    """evaluate .includes shape: False when token is absent from live HTML."""
    html = "<aside>Aeon uses Times New Roman.</aside>"
    assert evaluate_assertion_against_live_html(_AEON_FONT_ASSERTION, html) is False


def test_evaluate_forbidden_every_clean_html_returns_true() -> None:
    """forbidden.every assertion: True when NO forbidden token is in the HTML."""
    clean_html = "<html><body><p>Clean content with no brand assets.</p></body></html>"
    assert evaluate_assertion_against_live_html(_AEON_NO_LEAK_ASSERTION, clean_html) is True


def test_evaluate_forbidden_every_leaking_html_returns_false() -> None:
    """forbidden.every assertion: False when a forbidden token IS in the HTML."""
    leaking_html = "<html><body><img src='/brand/logo.png'></body></html>"
    assert (
        evaluate_assertion_against_live_html(_AEON_NO_LEAK_ASSERTION, leaking_html)
        is False
    )


def test_evaluate_forbidden_every_each_token_triggers_false() -> None:
    """Each individual forbidden token causes the assertion to fail."""
    evaluator = _AEON_NO_LEAK_ASSERTION["evaluate"]
    assert isinstance(evaluator, str)
    tokens = forbidden_tokens_from_evaluator(evaluator)
    for tok in tokens:
        html_with_token = f"<html><body><img src='{tok}'></body></html>"
        result = evaluate_assertion_against_live_html(
            _AEON_NO_LEAK_ASSERTION, html_with_token,
        )
        assert result is False, (
            f"Expected False when forbidden token {tok!r} is present. Got {result!r}."
        )


def test_evaluate_unrecognized_shape_returns_false() -> None:
    """Unrecognized evaluator shape (complex DOM query) returns False (conservative)."""
    dom_query_assertion = {
        "id": "aeon-about-team-avatars-photo-stripped",
        "evaluate": (
            "(() => { const members = document.querySelectorAll('.at__member');"
            " if (members.length === 0) return false;"
            " for (const m of members) { if (m.querySelector('img')) return false; }"
            " return true; })()"
        ),
        "expected": True,
    }
    any_html = "<html><body><div class='at__member'></div></body></html>"
    assert evaluate_assertion_against_live_html(dom_query_assertion, any_html) is False


def test_evaluate_no_evaluator_field_returns_false() -> None:
    """An assertion with neither kind nor evaluate returns False (conservative)."""
    assertion: dict = {"id": "x-mystery-check", "expected": True}
    assert evaluate_assertion_against_live_html(assertion, "<html></html>") is False


def test_evaluate_expected_polarity_false() -> None:
    """expected=False polarity: assertion passes when observed is False.

    If any corpus assertion had expected=False, the evaluator must honor it.
    Verified here with a synthetic case so the polarity path is always tested
    even when the current corpus has no expected=False assertions.
    """
    # synthetic: expected=False means the assertion PASSES when the token is ABSENT
    assertion: dict = {
        "id": "x-font-must-not-include-comic-sans",
        "evaluate": (
            "(() => { const fam = getComputedStyle(el).fontFamily;"
            " return fam.toLowerCase().includes('comic sans'); })()"
        ),
        "expected": False,
    }
    # HTML without the token: observed=False, expected=False -> False==False -> True (PASS)
    clean_html = "<html><body><p>Uses Inter.</p></body></html>"
    assert evaluate_assertion_against_live_html(assertion, clean_html) is True

    # HTML with the token: observed=True, expected=False -> True==False -> False (FAIL)
    bad_html = "<html><body><p>Font: Comic Sans MS.</p></body></html>"
    assert evaluate_assertion_against_live_html(assertion, bad_html) is False


# ---------------------------------------------------------------------------
# Backward compatibility: existing evaluate_font_family_against_live_html
# still works correctly for the text_content and includes shapes it handled.
# ---------------------------------------------------------------------------


def test_legacy_evaluator_still_works_for_font_assertions() -> None:
    """evaluate_font_family_against_live_html remains correct after refactor.

    Regression pin: the Phase 9.2 single-token extraction still routes
    correctly through the re-exported function in assertion_eval.py.
    """
    tc = {
        "id": "x-disclosure-aside-names-brand-font",
        "kind": "text_content",
        "expected_text": "Inter",
    }
    assert evaluate_font_family_against_live_html(
        tc, "<aside>Rendered with Inter font.</aside>",
    )
    assert not evaluate_font_family_against_live_html(
        tc, "<aside>Rendered with Helvetica font.</aside>",
    )

    ev = {
        "id": "x-font-family-uses-free-alt",
        "evaluate": (
            "(() => { const fam = getComputedStyle(el).fontFamily;"
            " return fam.toLowerCase().includes('plus jakarta sans'); })()"
        ),
    }
    assert evaluate_font_family_against_live_html(
        ev,
        "<style>body { font-family: 'Plus Jakarta Sans', sans-serif; }</style>",
    )
    assert not evaluate_font_family_against_live_html(
        ev,
        "<style>body { font-family: Times New Roman, serif; }</style>",
    )


def test_expected_token_from_assertion_text_content() -> None:
    """expected_token_from_assertion still resolves text_content correctly."""
    assertion = {
        "id": "x",
        "kind": "text_content",
        "expected_text": "PP Right Grotesk Wide",
    }
    assert expected_token_from_assertion(assertion) == "PP Right Grotesk Wide"


def test_expected_token_from_assertion_evaluate_includes() -> None:
    """expected_token_from_assertion extracts the token from .includes(...)."""
    assertion = {
        "id": "x",
        "evaluate": "(() => { return fam.toLowerCase().includes('inter'); })()",
    }
    assert expected_token_from_assertion(assertion) == "inter"


# ---------------------------------------------------------------------------
# Phase 11.1 RED: parser whitespace robustness pin
# ---------------------------------------------------------------------------


def test_forbidden_tokens_tolerates_whitespace_variants() -> None:
    """Parser must tolerate whitespace variants around 'const forbidden = [...]'.

    Phase 11.1 RED: the current regex 'const forbidden = [' (verbatim) uses a
    literal space before and after '=' and before '['. Compact forms, extra-space
    forms, and newline-before-bracket forms all return [] with the strict regex.
    These are valid JS whitespace variants that a spec author (or future code
    generator) could produce, creating a silent trademark gap.

    Phase 11.2 GREEN: regex hardened to r"const\\s+forbidden\\s*=\\s*[(.*?)]".
    All three variants yield the expected non-empty token list.
    """
    # Compact: no spaces around = or before [
    ev_compact = (
        "(() => { const forbidden=['brand.com/logo', 'brand-logo'];"
        " return forbidden.every(s => !html.includes(s)); })()"
    )
    assert forbidden_tokens_from_evaluator(ev_compact) == [
        "brand.com/logo",
        "brand-logo",
    ], (
        "Compact form 'const forbidden=[...]' must parse to expected tokens; "
        "got [] - parser is too strict on whitespace."
    )

    # Extra spaces around = and before [
    ev_extra = (
        "(() => { const  forbidden  =  ['brand.com/logo', 'brand-logo'];"
        " return forbidden.every(s => !html.includes(s)); })()"
    )
    assert forbidden_tokens_from_evaluator(ev_extra) == [
        "brand.com/logo",
        "brand-logo",
    ], (
        "Extra-space form 'const  forbidden  =  [...]' must parse; "
        "got [] - parser does not tolerate extra whitespace."
    )

    # Newline before the opening bracket
    ev_newline = (
        "(() => { const forbidden =\n['brand.com/logo', 'brand-logo'];"
        " return forbidden.every(s => !html.includes(s)); })()"
    )
    assert forbidden_tokens_from_evaluator(ev_newline) == [
        "brand.com/logo",
        "brand-logo",
    ], (
        "Newline-before-bracket form 'const forbidden =\\n[...]' must parse; "
        "got [] - parser does not tolerate newline before '['."
    )


# ---------------------------------------------------------------------------
# Phase 11.1 RED -> Phase 11.2 GREEN: AssertionSweepResult unit tests
# ---------------------------------------------------------------------------


def test_sweep_clean_html_no_wordmark_leak() -> None:
    """Sweep of no-leak assertion against clean HTML yields wordmark_leak=False."""
    assertions = [_AEON_NO_LEAK_ASSERTION]
    clean_html = "<html><body><p>Aeon is a magazine about ideas.</p></body></html>"
    result = evaluate_all_assertions_against_live_html(assertions, clean_html)
    assert result.wordmark_leak is False, (
        f"Clean HTML should not trigger wordmark_leak; got {result.wordmark_leak!r}"
    )
    assert isinstance(result, AssertionSweepResult)
    assert result.schema_version == "assertion_sweep_v1"


def test_sweep_leaking_html_sets_wordmark_leak() -> None:
    """Sweep of no-leak assertion against leaking HTML yields wordmark_leak=True.

    Phase 11.1 RED: the stub always returns wordmark_leak=False, so this
    assertion fails. Phase 11.2 GREEN: real implementation detects the leak.
    """
    assertions = [_AEON_NO_LEAK_ASSERTION]
    leaking_html = (
        "<html><body><img src='https://aeon.co/logo.png'></body></html>"
    )
    result = evaluate_all_assertions_against_live_html(assertions, leaking_html)
    assert result.wordmark_leak is True, (
        "Leaking HTML must set wordmark_leak=True on the sweep result; "
        f"got {result.wordmark_leak!r}. "
        "Phase 11.1 RED: stub always returns False."
    )
    assert _AEON_NO_LEAK_ASSERTION["id"] in result.failed, (
        f"The no-leak assertion id must appear in result.failed; "
        f"got {result.failed!r}"
    )


def test_sweep_unrecognized_shape_goes_to_browser_required() -> None:
    """Unrecognized evaluator (querySelectorAll) goes to browser_required, not failed.

    Phase 11.1 RED: stub returns empty lists. Phase 11.2 GREEN: the real
    implementation classifies the unrecognized shape as browser_required.
    """
    dom_assertion = {
        "id": "aeon-about-team-avatars-photo-stripped",
        "evaluate": (
            "(() => { const members = document.querySelectorAll('.at__member');"
            " if (members.length === 0) return false;"
            " for (const m of members) { if (m.querySelector('img')) return false; }"
            " return true; })()"
        ),
        "expected": True,
    }
    any_html = "<html><body><div class='at__member'></div></body></html>"
    result = evaluate_all_assertions_against_live_html([dom_assertion], any_html)
    assert dom_assertion["id"] in result.browser_required, (
        f"Unrecognized evaluator must land in browser_required; "
        f"got browser_required={result.browser_required!r}. "
        "Phase 11.1 RED: stub returns empty lists."
    )
    assert dom_assertion["id"] not in result.failed, (
        "Unrecognized evaluator must NOT be in failed (it is deferred to Phase 12, "
        f"not enforced in Phase 11). Got failed={result.failed!r}"
    )


def test_sweep_mixed_spec_correct_classification() -> None:
    """Mixed spec: clean no-leak passes, text_content passes, unrecognized deferred.

    Phase 11.1 RED: stub returns empty everything. Phase 11.2 GREEN: real impl.
    """
    text_assertion = {
        "id": "aeon-disclosure-aside-names-brand-font",
        "kind": "text_content",
        "expected_text": "PP Right Grotesk Wide",
        "expected": True,
    }
    dom_assertion = {
        "id": "aeon-about-team-avatars-photo-stripped",
        "evaluate": (
            "(() => { const members = document.querySelectorAll('.at__member');"
            " return true; })()"
        ),
        "expected": True,
    }
    html = (
        "<aside>Aeon uses PP Right Grotesk Wide.</aside>"
        "<p>Clean content, no brand assets.</p>"
    )
    result = evaluate_all_assertions_against_live_html(
        [_AEON_NO_LEAK_ASSERTION, text_assertion, dom_assertion],
        html,
    )
    assert result.wordmark_leak is False
    assert text_assertion["id"] in result.passed, (
        f"Passing text_content assertion must be in passed; got {result.passed!r}"
    )
    assert dom_assertion["id"] in result.browser_required, (
        f"DOM assertion must be in browser_required; "
        f"got {result.browser_required!r}"
    )
    assert NO_LEAK_ID_MARKER in NO_LEAK_ID_MARKER  # sanity: constant is importable
