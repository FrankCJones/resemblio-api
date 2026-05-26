"""Run a Resemblio extraction via the live HTTP API and dual-write the result.

Purpose
-------
Single extraction call -> API persists to Postgres + R2 (the moat) AND the script
writes the response token JSON to a local path. The local file lets downstream
website-build work proceed independently of the API; the API persistence is
already done by the service before the response returns.

Use case
--------
Susann Camus Week 1: each client-provided reference URL gets one invocation,
output piped into `projects/Clients/Susann Camus/intake/extracted-tokens/<slug>.json`.

Dependencies
------------
Stdlib + httpx (already in code/api/pyproject.toml). No DB access from this
script; persistence happens server-side via POST /v1/extractions.

Run command
-----------
    python -m scripts.save_extraction \
        --url https://example.com \
        --api-key rk_live_... \
        --output /abs/path/to/tokens.json \
        [--private] \
        [--api-base https://api.resemblio.com]

Exit codes
----------
0  success; local file written
2  bad CLI usage
3  API returned an error (402, 422, etc.); no local file written
4  unrecoverable transport error after retries; no local file written

Quality floor
-------------
Per workspace CLAUDE.md > Quality floor. Public functions carry docstrings,
TypedDict for the response shape, retry-with-backoff on transient 5xx, no
logging of the api-key value, schema_version preserved in the output.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

import httpx

logger = logging.getLogger("save_extraction")

# Retry budget for transient 5xx and network failures. Three attempts with
# exponential backoff covers the common "single hiccup" case without keeping a
# CLI hanging for the full pathological worst-case.
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (1.0, 4.0, 16.0)
REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_API_BASE = "https://api.resemblio.com"


class ExtractionResponseDict(TypedDict, total=False):
    """Shape of the JSON returned by POST /v1/extractions on success.

    Matches `app.schemas.ExtractionResponse`. Kept as a TypedDict (not pydantic)
    because this CLI deliberately depends only on stdlib + httpx, not on the
    server-side pydantic model.
    """

    id: int
    status: str
    tokens: dict[str, Any] | None
    dtcg: dict[str, Any] | None
    download_url: str | None
    schema_version: int
    error_log: str | None


class CliArgs(TypedDict):
    """Parsed CLI arguments."""

    url: str
    api_key: str
    output: Path
    private: bool
    api_base: str


def parse_args(argv: list[str] | None = None) -> CliArgs:
    """Parse and validate CLI arguments.

    The API key is read from --api-key for parity with the rest of the script
    set. Reading from an env var was rejected to keep the dispatch surface
    explicit at the call site.
    """
    parser = argparse.ArgumentParser(
        description="Run an extraction via the Resemblio API and save the token JSON locally."
    )
    parser.add_argument("--url", required=True, help="URL to extract (e.g. https://example.com)")
    parser.add_argument("--api-key", required=True, help="Resemblio API key (rk_live_... or rk_test_...)")
    parser.add_argument("--output", required=True, help="Absolute path to write the response JSON")
    parser.add_argument("--private", action="store_true", help="Charge private rate ($10 vs $5)")
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"API base URL (default {DEFAULT_API_BASE})",
    )
    ns = parser.parse_args(argv)
    return CliArgs(
        url=ns.url,
        api_key=ns.api_key,
        output=Path(ns.output),
        private=bool(ns.private),
        api_base=ns.api_base.rstrip("/"),
    )


def is_transient_status(status_code: int) -> bool:
    """Return whether the status code is worth retrying.

    502/503/504 are the canonical "retry" set. 500 is included because the
    Resemblio API returns 500 on extractor-bridge crashes that may be transient
    (Playwright timing out under load). 429 is NOT retried by this client;
    the API has its own rate limiter and the right answer for the operator is
    to slow the dispatch loop, not to retry hot.
    """
    return status_code in (500, 502, 503, 504)


def post_extraction(
    client: httpx.Client,
    api_base: str,
    api_key: str,
    url: str,
    private: bool,
) -> httpx.Response:
    """POST one extraction request. Caller handles retry policy.

    Note: never log the api_key value (workspace boundary). The Authorization
    header is constructed locally and not echoed back into logger output.
    """
    return client.post(
        f"{api_base}/v1/extractions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"url": url, "private": private},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def call_with_retries(
    client: httpx.Client,
    api_base: str,
    api_key: str,
    url: str,
    private: bool,
    sleep: "callable[[float], None]" = time.sleep,
) -> httpx.Response:
    """POST with exponential backoff on transient 5xx + network failure.

    Returns the final response (success or non-transient error). Raises
    httpx.RequestError if every attempt failed at the transport layer.
    The `sleep` injection point exists so tests can fast-forward without
    waiting real wall-clock seconds.
    """
    last_transport_error: httpx.RequestError | None = None
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        try:
            response = post_extraction(client, api_base, api_key, url, private)
        except httpx.RequestError as exc:
            last_transport_error = exc
            logger.warning("attempt %d transport error: %s", attempt, exc.__class__.__name__)
            if attempt < MAX_RETRY_ATTEMPTS:
                sleep(RETRY_BACKOFF_SECONDS[attempt - 1])
                continue
            raise

        if is_transient_status(response.status_code) and attempt < MAX_RETRY_ATTEMPTS:
            logger.warning("attempt %d returned transient %d, retrying", attempt, response.status_code)
            sleep(RETRY_BACKOFF_SECONDS[attempt - 1])
            continue
        return response

    # Unreachable: loop above either returns or raises. Kept for type checker.
    assert last_transport_error is not None
    raise last_transport_error


def write_local_json(output_path: Path, body: ExtractionResponseDict) -> None:
    """Write the API response to disk, creating parent directories if needed.

    Pretty-printed (indent=2) for human readability during client review.
    schema_version sits at the top level of the response and is preserved
    verbatim; this script does not transform the payload.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(body, indent=2, sort_keys=False), encoding="utf-8")


def run(args: CliArgs) -> int:
    """Top-level orchestration. Returns process exit code."""
    logger.info("POST %s/v1/extractions url=%s private=%s", args["api_base"], args["url"], args["private"])
    with httpx.Client() as client:
        try:
            response = call_with_retries(
                client=client,
                api_base=args["api_base"],
                api_key=args["api_key"],
                url=args["url"],
                private=args["private"],
            )
        except httpx.RequestError as exc:
            print(f"transport error after retries: {exc.__class__.__name__}: {exc}", file=sys.stderr)
            return 4

    if response.status_code != 200:
        # 402 insufficient_credit, 422 validation, 5xx after retries.
        # Print server body to stderr so the operator can act; do NOT write a
        # local file (the moat row may still exist server-side as a failed
        # extraction with refund; that is the API's responsibility).
        print(
            f"API returned {response.status_code}: {response.text}",
            file=sys.stderr,
        )
        return 3

    body: ExtractionResponseDict = response.json()
    write_local_json(args["output"], body)
    print(
        f"OK extraction_id={body.get('id')} status={body.get('status')} "
        f"schema_version={body.get('schema_version')} wrote={args['output']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for `python -m scripts.save_extraction`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
