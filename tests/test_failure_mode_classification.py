"""Tests for the extractor failure-mode classifier (S15 ADR).

Synthetic fixtures only; no network, no DB. Verifies:

1. Prefix-match coverage of every stable string the extractor emits today.
2. HTTP-status mapping per the S15 ADR table.
3. Refund eligibility honors the user-attributable vs Resemblio-attributable split.
4. Defensive credential-strip masks `password=`, `key=`, `api_key=`, `secret=`.
"""
from __future__ import annotations

import pytest

from app.failure_modes import (
    FailureCode,
    HTTP_STATUS_BY_CODE,
    REFUNDABLE_CODES,
    classify_extractor_error,
    http_status_for,
    is_refundable,
    redact_secrets,
)


# --- Classification ---------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("invalid url: missing scheme", FailureCode.INVALID_URL),
        ('unreachable: {"status_code": 0}', FailureCode.UNREACHABLE),
        ("fetch failed: status=403 ua=chrome", FailureCode.WAF_BLOCKED),
        ("fetch failed: status=429 ua=chrome", FailureCode.WAF_BLOCKED),
        ("fetch failed: status=503 ua=chrome", FailureCode.WAF_BLOCKED),
        ("fetch failed: status=401 ua=chrome", FailureCode.WAF_BLOCKED),
        ("fetch failed: status=0 ua=default", FailureCode.TIMEOUT),
        ("fetch failed: status=404 ua=chrome", FailureCode.NETWORK_ERROR),
        ("fetch failed: status=500 ua=default", FailureCode.NETWORK_ERROR),
        ("validation failed: missing required key 'accent'", FailureCode.NO_TOKENS_FOUND),
        ("model JSON parse failed: expecting value", FailureCode.MODEL_ERROR),
        ("anthropic failed: anthropic request failed after retries: 500", FailureCode.MODEL_ERROR),
        ("recon failed: boom", FailureCode.NETWORK_ERROR),
        ("postgres insert failed: connection refused", FailureCode.PERSIST_ERROR),
        (
            "validation failed: x; postgres insert failed: y",
            FailureCode.PERSIST_ERROR,
        ),
        ("extractor returned no tokens", FailureCode.NO_TOKENS_FOUND),
        (
            "Extractor unavailable on this host: No module named 'drl_adapter'",
            FailureCode.INTERNAL_ERROR,
        ),
        ("something completely unexpected", FailureCode.INTERNAL_ERROR),
        ("", FailureCode.INTERNAL_ERROR),
    ],
)
def test_classify_extractor_error(message: str, expected: FailureCode) -> None:
    """Each documented extractor prefix classifies into the right FailureCode."""
    assert classify_extractor_error(message) == expected


def test_classify_is_case_insensitive_on_prefix() -> None:
    """Classifier handles upper/lower variants the extractor may emit."""
    assert classify_extractor_error("Invalid URL: foo") == FailureCode.INVALID_URL
    assert (
        classify_extractor_error("Fetch Failed: status=403 ua=chrome")
        == FailureCode.WAF_BLOCKED
    )


# --- HTTP status & refund ---------------------------------------------------


def test_http_status_table_covers_every_code() -> None:
    """HTTP_STATUS_BY_CODE has an entry for every FailureCode value.

    Most codes are 4xx/5xx. S20's `LOW_QUALITY_OUTPUT` is the documented
    exception at 200 per ADR section 6 (request was valid; response is
    self-deprecating with auto-refund).
    """
    for code in FailureCode:
        assert code in HTTP_STATUS_BY_CODE
        status = HTTP_STATUS_BY_CODE[code]
        if code is FailureCode.LOW_QUALITY_OUTPUT:
            assert status == 200
        else:
            assert 400 <= status <= 599


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (FailureCode.INVALID_URL, 422),
        (FailureCode.UNREACHABLE, 502),
        (FailureCode.WAF_BLOCKED, 502),
        (FailureCode.TIMEOUT, 504),
        (FailureCode.JS_REQUIRED, 422),
        (FailureCode.NO_TOKENS_FOUND, 422),
        (FailureCode.NETWORK_ERROR, 502),
        (FailureCode.MODEL_ERROR, 502),
        (FailureCode.PERSIST_ERROR, 500),
        (FailureCode.VALIDATION_ERROR, 422),
        (FailureCode.INTERNAL_ERROR, 500),
    ],
)
def test_http_status_for_matches_adr(code: FailureCode, status: int) -> None:
    """Each code maps to the HTTP status named in the S15 ADR table."""
    assert http_status_for(code) == status


@pytest.mark.parametrize(
    "code",
    [FailureCode.MODEL_ERROR, FailureCode.PERSIST_ERROR, FailureCode.INTERNAL_ERROR],
)
def test_is_refundable_true_for_resemblio_attributable(code: FailureCode) -> None:
    """Resemblio-attributable codes refund per S15 ADR."""
    assert is_refundable(code) is True


@pytest.mark.parametrize(
    "code",
    [
        FailureCode.INVALID_URL,
        FailureCode.UNREACHABLE,
        FailureCode.WAF_BLOCKED,
        FailureCode.TIMEOUT,
        FailureCode.JS_REQUIRED,
        FailureCode.NO_TOKENS_FOUND,
        FailureCode.NETWORK_ERROR,
        FailureCode.VALIDATION_ERROR,
    ],
)
def test_is_refundable_false_for_user_attributable(code: FailureCode) -> None:
    """User-attributable codes do NOT refund per S15 ADR."""
    assert is_refundable(code) is False


def test_refundable_set_matches_adr() -> None:
    """Exactly the documented Resemblio-attributable codes are refundable.

    S15 ADR named MODEL_ERROR + PERSIST_ERROR + INTERNAL_ERROR. S20 ADR
    (Resemblio_BUILD_LOG.md, 2026-05-26) added LOW_QUALITY_OUTPUT as a fourth
    Resemblio-attributable code; the customer gave us a valid URL and we
    delivered qualitatively unusable tokens, so the credit refunds.
    """
    assert REFUNDABLE_CODES == frozenset(
        {
            FailureCode.MODEL_ERROR,
            FailureCode.PERSIST_ERROR,
            FailureCode.INTERNAL_ERROR,
            FailureCode.LOW_QUALITY_OUTPUT,
        }
    )


# --- Credential redaction ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        ("postgres insert failed: host=db password=hunter2 dbname=resemblio", "hunter2"),
        ("anthropic failed: request 401, api_key=sk-abc123", "sk-abc123"),
        ("storage failed: key=AKIAEXAMPLE&secret=topsecret", "topsecret"),
        ("DSN: postgres://u:pw@h/d?password=letmein&sslmode=require", "letmein"),
        ("auth: Secret=verysecret", "verysecret"),
    ],
)
def test_redact_secrets_masks_credential_substrings(raw: str, needle: str) -> None:
    """Credential-shaped substrings get masked; the raw secret never leaks."""
    out = redact_secrets(raw)
    assert needle not in out
    assert "[REDACTED]" in out


def test_redact_secrets_idempotent() -> None:
    """Re-redacting already-redacted output does not double-mask."""
    once = redact_secrets("password=abc and key=def")
    twice = redact_secrets(once)
    assert once == twice


def test_redact_secrets_preserves_non_secret_content() -> None:
    """Redaction does not touch unrelated text."""
    assert redact_secrets("fetch failed: status=403 ua=chrome") == (
        "fetch failed: status=403 ua=chrome"
    )


def test_redact_secrets_empty_string() -> None:
    """Empty input returns empty output."""
    assert redact_secrets("") == ""
