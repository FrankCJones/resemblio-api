"""Unit tests for `app.site_classifier`.

All network is mocked via ``httpx.MockTransport``; no live HTTP. The
fixtures in this module are synthetic body templates that match the
YAML signal patterns per class. The transport handler returns these
templates with the right status + headers per scenario.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import site_classifier
from app.constants import (
    ANON_CLASS_HTML_FIRST,
    ANON_CLASS_JS_RENDERED,
    ANON_CLASS_UNKNOWN,
    ANON_CLASS_WAF_BLOCKED,
    ANON_CLASS_WIX,
)
from app.site_classifier import (
    BODY_EXCERPT_BYTES,
    HTML_FIRST_MIN_BODY_BYTES,
    MAX_BODY_BYTES,
    RESULT_SCHEMA_VERSION,
    SIGNALS_SCHEMA_VERSION,
    ClassificationResult,
    classify_url,
    extraction_floor_for,
    is_supported,
)


# --------------------------------------------------------------------------- #
# Synthetic body fixtures                                                     #
# --------------------------------------------------------------------------- #


def _padded(body: str, target_size: int = HTML_FIRST_MIN_BODY_BYTES + 256) -> str:
    """Pad a body to exceed HTML_FIRST_MIN_BODY_BYTES.

    Several positive-class fixtures need bodies larger than 5 KB; we
    pad with a harmless comment block so the substantive signals
    still match.
    """
    if len(body) >= target_size:
        return body
    pad = "<!-- " + ("x" * (target_size - len(body) - 10)) + " -->"
    return body + pad


WIX_BODY = _padded(
    """<!doctype html><html><head>
    <meta name="generator" content="Wix.com Website Builder" />
    <script src="https://static.parastorage.com/services/wix-thunderbolt/main.js"></script>
    </head><body><div id="root"></div></body></html>"""
)

JS_RENDERED_BODY = _padded(
    """<!doctype html><html><head>
    <script type="module" src="/_next/static/chunks/main.js"></script>
    </head><body>
    <div id="__next"></div>
    <noscript>You need to enable JavaScript to run this app.</noscript>
    </body></html>"""
)

HTML_FIRST_BODY = _padded(
    """<!doctype html><html><head>
    <link rel="stylesheet" href="/styles.css">
    <style>body { font-family: sans-serif; }</style>
    </head><body>
    <main>
      <article>
        <h1>Welcome to the bakery</h1>
        <p>We bake bread every morning before dawn, with sourdough starters
        that have been alive since 1972.</p>
        <section>
          <h2>Hours</h2>
          <p>Open Tuesday through Sunday, six to two.</p>
        </section>
      </article>
    </main>
    </body></html>""",
    target_size=HTML_FIRST_MIN_BODY_BYTES + 512,
)

CLOUDFLARE_CHALLENGE_BODY = """<!doctype html><html><head>
<title>Just a moment...</title></head>
<body><div class="cf-turnstile">Attention Required! | Cloudflare</div>
<p>Cloudflare Ray ID: deadbeefcafe</p></body></html>"""

AKAMAI_BLOCKED_BODY = """<!doctype html><html><body>
<h1>Access Denied</h1><p>You don't have permission. Reference: Akamai</p>
</body></html>"""

TINY_BODY = "<html><body>hi</body></html>"


# --------------------------------------------------------------------------- #
# Transport helpers                                                           #
# --------------------------------------------------------------------------- #


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    """Build an httpx.Client wired to a MockTransport handler."""
    transport = httpx.MockTransport(handler)
    return httpx.Client(
        transport=transport,
        follow_redirects=True,
        max_redirects=site_classifier.MAX_REDIRECTS,
        timeout=site_classifier.REQUEST_TIMEOUT_SEC,
    )


def _static_handler(
    *,
    status: int = 200,
    body: str = "",
    headers: dict[str, str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler that always replies with the given response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body.encode("utf-8"), headers=headers or {})

    return handler


@pytest.fixture(autouse=True)
def _clear_signals_cache() -> None:
    """Reset the in-process signals cache between tests."""
    site_classifier._reset_signals_cache()


# --------------------------------------------------------------------------- #
# Positive-class tests                                                        #
# --------------------------------------------------------------------------- #


def test_wix_class_positive_classifies_as_wix() -> None:
    client = _make_client(
        _static_handler(
            body=WIX_BODY,
            headers={"X-Wix-Request-Id": "abc123", "content-type": "text/html"},
        )
    )
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_WIX
    assert result.class_name == ANON_CLASS_WIX  # alias property
    assert result.confidence >= 0.80
    assert any("x-wix-request-id" in s.lower() for s in result.signals_matched)
    assert result.schema_version == RESULT_SCHEMA_VERSION


def test_js_rendered_positive_classifies_as_js_rendered() -> None:
    client = _make_client(
        _static_handler(
            body=JS_RENDERED_BODY,
            headers={"content-type": "text/html", "x-powered-by": "Next.js"},
        )
    )
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_JS_RENDERED
    assert result.confidence >= 0.55
    assert len(result.signals_matched) >= 2


def test_html_first_positive_classifies_as_html_first() -> None:
    client = _make_client(
        _static_handler(
            body=HTML_FIRST_BODY,
            headers={"content-type": "text/html"},
        )
    )
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_HTML_FIRST
    assert result.confidence >= 0.65


def test_waf_blocked_cloudflare_challenge_classifies_as_waf() -> None:
    client = _make_client(
        _static_handler(
            status=403,
            body=CLOUDFLARE_CHALLENGE_BODY,
            headers={"cf-ray": "abc-DFW", "cf-mitigated": "challenge"},
        )
    )
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_WAF_BLOCKED
    assert result.http_status == 403


def test_waf_blocked_akamai_503_classifies_as_waf_after_retries() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            503,
            content=AKAMAI_BLOCKED_BODY.encode("utf-8"),
            headers={"server": "AkamaiGHost"},
        )

    client = _make_client(handler)
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_WAF_BLOCKED
    # 503 triggers retry: 1 initial + 2 retries = 3 attempts.
    assert call_count["n"] == 3


# --------------------------------------------------------------------------- #
# Negative-class tests                                                        #
# --------------------------------------------------------------------------- #


def test_html_first_body_does_not_misfire_wix() -> None:
    client = _make_client(_static_handler(body=HTML_FIRST_BODY))
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label != ANON_CLASS_WIX


def test_wix_body_does_not_misfire_html_first_due_to_precedence() -> None:
    # Precedence: wix beats html_first even when both could match.
    client = _make_client(
        _static_handler(body=WIX_BODY, headers={"X-Wix-Request-Id": "z"})
    )
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_WIX


def test_js_rendered_with_only_one_signal_does_not_classify_js() -> None:
    body = _padded(
        """<!doctype html><html><head></head><body>
        <div id="__next"></div>
        <main><article><h1>Headline</h1><p>Substantive paragraph copy
        that spans more than forty characters so the html_first p-tag
        pattern matches and the page lands in html_first.</p></article></main>
        </body></html>"""
    )
    client = _make_client(_static_handler(body=body))
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label != ANON_CLASS_JS_RENDERED


def test_html_first_tiny_body_does_not_classify_html_first() -> None:
    # Body under 5 KB must not classify html_first even with right markup.
    client = _make_client(_static_handler(body="<main><h1>hi</h1></main>"))
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label != ANON_CLASS_HTML_FIRST


def test_waf_status_does_not_misfire_on_html_first_200() -> None:
    client = _make_client(_static_handler(status=200, body=HTML_FIRST_BODY))
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label != ANON_CLASS_WAF_BLOCKED


def test_wix_one_signal_alone_does_not_classify_wix() -> None:
    # Only the header signal, no body marker; min_signals=2 must hold.
    body = _padded("<html><body><p>Just some content.</p></body></html>")
    client = _make_client(
        _static_handler(body=body, headers={"X-Wix-Request-Id": "x"})
    )
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label != ANON_CLASS_WIX


# --------------------------------------------------------------------------- #
# Edge cases                                                                  #
# --------------------------------------------------------------------------- #


def test_empty_200_body_classifies_unknown() -> None:
    client = _make_client(_static_handler(status=200, body=""))
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_UNKNOWN
    assert result.confidence == 0.0
    assert result.http_status == 200


def test_tiny_body_classifies_unknown() -> None:
    client = _make_client(_static_handler(body=TINY_BODY))
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_UNKNOWN


def test_redirect_chain_followed_and_final_classified() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(301, headers={"Location": "https://example.com/step2"})
        if calls["n"] == 2:
            return httpx.Response(301, headers={"Location": "https://example.com/final"})
        return httpx.Response(
            200,
            content=HTML_FIRST_BODY.encode("utf-8"),
            headers={"content-type": "text/html"},
        )

    client = _make_client(handler)
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_HTML_FIRST
    assert calls["n"] == 3


def test_transport_error_returns_unknown_with_error_signal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns lookup failed")

    client = _make_client(handler)
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_UNKNOWN
    assert result.http_status == -1
    assert any("fetch_error" in s for s in result.signals_matched)


def test_timeout_retries_then_returns_unknown() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.TimeoutException("read timeout")

    client = _make_client(handler)
    sleeps: list[float] = []
    result = classify_url(
        "https://example.com/", client=client, sleep=lambda d: sleeps.append(d)
    )
    assert result.label == ANON_CLASS_UNKNOWN
    # MAX_ATTEMPTS = 3 (1 initial + 2 retries)
    assert attempts["n"] == 3
    # Two backoff sleeps between three attempts.
    assert len(sleeps) == 2


def test_5xx_retries_then_succeeds_on_third_attempt() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(502, content=b"bad gateway")
        return httpx.Response(
            200,
            content=HTML_FIRST_BODY.encode("utf-8"),
            headers={"content-type": "text/html"},
        )

    client = _make_client(handler)
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_HTML_FIRST
    assert attempts["n"] == 3


def test_4xx_status_does_not_retry() -> None:
    # 404 is a real signal (page not found), not retryable.
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404, content=b"<html>404</html>")

    client = _make_client(handler)
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert attempts["n"] == 1
    assert result.http_status == 404
    assert result.label == ANON_CLASS_UNKNOWN


def test_large_body_capped_at_max_body_bytes() -> None:
    # Hand the classifier a 1 MB body of html_first markers; only the
    # first 16 KB are inspected. The markers in the first 16 KB win.
    body = HTML_FIRST_BODY + ("x" * (1024 * 1024))
    client = _make_client(_static_handler(body=body))
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_HTML_FIRST
    assert len(result.body_excerpt) <= BODY_EXCERPT_BYTES


def test_trusted_auth_context_suppresses_401_waf_signal() -> None:
    client = _make_client(
        _static_handler(
            status=401,
            body=_padded(HTML_FIRST_BODY),
            headers={"WWW-Authenticate": "Basic realm=staging"},
        )
    )
    result = classify_url(
        "https://example.com/",
        client=client,
        trusted_auth_context=True,
        sleep=lambda _: None,
    )
    assert result.label != ANON_CLASS_WAF_BLOCKED


def test_untrusted_auth_context_401_classifies_waf() -> None:
    client = _make_client(
        _static_handler(
            status=401,
            body="<html><body>auth required</body></html>",
            headers={"WWW-Authenticate": "Basic realm=staging"},
        )
    )
    result = classify_url(
        "https://example.com/", client=client, sleep=lambda _: None
    )
    assert result.label == ANON_CLASS_WAF_BLOCKED


def test_precedence_wix_beats_js_rendered() -> None:
    body = _padded(
        """<!doctype html><html><head>
        <meta name="generator" content="Wix.com Website Builder" />
        <script src="https://static.parastorage.com/x.js"></script>
        <script type="module"></script>
        </head><body>
        <div id="__next"></div>
        <noscript>You need to enable JavaScript to run this app.</noscript>
        </body></html>"""
    )
    client = _make_client(_static_handler(body=body))
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_WIX


def test_precedence_waf_beats_html_first_on_403() -> None:
    client = _make_client(
        _static_handler(
            status=403,
            body=_padded(HTML_FIRST_BODY),
            headers={"cf-ray": "id123", "server": "cloudflare"},
        )
    )
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.label == ANON_CLASS_WAF_BLOCKED


def test_result_headers_retained_are_subset_only() -> None:
    client = _make_client(
        _static_handler(
            body=HTML_FIRST_BODY,
            headers={
                "content-type": "text/html",
                "x-secret-internal": "should-be-stripped",
                "server": "nginx",
            },
        )
    )
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    headers_d = result.headers_dict
    assert "content-type" in headers_d
    assert "server" in headers_d
    assert "x-secret-internal" not in headers_d


def test_result_body_excerpt_uses_replace_on_invalid_utf8() -> None:
    invalid_bytes = b"\xff\xfe<html><body>hi</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=invalid_bytes)

    client = _make_client(handler)
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert isinstance(result.body_excerpt, str)


def test_result_carries_schema_version() -> None:
    client = _make_client(_static_handler(body=HTML_FIRST_BODY))
    result = classify_url("https://example.com/", client=client, sleep=lambda _: None)
    assert result.schema_version == RESULT_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# Class-disposition helpers                                                   #
# --------------------------------------------------------------------------- #


def test_is_supported_for_each_class() -> None:
    assert is_supported(ANON_CLASS_HTML_FIRST) is True
    assert is_supported(ANON_CLASS_JS_RENDERED) is True
    assert is_supported(ANON_CLASS_WIX) is False
    assert is_supported(ANON_CLASS_WAF_BLOCKED) is False
    assert is_supported(ANON_CLASS_UNKNOWN) is False


def test_extraction_floor_for_each_class() -> None:
    assert extraction_floor_for(ANON_CLASS_HTML_FIRST) == 0.60
    assert extraction_floor_for(ANON_CLASS_JS_RENDERED) == 0.40
    assert extraction_floor_for(ANON_CLASS_WIX) is None
    assert extraction_floor_for(ANON_CLASS_WAF_BLOCKED) is None
    assert extraction_floor_for(ANON_CLASS_UNKNOWN) is None


def test_extraction_floor_raises_on_unknown_label() -> None:
    with pytest.raises(KeyError):
        extraction_floor_for("not_a_real_class")


# --------------------------------------------------------------------------- #
# Signals loader                                                              #
# --------------------------------------------------------------------------- #


def test_signals_loader_schema_version_pinned(tmp_path: Path) -> None:
    bad_yaml = tmp_path / "bad.yml"
    bad_yaml.write_text(
        "schema_version: WRONG_VERSION\nclasses: {}\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError):
        site_classifier._load_signals(bad_yaml)


def test_signals_loader_caches_on_mtime(tmp_path: Path) -> None:
    yml = tmp_path / "ok.yml"
    yml.write_text(
        f"schema_version: {SIGNALS_SCHEMA_VERSION}\nclasses: {{}}\n",
        encoding="utf-8",
    )
    first = site_classifier._load_signals(yml)
    second = site_classifier._load_signals(yml)
    # Same mtime -> cache returns identical tuple object.
    assert first is second


# --------------------------------------------------------------------------- #
# Backward-compat with O1 stub call site                                       #
# --------------------------------------------------------------------------- #


def test_existing_o1_import_contract_holds() -> None:
    # The Stage-O1 route imports ClassificationResult, classify_url,
    # is_supported. Make sure the public surface still exposes them at
    # the same names. Direct attribute checks catch silent renames.
    assert ClassificationResult.__name__ == "ClassificationResult"
    assert callable(classify_url)
    assert callable(is_supported)


def test_classification_result_default_field_values() -> None:
    # The original stub returned dataclass(label, confidence,
    # schema_version) only. The new fields all carry safe defaults so
    # the older stub-shaped construction still type-checks.
    r = ClassificationResult(label=ANON_CLASS_HTML_FIRST, confidence=1.0)
    assert r.label == ANON_CLASS_HTML_FIRST
    assert r.confidence == 1.0
    assert r.schema_version == RESULT_SCHEMA_VERSION
    assert r.signals_matched == ()
    assert r.http_status == 0
    assert r.response_headers == ()
    assert r.body_excerpt == ""
