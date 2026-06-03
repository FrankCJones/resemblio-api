"""URL site-class classifier on first-byte HTTP response.

Stage O3 of the URL-first onboarding respec (2026-06-03). Given a URL,
fetch the first ~16 KB of the response and label it as one of:

  - ``html_first``    SUPPORTED  (extraction-quality floor 0.60)
  - ``js_rendered``   SUPPORTED  (extraction-quality floor 0.40)
  - ``wix_class``     OUT-OF-SCOPE (auto-refund / notify-when-supported)
  - ``waf_blocked``   OUT-OF-SCOPE (auto-refund)
  - ``unknown``       OUT-OF-SCOPE (auto-refund + flag for tuning)

This module replaces the Stage-O1 stub that always returned ``html_first``.
The public interface is preserved verbatim for the existing call site in
``app.routes.extractions_anonymous``:

  - ``ClassificationResult`` dataclass with ``label``, ``confidence``,
    ``schema_version`` (existing fields) plus the new diagnostic fields
    ``signals_matched``, ``http_status``, ``response_headers``,
    ``body_excerpt`` (all optional with safe defaults so older imports
    keep working).
  - ``classify_url(url: str) -> ClassificationResult``.
  - ``is_supported(label: str) -> bool``.

Design contract
===============

* **Pure I/O at one seam.** The single network call lives in
  ``_fetch_first_bytes`` which accepts an injected ``httpx.Client``.
  Tests drive the function with ``httpx.MockTransport``; no live HTTP
  in unit tests.
* **Retry with backoff.** Three attempts total (initial + two retries)
  with exponential delays (``RETRY_DELAYS_SEC``). Retries fire on
  transport errors and 5xx responses only; 4xx responses are real
  classification signals (a 403 from Cloudflare is ``waf_blocked``,
  not a retryable failure).
* **Redirect handling.** Up to ``MAX_REDIRECTS`` hops followed by
  ``httpx.Client(follow_redirects=True)``. The final response is what
  gets classified.
* **Precedence.** Out-of-scope classes resolve first:
  ``wix_class > waf_blocked > js_rendered > html_first``. A Wix site
  that also happens to render via JS classifies as ``wix_class``; we
  do not waste a Playwright slot on it.
* **Tunable without redeploy.** Patterns live in
  ``site_classifier_signals.yml`` (sibling file). The loader caches
  by mtime; editing the YAML and bouncing the API picks up the new
  signals. The schema_version pin (``SIGNALS_SCHEMA_VERSION``) makes
  the loader raise on a drifted config rather than silently degrading
  to ``unknown`` for every URL.

Latency budget
==============

Anonymous-extract route (Stage O1) gates supported vs. unsupported
classes off this function before enqueueing the heavy extractor. The
hard budget is <5 s; the soft target is <500 ms p95. Caps that hold
that budget:

* ``REQUEST_TIMEOUT_SEC`` (5.0 s connect+read per attempt).
* ``MAX_BODY_BYTES`` (16 KB read cap).
* ``MAX_ATTEMPTS`` (3 total; worst-case 5 + 0.25 + 5 + 1.0 + 5 = 16 s
  is bounded but only triggers on every-attempt timeout, which is
  itself the ``unknown`` outcome).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml

from app.constants import (
    ANON_CLASS_HTML_FIRST,
    ANON_CLASS_JS_RENDERED,
    ANON_CLASS_UNKNOWN,
    ANON_CLASS_WAF_BLOCKED,
    ANON_CLASS_WIX,
    ANON_SUPPORTED_CLASSES,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Schema versions                                                             #
# --------------------------------------------------------------------------- #

SIGNALS_SCHEMA_VERSION: str = "site_classifier_signals_v1"
RESULT_SCHEMA_VERSION: int = 1

# --------------------------------------------------------------------------- #
# Tuning constants                                                            #
# --------------------------------------------------------------------------- #

# Total per-attempt HTTP timeout (connect + read). 5.0 s is the brief's
# anonymous-extract latency budget; the soft target is <500 ms p95.
REQUEST_TIMEOUT_SEC: float = 5.0

# Maximum body bytes read per attempt. 16 KB covers every signal in the
# YAML in practice without streaming the full page.
MAX_BODY_BYTES: int = 16 * 1024

# Body excerpt preserved on ClassificationResult (kept smaller than
# MAX_BODY_BYTES so the response struct stays compact for logging).
BODY_EXCERPT_BYTES: int = 4 * 1024

# Retry delays between attempts on transport-error / 5xx. Exponential.
# Two retries (three attempts total) keeps the upper-bound latency
# bounded.
RETRY_DELAYS_SEC: tuple[float, ...] = (0.25, 1.0)
MAX_ATTEMPTS: int = len(RETRY_DELAYS_SEC) + 1

# Maximum redirect chain depth. 3 covers www <-> apex and http -> https
# in the common case without letting a tarpit waste budget.
MAX_REDIRECTS: int = 3

# html_first requires a non-trivial body so a 200 OK with an empty body
# does not auto-classify supported. 5 KB matches the brief's "substantive
# semantic markup" threshold.
HTML_FIRST_MIN_BODY_BYTES: int = 5 * 1024

# Headers retained on the result struct. Other headers are dropped so we
# do not surface server-internal hints downstream and keep the response
# size predictable.
HEADERS_RETAINED: frozenset[str] = frozenset(
    {
        "content-type",
        "server",
        "cf-ray",
        "cf-mitigated",
        "x-akamai",
        "x-wix-request-id",
        "x-wix-published-version",
        "x-powered-by",
    }
)

# Class precedence: out-of-scope classes resolve first. The order maps to
# the YAML's class blocks; classes missing from the YAML are skipped.
CLASS_PRECEDENCE: tuple[str, ...] = (
    ANON_CLASS_WIX,
    ANON_CLASS_WAF_BLOCKED,
    ANON_CLASS_JS_RENDERED,
    ANON_CLASS_HTML_FIRST,
)

# User-Agent. Identifies the classifier so target sites can opt out via
# robots.txt rules they author themselves. Distinct from the extractor
# UA so the two surfaces are independently rate-limited by upstream
# servers if they want.
USER_AGENT: str = (
    "Mozilla/5.0 (compatible; ResemblioClassifier/1.0; "
    "+https://resemblio.com/bot)"
)

# Path to the signal YAML config. Co-located with this module so
# deployment ships them together. Override for tests via the
# ``signals_path`` argument on ``classify_url``.
DEFAULT_SIGNALS_PATH: Path = Path(__file__).resolve().parent / "site_classifier_signals.yml"


# --------------------------------------------------------------------------- #
# Result type                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClassificationResult:
    """Structured classifier output consumed by the anonymous-extract route.

    Backward-compat: the original Stage-O1 stub returned a frozen
    dataclass with ``label``, ``confidence``, ``schema_version`` only.
    Those three fields stay first with the same names. New diagnostic
    fields are added with safe defaults so existing callers that only
    use the original three continue to work without changes.

    Fields:
      label:             one of html_first / js_rendered / wix_class /
                         waf_blocked / unknown.
      confidence:        float in [0.0, 1.0]. For ``unknown``, always 0.0.
      schema_version:    pinned to ``RESULT_SCHEMA_VERSION``.
      signals_matched:   human-readable signal labels for the chosen
                         class (or every match across classes when the
                         result is ``unknown``, so the tuning workflow
                         sees what almost fired).
      http_status:       final HTTP status. ``-1`` on transport failure
                         after retries.
      response_headers:  lowercase-key dict of retained headers (see
                         HEADERS_RETAINED).
      body_excerpt:      first ``BODY_EXCERPT_BYTES`` of the response
                         body decoded best-effort as UTF-8 with
                         errors=replace.
    """

    label: str
    confidence: float
    schema_version: int = RESULT_SCHEMA_VERSION
    signals_matched: tuple[str, ...] = ()
    http_status: int = 0
    response_headers: tuple[tuple[str, str], ...] = ()
    body_excerpt: str = ""

    @property
    def class_name(self) -> str:
        """Alias for ``label``; matches the CTO-respec naming convention.

        The respec spec and other planning docs refer to ``class_name``
        while the in-process stub used ``label``. Keep both alive so
        either spelling reads cleanly.
        """
        return self.label

    @property
    def headers_dict(self) -> dict[str, str]:
        """Return ``response_headers`` as a plain dict for easier logging."""
        return dict(self.response_headers)


# --------------------------------------------------------------------------- #
# Internal: signal loading                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _ClassSignals:
    """Compiled signal block for one class.

    Encapsulates the regex-compiled body patterns, the (header, regex)
    pairs, and the per-class scoring constants. Compilation runs once
    per file mtime via the ``_load_signals`` cache; the hot path
    (classification) never re-compiles regex.
    """

    name: str
    min_signals: int
    confidence_base: float
    confidence_step: float
    body_patterns: tuple[tuple[str, re.Pattern[str]], ...]
    header_patterns: tuple[tuple[str, re.Pattern[str]], ...]
    status_codes: frozenset[int]


# (mtime, compiled) cache so a YAML edit picks up on the next request
# without forcing an API process restart in production.
_signals_cache: dict[Path, tuple[float, tuple[_ClassSignals, ...]]] = {}


def _compile_body_pattern(raw: str) -> re.Pattern[str]:
    """Compile a YAML body pattern.

    Default-case substrings compile to escaped patterns. Strings
    prefixed with ``re:`` compile as raw regex. The split keeps the
    YAML human-readable for the common case while still allowing
    targeted regex.
    """
    if raw.startswith("re:"):
        return re.compile(raw[len("re:"):], re.IGNORECASE | re.DOTALL)
    return re.compile(re.escape(raw), re.IGNORECASE)


def _compile_header_pattern(needle: str) -> re.Pattern[str]:
    """Compile a header value pattern; empty needle matches any value."""
    if needle == "":
        return re.compile(r".*", re.IGNORECASE | re.DOTALL)
    return re.compile(re.escape(needle), re.IGNORECASE)


def _load_signals(path: Path | None = None) -> tuple[_ClassSignals, ...]:
    """Load and cache compiled signals from the YAML config.

    Re-reads the file when its mtime changes; otherwise returns the
    cached compiled tuple. Raises ``RuntimeError`` if the schema
    version drifts from ``SIGNALS_SCHEMA_VERSION`` so a malformed config
    fails loud rather than silently classifying every URL as
    ``unknown``.
    """
    target = (path or DEFAULT_SIGNALS_PATH).resolve()
    mtime = target.stat().st_mtime
    cached = _signals_cache.get(target)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    with target.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    schema_version = raw.get("schema_version")
    if schema_version != SIGNALS_SCHEMA_VERSION:
        raise RuntimeError(
            f"site_classifier signals schema_version mismatch: "
            f"file={schema_version!r} expected={SIGNALS_SCHEMA_VERSION!r}"
        )

    compiled: list[_ClassSignals] = []
    classes_block = raw.get("classes") or {}
    for name in CLASS_PRECEDENCE:
        block = classes_block.get(name)
        if block is None:
            continue
        compiled.append(
            _ClassSignals(
                name=name,
                min_signals=int(block.get("min_signals", 1)),
                confidence_base=float(block.get("confidence_base", 0.5)),
                confidence_step=float(block.get("confidence_step", 0.05)),
                body_patterns=tuple(
                    (raw_pat, _compile_body_pattern(raw_pat))
                    for raw_pat in (block.get("patterns_body") or [])
                ),
                header_patterns=tuple(
                    (str(header_name).lower(), _compile_header_pattern(str(needle)))
                    for header_name, needle in (block.get("patterns_headers") or [])
                ),
                status_codes=frozenset(int(s) for s in (block.get("status_codes") or [])),
            )
        )
    snapshot = tuple(compiled)
    _signals_cache[target] = (mtime, snapshot)
    return snapshot


def _reset_signals_cache() -> None:
    """Drop the in-process signals cache. Test seam only."""
    _signals_cache.clear()


# --------------------------------------------------------------------------- #
# Internal: fetching                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _FetchOutcome:
    """Result of one classifier fetch.

    ``status`` is ``-1`` when no response was obtained (transport error
    or timeout exhausted after retries). ``body`` is bytes capped at
    ``MAX_BODY_BYTES``; ``headers`` is a lowercase-key plain dict.
    """

    status: int
    headers: dict[str, str]
    body: bytes
    error: str | None = None


def _fetch_first_bytes(
    url: str,
    *,
    client: httpx.Client | None = None,
    timeout_sec: float = REQUEST_TIMEOUT_SEC,
    retry_delays: tuple[float, ...] = RETRY_DELAYS_SEC,
    sleep: Callable[[float], None] = time.sleep,
) -> _FetchOutcome:
    """Fetch up to ``MAX_BODY_BYTES`` of the URL with retry + backoff.

    ``client`` is the dependency-injection seam: tests pass a client
    wired to ``httpx.MockTransport`` so no live network fires. When
    None, a short-lived client is created and closed inside this call.
    ``sleep`` is also injected so tests do not wait between retries.

    Retry policy:
      - Transport errors (Timeout, ConnectError, etc.) -> retry.
      - 5xx -> retry.
      - 4xx -> return as-is (4xx is real classification signal).

    On exhaustion the outcome carries ``status=-1`` and ``error`` set
    to the last exception or upstream status.
    """
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=timeout_sec,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            headers={"User-Agent": USER_AGENT},
        )
    last_error: str | None = None
    try:
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = client.get(url)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("site_classifier fetch attempt %d failed: %s", attempt, last_error)
                if attempt < len(retry_delays):
                    sleep(retry_delays[attempt])
                continue

            if 500 <= response.status_code < 600 and attempt < len(retry_delays):
                last_error = f"upstream {response.status_code}"
                sleep(retry_delays[attempt])
                continue

            # Cap body at MAX_BODY_BYTES. httpx already buffered the
            # full response; the sync client does not expose a "stop
            # at N bytes" knob, so we slice defensively in case the
            # target streams a large body.
            body = response.content[:MAX_BODY_BYTES]
            headers = {k.lower(): v for k, v in response.headers.items()}
            return _FetchOutcome(
                status=response.status_code,
                headers=headers,
                body=body,
                error=None,
            )

        return _FetchOutcome(status=-1, headers={}, body=b"", error=last_error)
    finally:
        if owns_client:
            client.close()


# --------------------------------------------------------------------------- #
# Internal: signal evaluation                                                 #
# --------------------------------------------------------------------------- #


def _count_signals(
    signals: _ClassSignals,
    *,
    body_text: str,
    headers: dict[str, str],
    status: int,
) -> list[str]:
    """Return the human-readable labels of every signal that matched.

    Each body pattern, header pattern, and status-code listing counts
    as one signal. The labels include the raw pattern source so the
    tuning workflow can grep the YAML to find the matching block.
    """
    matched: list[str] = []
    for raw_pat, compiled in signals.body_patterns:
        if compiled.search(body_text):
            matched.append(f"body:{raw_pat[:80]}")
    for header_name, compiled in signals.header_patterns:
        value = headers.get(header_name)
        if value is not None and compiled.search(value):
            matched.append(f"header:{header_name}={value[:60]}")
    if status in signals.status_codes:
        matched.append(f"status:{status}")
    return matched


def _confidence_for(signals: _ClassSignals, matched: int) -> float:
    """Compute the class confidence given the matched-signal count."""
    if matched < signals.min_signals:
        return 0.0
    over = matched - signals.min_signals
    return min(1.0, signals.confidence_base + over * signals.confidence_step)


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def classify_url(
    url: str,
    *,
    client: httpx.Client | None = None,
    trusted_auth_context: bool = False,
    signals_path: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ClassificationResult:
    """Classify a URL into one of the five Resemblio support classes.

    Args:
      url: absolute http(s) URL. Caller is expected to have done basic
        syntactic validation (the anonymous-extract route uses
        Pydantic ``AnyHttpUrl``); this function does not parse-check
        the URL beyond what httpx enforces.
      client: optional ``httpx.Client`` for dependency-injected testing.
        When None, a short-lived client is created and closed
        internally.
      trusted_auth_context: when True, the classifier does NOT treat
        401 / 407 responses as ``waf_blocked`` signals. Set this for
        private extractions where the caller supplied HTTP basic auth
        and a 401 is a credentials problem, not a WAF challenge.
      signals_path: override path to the YAML signals config (test
        seam).
      sleep: injected sleep function for retry backoff (test seam).

    Returns:
      A ``ClassificationResult`` dataclass.

    Behaviour on errors:
      Transport failures (DNS, timeout exhausted, etc.) return
      ``label='unknown'`` with ``http_status=-1`` and the underlying
      error message included in ``signals_matched``. This is the
      out-of-scope-with-tuning path: the route refunds and captures
      the URL for human review.
    """
    signals = _load_signals(signals_path)
    outcome = _fetch_first_bytes(url, client=client, sleep=sleep)

    if outcome.status == -1:
        return ClassificationResult(
            label=ANON_CLASS_UNKNOWN,
            confidence=0.0,
            schema_version=RESULT_SCHEMA_VERSION,
            signals_matched=(f"fetch_error:{outcome.error or 'unknown'}",),
            http_status=-1,
            response_headers=(),
            body_excerpt="",
        )

    body_text = outcome.body.decode("utf-8", errors="replace")
    body_size = len(outcome.body)
    retained_headers = tuple(
        (k, v) for k, v in outcome.headers.items() if k in HEADERS_RETAINED
    )
    excerpt = body_text[:BODY_EXCERPT_BYTES]

    diagnostic_signals: list[str] = []
    for class_signals in signals:
        matched = _count_signals(
            class_signals,
            body_text=body_text,
            headers=outcome.headers,
            status=outcome.status,
        )
        # Suppress the 401/407 status signal under trusted auth context.
        # Body and header signals still count so a real WAF page hidden
        # behind a basic-auth realm still classifies waf_blocked.
        if (
            class_signals.name == ANON_CLASS_WAF_BLOCKED
            and trusted_auth_context
            and outcome.status in (401, 407)
        ):
            matched = [m for m in matched if not m.startswith(f"status:{outcome.status}")]

        # html_first requires a non-trivial body. Tiny responses with
        # the right semantic markup are not actually extractable.
        if (
            class_signals.name == ANON_CLASS_HTML_FIRST
            and body_size < HTML_FIRST_MIN_BODY_BYTES
        ):
            diagnostic_signals.extend(matched)
            continue

        if len(matched) >= class_signals.min_signals:
            return ClassificationResult(
                label=class_signals.name,
                confidence=_confidence_for(class_signals, len(matched)),
                schema_version=RESULT_SCHEMA_VERSION,
                signals_matched=tuple(matched),
                http_status=outcome.status,
                response_headers=retained_headers,
                body_excerpt=excerpt,
            )
        diagnostic_signals.extend(matched)

    return ClassificationResult(
        label=ANON_CLASS_UNKNOWN,
        confidence=0.0,
        schema_version=RESULT_SCHEMA_VERSION,
        signals_matched=tuple(diagnostic_signals),
        http_status=outcome.status,
        response_headers=retained_headers,
        body_excerpt=excerpt,
    )


def is_supported(label: str) -> bool:
    """Return True when ``label`` is in the supported set.

    Pure-data helper. Centralized so the route handler does not embed
    the supported-set membership check inline (the set itself lives in
    ``app.constants.ANON_SUPPORTED_CLASSES``).
    """
    return label in ANON_SUPPORTED_CLASSES


# --------------------------------------------------------------------------- #
# Class-disposition helper                                                    #
# --------------------------------------------------------------------------- #


# Class -> (is_supported, extraction_quality_floor). Out-of-scope
# classes carry None for the floor; the route uses is_supported=False
# to short-circuit the extraction pipeline. Floors lock per the
# URL-first respec Decision 3.
CLASS_DISPOSITION: dict[str, tuple[bool, float | None]] = {
    ANON_CLASS_HTML_FIRST: (True, 0.60),
    ANON_CLASS_JS_RENDERED: (True, 0.40),
    ANON_CLASS_WIX: (False, None),
    ANON_CLASS_WAF_BLOCKED: (False, None),
    ANON_CLASS_UNKNOWN: (False, None),
}


def extraction_floor_for(label: str) -> float | None:
    """Return the extraction-quality floor for ``label`` (None when out-of-scope).

    Raises ``KeyError`` when ``label`` is not one of the five known
    classes; callers should never pass arbitrary strings.
    """
    return CLASS_DISPOSITION[label][1]
