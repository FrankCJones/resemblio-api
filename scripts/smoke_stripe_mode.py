"""Post-cutover smoke harness: confirm Stripe mode is what the env claims.

Run from the prod box (or any host that has the right env vars and can reach
api.resemblio.com):

    RESEMBLIO_STRIPE_MODE=live python smoke_stripe_mode.py

What it does:
  1. Reads RESEMBLIO_STRIPE_MODE + STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST from env
  2. Hits the Stripe /v1/account endpoint with the restricted key, prints the
     returned account id (so the operator can eyeball-match it against the
     known acct_* in Resemblio_STRIPE.md Section 1)
  3. Verifies the key prefix matches the declared mode (sk_live_/rk_live_ for
     live; sk_test_/rk_test_ for test) - this is the same check the startup
     validator does, repeated here so the smoke runs even when boot succeeded
     under a wrong assumption
  4. Hits the local /v1/healthz and asserts 200
  5. Optionally creates a SetupIntent (no charge) when --setup-intent is
     passed; useful as a deeper Stripe-side auth check
  6. Prints "STRIPE MODE: <mode> (verified)" on success, exits 0
     Prints "STRIPE MODE: <claimed> != <actual> (FAILED)" and exits 1 on any
     mismatch

Dependencies: stdlib + requests (already in the api pyproject).

Schema: smoke_stripe_mode_v1
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Literal

import requests

SCHEMA_VERSION = "smoke_stripe_mode_v1"

# Local health endpoint defaults to the Caddy-fronted prod URL; override with
# --healthz-url for local testing (e.g. http://127.0.0.1:8000/v1/healthz).
_DEFAULT_HEALTHZ_URL = "https://api.resemblio.com/v1/healthz"
_STRIPE_ACCOUNT_URL = "https://api.stripe.com/v1/account"
_STRIPE_SETUP_INTENT_URL = "https://api.stripe.com/v1/setup_intents"
_REQUEST_TIMEOUT_SECONDS = 15
_RETRY_DELAYS_SECONDS = (1.0, 4.0)

logger = logging.getLogger("smoke_stripe_mode")


Mode = Literal["test", "live"]


@dataclass(frozen=True)
class SmokeResult:
    """Outcome of one smoke run."""

    claimed_mode: Mode
    detected_mode: Mode
    stripe_account_id: str
    healthz_status: int
    setup_intent_id: str | None


class SmokeError(RuntimeError):
    """Raised when any smoke check fails."""


def _get_env(name: str) -> str:
    """Read an env var or raise SmokeError with a clear message."""
    value = os.environ.get(name)
    if not value:
        raise SmokeError(f"{name} must be set in the environment for the smoke harness")
    return value


def _detect_mode_from_key(restricted_key: str) -> Mode:
    """Classify a restricted key by prefix. Raises if neither prefix matches."""
    lowered = restricted_key.lower()
    if lowered.startswith(("sk_live", "rk_live")):
        return "live"
    if lowered.startswith(("sk_test", "rk_test")):
        return "test"
    raise SmokeError(
        f"Restricted key prefix {restricted_key[:8]!r} is neither test nor live; "
        "the env value does not look like a Stripe restricted key"
    )


def _with_retries(call, operation: str):  # type: ignore[no-untyped-def]
    """Run a callable with simple backoff. Returns the call result or raises."""
    last_error: Exception | None = None
    for index, delay in enumerate(_RETRY_DELAYS_SECONDS):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - smoke surface, want every transient covered
            last_error = exc
            if index == len(_RETRY_DELAYS_SECONDS) - 1:
                break
            logger.warning("%s failed; retrying attempt=%s", operation, index + 1)
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def _fetch_stripe_account(restricted_key: str) -> str:
    """Return the Stripe account id by calling /v1/account."""
    def _call() -> str:
        response = requests.get(
            _STRIPE_ACCOUNT_URL,
            auth=(restricted_key, ""),
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        account_id = response.json().get("id")
        if not account_id:
            raise SmokeError("Stripe /v1/account response missing 'id'")
        return str(account_id)

    return _with_retries(_call, "stripe.account.fetch")


def _fetch_healthz(url: str) -> int:
    """Return the HTTP status from the local healthz endpoint."""
    def _call() -> int:
        response = requests.get(url, timeout=_REQUEST_TIMEOUT_SECONDS)
        return response.status_code

    return _with_retries(_call, "healthz.fetch")


def _create_setup_intent(restricted_key: str) -> str:
    """Create a $0 SetupIntent (no charge) and return its id. Used as a Stripe-side auth probe."""
    def _call() -> str:
        response = requests.post(
            _STRIPE_SETUP_INTENT_URL,
            auth=(restricted_key, ""),
            timeout=_REQUEST_TIMEOUT_SECONDS,
            data={"usage": "off_session"},
        )
        response.raise_for_status()
        intent_id = response.json().get("id")
        if not intent_id:
            raise SmokeError("Stripe /v1/setup_intents response missing 'id'")
        return str(intent_id)

    return _with_retries(_call, "stripe.setup_intent.create")


def run_smoke(*, healthz_url: str, create_setup_intent: bool) -> SmokeResult:
    """Execute the full smoke sequence and return a structured result."""
    claimed_mode_raw = _get_env("RESEMBLIO_STRIPE_MODE").lower()
    if claimed_mode_raw not in ("test", "live"):
        raise SmokeError(f"RESEMBLIO_STRIPE_MODE must be 'test' or 'live'; got {claimed_mode_raw!r}")
    claimed_mode: Mode = claimed_mode_raw  # type: ignore[assignment]
    restricted_key = _get_env("STRIPE_RESTRICTED_KEY_RESEMBLIO_TEST")
    detected_mode = _detect_mode_from_key(restricted_key)
    if detected_mode != claimed_mode:
        raise SmokeError(
            f"Mode mismatch: RESEMBLIO_STRIPE_MODE={claimed_mode} but restricted "
            f"key prefix {restricted_key[:8]!r} is {detected_mode}-mode"
        )
    account_id = _fetch_stripe_account(restricted_key)
    healthz_status = _fetch_healthz(healthz_url)
    if healthz_status != 200:
        raise SmokeError(f"Local healthz returned {healthz_status} (expected 200) at {healthz_url}")
    setup_intent_id: str | None = None
    if create_setup_intent:
        setup_intent_id = _create_setup_intent(restricted_key)
    return SmokeResult(
        claimed_mode=claimed_mode,
        detected_mode=detected_mode,
        stripe_account_id=account_id,
        healthz_status=healthz_status,
        setup_intent_id=setup_intent_id,
    )


def main() -> int:
    """CLI entrypoint. Returns shell exit code (0=ok, 1=fail)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Resemblio Stripe-mode smoke harness")
    parser.add_argument("--healthz-url", default=_DEFAULT_HEALTHZ_URL, help=f"Healthz URL (default: {_DEFAULT_HEALTHZ_URL})")
    parser.add_argument("--setup-intent", action="store_true", help="Also create a $0 SetupIntent as an extra Stripe-auth probe")
    args = parser.parse_args()
    try:
        result = run_smoke(healthz_url=args.healthz_url, create_setup_intent=args.setup_intent)
    except SmokeError as exc:
        logger.error("Smoke FAILED: %s", exc)
        print(f"STRIPE MODE: smoke failed: {exc}", file=sys.stderr)
        return 1
    print(f"STRIPE MODE: {result.detected_mode} (verified)")
    print(f"  stripe_account_id={result.stripe_account_id}")
    print(f"  healthz_status={result.healthz_status}")
    if result.setup_intent_id:
        print(f"  setup_intent_id={result.setup_intent_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
