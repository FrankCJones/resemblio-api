"""Unit tests for scripts/save_extraction.py.

All tests use httpx MockTransport with synthetic fixtures. No real network.
The retry sleep is monkeypatched to a no-op so the suite runs fast.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts import save_extraction
from scripts.save_extraction import (
    CliArgs,
    call_with_retries,
    is_transient_status,
    parse_args,
    run,
    write_local_json,
)

# Capture the real httpx.Client before any test monkeypatches save_extraction.httpx.Client.
# Tests below replace save_extraction.httpx.Client with a factory that returns a mocked
# client. Because save_extraction.httpx is the same module object as httpx itself, that
# patch also rebinds httpx.Client globally; if the factory called httpx.Client(...) it
# would recurse into itself. Holding the real constructor in this module-level name lets
# the factory delegate to the un-patched original.
_REAL_HTTPX_CLIENT = httpx.Client


SUCCESS_BODY: dict[str, Any] = {
    "id": 42,
    "status": "ok",
    "tokens": {"bg": "#ffffff", "text": "#111111"},
    "dtcg": {"color": {"bg": {"$value": "#ffffff", "$type": "color"}}},
    "download_url": "https://r2.test/extractions/1/42.zip?expires=900",
    "schema_version": 1,
    "error_log": None,
}


def _client_with_transport(handler: "callable[[httpx.Request], httpx.Response]") -> httpx.Client:
    """Build an httpx.Client wired to a MockTransport handler."""
    return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler))


def test_is_transient_status_classifies_correctly() -> None:
    assert is_transient_status(500) is True
    assert is_transient_status(502) is True
    assert is_transient_status(503) is True
    assert is_transient_status(504) is True
    assert is_transient_status(429) is False
    assert is_transient_status(402) is False
    assert is_transient_status(422) is False
    assert is_transient_status(200) is False


def test_parse_args_requires_url_key_output() -> None:
    args = parse_args(
        [
            "--url", "https://example.com",
            "--api-key", "rk_test_dummy",
            "--output", "/tmp/out.json",
        ]
    )
    assert args["url"] == "https://example.com"
    assert args["api_key"] == "rk_test_dummy"
    assert args["output"] == Path("/tmp/out.json")
    assert args["private"] is False
    assert args["api_base"] == save_extraction.DEFAULT_API_BASE


def test_parse_args_private_and_custom_base() -> None:
    args = parse_args(
        [
            "--url", "https://example.com",
            "--api-key", "rk_test_dummy",
            "--output", "/tmp/out.json",
            "--private",
            "--api-base", "https://api.staging.resemblio.com/",
        ]
    )
    assert args["private"] is True
    assert args["api_base"] == "https://api.staging.resemblio.com"  # trailing slash stripped


def test_call_with_retries_success_on_first_attempt() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=SUCCESS_BODY)

    with _client_with_transport(handler) as client:
        response = call_with_retries(
            client=client,
            api_base="https://api.test",
            api_key="rk_test_dummy",
            url="https://example.com",
            private=False,
            sleep=lambda _s: None,
        )
    assert response.status_code == 200
    assert len(calls) == 1
    payload = json.loads(calls[0].content)
    assert payload == {"url": "https://example.com", "private": False}
    assert calls[0].headers["authorization"] == "Bearer rk_test_dummy"


def test_call_with_retries_retries_on_502_then_succeeds() -> None:
    responses_in_order = [
        httpx.Response(502, text="bad gateway"),
        httpx.Response(200, json=SUCCESS_BODY),
    ]
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return responses_in_order.pop(0)

    sleeps: list[float] = []
    with _client_with_transport(handler) as client:
        response = call_with_retries(
            client=client,
            api_base="https://api.test",
            api_key="rk_test_dummy",
            url="https://example.com",
            private=False,
            sleep=sleeps.append,
        )
    assert response.status_code == 200
    assert len(attempts) == 2
    assert sleeps == [save_extraction.RETRY_BACKOFF_SECONDS[0]]


def test_call_with_retries_does_not_retry_402() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(402, json={"error": "insufficient_credit"})

    with _client_with_transport(handler) as client:
        response = call_with_retries(
            client=client,
            api_base="https://api.test",
            api_key="rk_test_dummy",
            url="https://example.com",
            private=False,
            sleep=lambda _s: None,
        )
    assert response.status_code == 402
    assert len(calls) == 1


def test_call_with_retries_exhausts_on_persistent_5xx() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="unavailable")

    with _client_with_transport(handler) as client:
        response = call_with_retries(
            client=client,
            api_base="https://api.test",
            api_key="rk_test_dummy",
            url="https://example.com",
            private=False,
            sleep=lambda _s: None,
        )
    assert response.status_code == 503
    assert len(calls) == save_extraction.MAX_RETRY_ATTEMPTS


def test_call_with_retries_raises_on_persistent_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with _client_with_transport(handler) as client:
        with pytest.raises(httpx.RequestError):
            call_with_retries(
                client=client,
                api_base="https://api.test",
                api_key="rk_test_dummy",
                url="https://example.com",
                private=False,
                sleep=lambda _s: None,
            )


def test_write_local_json_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "out.json"
    write_local_json(target, SUCCESS_BODY)  # type: ignore[arg-type]
    assert target.exists()
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["id"] == 42
    assert written["schema_version"] == 1
    assert written["tokens"] == {"bg": "#ffffff", "text": "#111111"}


def test_run_writes_file_on_200(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SUCCESS_BODY)

    monkeypatch.setattr(
        save_extraction.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )
    out_path = tmp_path / "tokens.json"
    code = run(
        CliArgs(
            url="https://example.com",
            api_key="rk_test_dummy",
            output=out_path,
            private=False,
            api_base="https://api.test",
        )
    )
    assert code == 0
    body = json.loads(out_path.read_text(encoding="utf-8"))
    assert body["schema_version"] == 1


def test_run_does_not_write_on_402(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": "insufficient_credit"})

    monkeypatch.setattr(
        save_extraction.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )
    out_path = tmp_path / "tokens.json"
    code = run(
        CliArgs(
            url="https://example.com",
            api_key="rk_test_dummy",
            output=out_path,
            private=False,
            api_base="https://api.test",
        )
    )
    assert code == 3
    assert not out_path.exists()


def test_run_does_not_write_on_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "validation"})

    monkeypatch.setattr(
        save_extraction.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )
    out_path = tmp_path / "tokens.json"
    code = run(
        CliArgs(
            url="https://example.com",
            api_key="rk_test_dummy",
            output=out_path,
            private=False,
            api_base="https://api.test",
        )
    )
    assert code == 3
    assert not out_path.exists()


def test_run_returns_4_on_persistent_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        save_extraction.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )
    # Patch retry sleep so this doesn't actually take 21 seconds.
    monkeypatch.setattr(save_extraction.time, "sleep", lambda _s: None)
    out_path = tmp_path / "tokens.json"
    code = run(
        CliArgs(
            url="https://example.com",
            api_key="rk_test_dummy",
            output=out_path,
            private=False,
            api_base="https://api.test",
        )
    )
    assert code == 4
    assert not out_path.exists()


def test_api_key_never_logged(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-braces: the bearer value must not appear in log output."""
    secret = "rk_live_THIS_IS_SECRET_DO_NOT_LOG"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SUCCESS_BODY)

    monkeypatch.setattr(
        save_extraction.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )

    with caplog.at_level("DEBUG", logger="save_extraction"):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            run(
                CliArgs(
                    url="https://example.com",
                    api_key=secret,
                    output=Path(td) / "out.json",
                    private=False,
                    api_base="https://api.test",
                )
            )
    for record in caplog.records:
        assert secret not in record.getMessage()
