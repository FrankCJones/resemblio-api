"""Extractor failure-mode classification (S15 ADR).

Resemblio's extractor returns a free-text error string. This module classifies
that string into a stable enum (`FailureCode`) so `POST /v1/extractions` can
surface a machine-actionable `error_code` to clients while preserving the raw
`error_log` for support visibility.

Classification is prefix-based against the extractor's stable string format
(see `projects/Resemblio/code/extractor/codex_extractor.py`). The contract:

    extractor return string                 -> FailureCode
    "invalid url: ..."                      -> INVALID_URL
    "unreachable: {...json...}"             -> UNREACHABLE
    "fetch failed: status=403 ua=..."       -> WAF_BLOCKED
    "fetch failed: status=429 ua=..."       -> WAF_BLOCKED
    "fetch failed: status=503 ua=..."       -> WAF_BLOCKED
    "fetch failed: status=0 ua=..."         -> TIMEOUT
    "fetch failed: status=4xx/5xx ua=..."   -> NETWORK_ERROR (other)
    "validation failed: ..."                -> NO_TOKENS_FOUND
        (JS-shell heuristic upgrade to JS_REQUIRED is applied upstream when
        the bridge has access to the fetched body length; not done here.)
    "model JSON parse failed: ..."          -> MODEL_ERROR
    "anthropic failed: ..."                 -> MODEL_ERROR
    "recon failed: ..."                     -> NETWORK_ERROR
    "postgres insert failed: ..."           -> PERSIST_ERROR
    "extractor returned no tokens"          -> NO_TOKENS_FOUND
    "Extractor unavailable on this host"    -> INTERNAL_ERROR
    "<anything else>"                       -> INTERNAL_ERROR

Per S15 ADR credit-handling rule, the API endpoint refunds credit only on the
Resemblio-attributable codes (`MODEL_ERROR`, `PERSIST_ERROR`, `INTERNAL_ERROR`).
URL-quality codes (`INVALID_URL`, `UNREACHABLE`, `WAF_BLOCKED`, `TIMEOUT`,
`JS_REQUIRED`, `NO_TOKENS_FOUND`, `NETWORK_ERROR`) consume credit.

Output safety: `redact_secrets` strips any `password=...` or `key=...` substring
from a free-text error before it lands in an HTTP response body (S15 ADR
defensive credential-strip rule).
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Final


class FailureCode(str, Enum):
    """Stable enum surfaced as `error_code` on extraction failure responses.

    Values are snake_case strings safe for JSON. The class is a `str` Enum so
    `FailureCode.WAF_BLOCKED == "waf_blocked"` evaluates True and json-encoding
    yields the string verbatim.
    """

    INVALID_URL = "invalid_url"
    UNREACHABLE = "unreachable"
    WAF_BLOCKED = "waf_blocked"
    TIMEOUT = "timeout"
    JS_REQUIRED = "js_required"
    NO_TOKENS_FOUND = "no_tokens_found"
    NETWORK_ERROR = "network_error"
    MODEL_ERROR = "model_error"
    PERSIST_ERROR = "persist_error"
    VALIDATION_ERROR = "validation_error"
    INTERNAL_ERROR = "internal_error"
    # S20: output-quality scoring classifies a structurally valid 200 response
    # whose composite quality score falls below the threshold as
    # `low_quality_output`. Resemblio-attributable (extractor produced
    # qualitatively unusable tokens) and therefore refundable. See
    # `app/quality_scoring.py` and Resemblio_BUILD_LOG.md S20 ADR.
    LOW_QUALITY_OUTPUT = "low_quality_output"


# HTTP status mapping per S15 ADR (Table: Recommended API error-code contract).
# Centralized here so the route handler does not carry magic numbers.
HTTP_STATUS_BY_CODE: Final[dict[FailureCode, int]] = {
    FailureCode.INVALID_URL: 422,
    FailureCode.UNREACHABLE: 502,
    FailureCode.WAF_BLOCKED: 502,
    FailureCode.TIMEOUT: 504,
    FailureCode.JS_REQUIRED: 422,
    FailureCode.NO_TOKENS_FOUND: 422,
    FailureCode.NETWORK_ERROR: 502,
    FailureCode.MODEL_ERROR: 502,
    FailureCode.PERSIST_ERROR: 500,
    FailureCode.VALIDATION_ERROR: 422,
    FailureCode.INTERNAL_ERROR: 500,
    # S20: low-quality output is surfaced as HTTP 200 because the request was
    # valid and tokens were produced; the response body is self-deprecating
    # via `status="low_quality"` and includes the auto-refund pointer.
    FailureCode.LOW_QUALITY_OUTPUT: 200,
}

# Per S15 ADR credit-handling rule: Resemblio-attributable failures refund.
REFUNDABLE_CODES: Final[frozenset[FailureCode]] = frozenset({
    FailureCode.MODEL_ERROR,
    FailureCode.PERSIST_ERROR,
    FailureCode.INTERNAL_ERROR,
    FailureCode.LOW_QUALITY_OUTPUT,
})

# Statuses that look like WAF mitigation when seen on the Chrome UA retry.
_WAF_STATUS_CODES: Final[frozenset[int]] = frozenset({401, 403, 405, 406, 429, 503})

# Regex captures the integer status from "fetch failed: status=NNN ua=..."
_FETCH_FAIL_STATUS_RE: Final[re.Pattern[str]] = re.compile(
    r"^fetch failed:\s*status=(-?\d+)"
)

# Defensive credential-strip patterns. `password=...` and `key=...` substrings
# in DSN or query-string form get masked. Matches stop at whitespace, `&`, `;`,
# or end-of-string, which covers libpq DSN, URL query, and JSON-string contexts
# without over-redacting structured data.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(password=)([^\s&;\"']+)", re.IGNORECASE),
    re.compile(r"(api[_-]?key=)([^\s&;\"']+)", re.IGNORECASE),
    re.compile(r"(secret=)([^\s&;\"']+)", re.IGNORECASE),
    # Bare `key=` last so the `api_key=` / `api-key=` rule wins first.
    re.compile(r"(?<![A-Za-z0-9_])(key=)([^\s&;\"']+)", re.IGNORECASE),
)
_REDACTED: Final[str] = "[REDACTED]"


def redact_secrets(message: str) -> str:
    """Mask credential-shaped substrings in a free-text error message.

    Returns the message with `password=...`, `key=...`, `api_key=...`, and
    `secret=...` values replaced by `[REDACTED]`. Idempotent; safe to call on
    an already-redacted string.
    """
    if not message:
        return message
    out = message
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}{_REDACTED}", out)
    return out


def classify_extractor_error(error: str) -> FailureCode:
    """Map a free-text extractor error to a `FailureCode`.

    Uses prefix matching against the stable strings emitted by
    `extractor.codex_extractor.CodexExtractor.extract`. Unknown shapes fall
    through to `INTERNAL_ERROR` so the response remains well-typed even when
    the extractor drifts.
    """
    if not error:
        return FailureCode.INTERNAL_ERROR
    text = error.strip()
    lower = text.lower()

    if lower.startswith("invalid url:"):
        return FailureCode.INVALID_URL
    if lower.startswith("unreachable:"):
        return FailureCode.UNREACHABLE
    if lower.startswith("fetch failed:"):
        match = _FETCH_FAIL_STATUS_RE.match(lower)
        if match:
            status = int(match.group(1))
            if status == 0:
                return FailureCode.TIMEOUT
            if status in _WAF_STATUS_CODES:
                return FailureCode.WAF_BLOCKED
            return FailureCode.NETWORK_ERROR
        return FailureCode.NETWORK_ERROR
    if lower.startswith("validation failed:"):
        return FailureCode.NO_TOKENS_FOUND
    if lower.startswith("model json parse failed:"):
        return FailureCode.MODEL_ERROR
    if lower.startswith("anthropic failed:"):
        return FailureCode.MODEL_ERROR
    if lower.startswith("recon failed:"):
        return FailureCode.NETWORK_ERROR
    if lower.startswith("postgres insert failed:"):
        return FailureCode.PERSIST_ERROR
    if "postgres insert failed:" in lower:
        # Chained form: "<earlier error>; postgres insert failed: ..."
        return FailureCode.PERSIST_ERROR
    if lower.startswith("extractor returned no tokens"):
        return FailureCode.NO_TOKENS_FOUND
    if lower.startswith("extractor unavailable on this host"):
        return FailureCode.INTERNAL_ERROR
    return FailureCode.INTERNAL_ERROR


def http_status_for(code: FailureCode) -> int:
    """Return the HTTP status code associated with a `FailureCode`."""
    return HTTP_STATUS_BY_CODE[code]


def is_refundable(code: FailureCode) -> bool:
    """Return True when the failure is Resemblio-attributable (refund credit)."""
    return code in REFUNDABLE_CODES
