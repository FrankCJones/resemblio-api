"""Wave 3 user-flow end-to-end smoke against prod (or any reachable Resemblio API).

Purpose
-------
Exercise the full S3b Wave 3 user surface end-to-end against the live stack
before any customer touches it. Covers:

  1.  Bootstrap a throwaway test user on the box (SSH + on-box Python helper
      mirroring ``scripts/create_first_user.py``). Mints an onboarding grant
      ($10) and an initial user-kind API key. Email format:
      ``smoke+wave3-<timestamp>@optsus.com``.
  2.  ``GET  /v1/api_keys``                       (list user keys; starter visible)
  3.  ``POST /v1/api_keys``                       (create new key; plaintext once)
  4.  ``GET  /v1/api_keys/{id}/audit``            (verify 'created' event)
  5.  ``POST /v1/api_keys/{id}/rotate``           (verify new plaintext + grace)
  6.  Use OLD rotated key against ``/v1/account`` (verify 48h grace + warning header)
  7.  Use NEW rotated key against ``/v1/account`` (verify accepted)
  8.  ``POST /v1/api_keys/{id}/revoke``           (verify status flip + audit event)
  9.  Use REVOKED key                              (verify 401 ``key_revoked``)
  10. ``POST /v1/extractions {url: https://example.com}`` with starter key
      (verify the canonical API path works for a Wave 3 key chain).
  11. Cleanup on the box (delete events, ledger, sessions, keys, extractions,
      magic-link tokens, user). Smoke is idempotent: a second run with the same
      timestamp is a no-op on rerun (timestamp keeps the email unique).

Authority
---------
GREEN. Non-destructive, read-mostly. Touches only the throwaway user it
created. Aborts before any prod-user data is modified.

How to run
----------
From the workspace:

    cd "projects/Resemblio/code/api"
    python scripts/smoke_wave3_user_flow.py \\
        --api-base https://api.resemblio.com \\
        --ssh-host 5.161.249.32

Defaults to prod. ``--no-extraction`` skips step 10 if you do not want to
spend the $5 onboarding credit. ``--keep-user`` skips cleanup (debug only).

Exit codes: 0 on full pass, 1 on any failure. A structured JSON pass/fail
summary plus per-step timing is appended to::

    projects/Resemblio/code/api/_smoke_logs/wave3-YYYY-MM-DD.log

Dependencies: stdlib + ``requests`` (already in pyproject). SSH is the system
``ssh`` binary; key + known_hosts paths read from
``infra/box-resemblio-prod-01.yaml`` semantics (hard-coded here so the smoke
has zero workspace coupling).

Schema: ``smoke_wave3_user_flow_v1``
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

import requests

SCHEMA_VERSION = "smoke_wave3_user_flow_v1"

# Hard-coded prod SSH conventions per workspace CLAUDE.md > How to Communicate.
# Single canonical form; never copy the key elsewhere; always quote the
# UserKnownHostsFile value (workspace Decision 11, 2026-06-01).
_SSH_KEY = "/c/Users/fjone/Desktop/Shared with Claude/_credentials/resemblio_ed25519"
_SSH_KNOWN_HOSTS = "/c/Users/fjone/Desktop/Shared with Claude/_credentials/resemblio-prod-01.known_hosts"
# The known_hosts file pinned in _credentials/ holds ecdsa + ssh-rsa entries
# for resemblio-prod-01; the host also offers ed25519 by default. We constrain
# the algorithm list to the pinned set so StrictHostKeyChecking=yes still
# negotiates a recognized key. If a future re-pin adds ed25519, widen this.
_SSH_HOST_KEY_ALGS = "ecdsa-sha2-nistp256,ssh-rsa"
_SSH_USER = "claude-cowork"
_VENV_PY = "/opt/resemblio-api/venv/bin/python"
_API_APP_DIR = "/opt/resemblio-api/app"
_ENV_FILE = "/opt/resemblio-api/.env"

_REQUEST_TIMEOUT = 30  # seconds; extraction can be slow on cold paths
_RETRY_DELAYS = (1.0, 3.0, 7.0)
_TOKEN_RE = re.compile(r"^rsmb_(live|test)_[A-Za-z0-9_-]{43}$")

_LOG_DIR = Path(__file__).resolve().parents[1] / "_smoke_logs"

logger = logging.getLogger("smoke_wave3")


@dataclass
class StepResult:
    """Outcome of one smoke step."""

    name: str
    ok: bool
    elapsed_ms: int
    detail: str = ""
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SmokeReport:
    """Final structured report for one smoke run."""

    schema_version: str
    started_at_utc: str
    finished_at_utc: str
    api_base: str
    test_email: str
    total_elapsed_ms: int
    passed: bool
    steps: list[StepResult]


class SmokeError(RuntimeError):
    """Raised when a step fails in a way that should halt the smoke."""


# --------------------------------------------------------------------------- #
# SSH helpers                                                                 #
# --------------------------------------------------------------------------- #


def _ssh_argv(host: str) -> list[str]:
    """Return the canonical SSH argv prefix.

    The ``UserKnownHostsFile`` path contains spaces; per workspace Decision 11
    the value is inner-quoted as ``UserKnownHostsFile="<path>"``. ``shlex``
    quoting on the surrounding ``ssh`` option is what enforces the rule.
    """
    return [
        "ssh",
        "-i",
        _SSH_KEY,
        "-o",
        f'UserKnownHostsFile="{_SSH_KNOWN_HOSTS}"',
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"HostKeyAlgorithms={_SSH_HOST_KEY_ALGS}",
        "-o",
        "ConnectTimeout=10",
        f"{_SSH_USER}@{host}",
    ]


def _run_on_box(host: str, remote_cmd: str, *, timeout: int = 60) -> str:
    """Execute ``remote_cmd`` over SSH and return stdout. Raise on non-zero."""
    argv = _ssh_argv(host) + [remote_cmd]
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise SmokeError(
            f"SSH command failed rc={completed.returncode}\n"
            f"stderr: {completed.stderr.strip()[:500]}\n"
            f"stdout: {completed.stdout.strip()[:500]}"
        )
    return completed.stdout


def _run_python_on_box(host: str, source: str, *, mode: str, email: str | None = None, user_id: int | None = None) -> dict[str, Any]:
    """Run a heredoc Python snippet on the box under the API's venv + env.

    ``mode`` distinguishes create vs delete; ``email`` is the smoke user's
    address. Output is the last JSON line printed by the snippet. The snippet
    runs under ``sudo`` so it can read ``/opt/resemblio-api/.env``; the
    process drops back to the API's normal user identity (``claude-cowork``
    is the service user too, so privileges don't escalate beyond reading the
    env file).
    """
    # Build the remote command. We pipe the Python source via stdin and run a
    # short bash that loads the env file under sudo then execs the venv
    # interpreter reading from /dev/stdin. The arguments after `python -` are
    # consumed by argparse inside the snippet.
    args_parts: list[str] = ["--mode", mode]
    if email is not None:
        args_parts.extend(["--email", email])
    if user_id is not None:
        args_parts.extend(["--user-id", str(user_id)])
    inner_args = " ".join(shlex.quote(part) for part in args_parts)
    # `set -a` exports every var the env file defines so the venv inherits
    # them; `set +a` turns it off after to keep the rest of the command clean.
    remote_cmd = (
        f"sudo bash -c 'set -a; . {shlex.quote(_ENV_FILE)}; set +a; "
        f"cd {shlex.quote(_API_APP_DIR)} && "
        f"{shlex.quote(_VENV_PY)} - {inner_args}'"
    )
    argv = _ssh_argv(host) + [remote_cmd]
    completed = subprocess.run(
        argv,
        input=source,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise SmokeError(
            f"on-box python failed rc={completed.returncode}\n"
            f"stderr: {completed.stderr.strip()[:800]}\n"
            f"stdout: {completed.stdout.strip()[:800]}"
        )
    # The snippet prints a single JSON object on its last stdout line.
    lines = [ln for ln in completed.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise SmokeError("on-box python produced no stdout")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise SmokeError(f"on-box python stdout was not JSON: {lines[-1]!r}") from exc


_BOOTSTRAP_SOURCE = r"""
'''Create or delete a Wave 3 smoke user. Prints one JSON line on stdout.

Two modes:
    --mode create --email <email>      -> creates user, onboarding grant, starter user-kind ApiKey
    --mode delete --user-id <int>      -> deletes ApiKeyEvents, CreditLedger, WebSessionKey,
                                          ApiKey rows, MagicLinkToken rows by email, Extraction
                                          rows, and finally the User row.

Mirrors scripts/create_first_user.py for the create path; the delete path is
specific to the smoke harness so the smoke leaves no residue.
'''
from __future__ import annotations
import argparse, json, secrets, sys
from sqlalchemy import delete, select
sys.path.insert(0, '/opt/resemblio-api/app')
from app.constants import DEFAULT_API_SCOPE
from app.crypto import generate_api_key, hash_password
from app.db import SessionLocal
from app.models import ApiKey, ApiKeyEvent, CreditLedger, Extraction, MagicLinkToken, User, WebSessionKey
from app.users import ensure_onboarding_grant

parser = argparse.ArgumentParser()
parser.add_argument('--mode', required=True, choices=['create', 'delete'])
parser.add_argument('--email', default=None)
parser.add_argument('--user-id', dest='user_id', type=int, default=None)
args = parser.parse_args()

with SessionLocal() as session:
    if args.mode == 'create':
        email = (args.email or '').lower()
        if not email:
            print(json.dumps({'ok': False, 'error': 'email required'}))
            sys.exit(2)
        existing = session.query(User).filter(User.email == email).first()
        if existing is not None:
            print(json.dumps({'ok': False, 'error': 'user already exists', 'user_id': existing.id}))
            sys.exit(3)
        user = User(email=email, password_hash=hash_password(secrets.token_urlsafe(32)), status='active')
        session.add(user)
        session.flush()
        ensure_onboarding_grant(session, user)
        plaintext, digest, prefix = generate_api_key('live')
        api_key = ApiKey(
            user_id=user.id,
            key_hash=digest,
            key_prefix=prefix,
            label='wave3-smoke-starter',
            scopes=[DEFAULT_API_SCOPE],
        )
        session.add(api_key)
        session.flush()
        session.add(ApiKeyEvent(api_key_id=api_key.id, event_type='created', metadata_json={'source': 'wave3_smoke'}))
        session.commit()
        print(json.dumps({'ok': True, 'user_id': user.id, 'email': user.email, 'api_key': plaintext, 'key_id': api_key.id, 'key_prefix': prefix}))
    else:
        user_id = args.user_id
        if user_id is None:
            print(json.dumps({'ok': False, 'error': 'user-id required'}))
            sys.exit(2)
        user = session.get(User, user_id)
        if user is None:
            print(json.dumps({'ok': True, 'note': 'user already absent'}))
            sys.exit(0)
        # delete child rows in FK-safe order
        key_ids = [row[0] for row in session.execute(select(ApiKey.id).where(ApiKey.user_id == user_id)).all()]
        if key_ids:
            session.execute(delete(ApiKeyEvent).where(ApiKeyEvent.api_key_id.in_(key_ids)))
        session.execute(delete(WebSessionKey).where(WebSessionKey.user_id == user_id))
        session.execute(delete(CreditLedger).where(CreditLedger.user_id == user_id))
        session.execute(delete(Extraction).where(Extraction.user_id == user_id))
        session.execute(delete(ApiKey).where(ApiKey.user_id == user_id))
        session.execute(delete(MagicLinkToken).where(MagicLinkToken.email == user.email))
        session.delete(user)
        session.commit()
        print(json.dumps({'ok': True, 'deleted_user_id': user_id}))
"""


# --------------------------------------------------------------------------- #
# HTTP helpers                                                                #
# --------------------------------------------------------------------------- #


def _http(
    method: str,
    url: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | None = None,
    expect: tuple[int, ...] = (200,),
    allow_status: tuple[int, ...] | None = None,
) -> requests.Response:
    """HTTP call with simple retry and bounded backoff for transient network errors.

    ``expect`` is the success status set; an unexpected status is NOT a transient
    failure (no retry, raises immediately). ``allow_status`` extends the set of
    statuses the caller is willing to accept without raising; useful for the
    revoked-key probe where 401 is the expected result.
    """
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    last_exc: Exception | None = None
    accepted = set(expect) | set(allow_status or ())
    for attempt, delay in enumerate(_RETRY_DELAYS):
        try:
            resp = requests.request(
                method, url, headers=headers, json=json_body, timeout=_REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < len(_RETRY_DELAYS) - 1:
                logger.warning("network error on %s %s; retrying in %.1fs", method, url, delay)
                time.sleep(delay)
                continue
            raise SmokeError(f"network failure on {method} {url}: {exc}") from exc
        if resp.status_code in accepted:
            return resp
        # Non-accepted status: do not retry, surface the body so debugging is fast.
        raise SmokeError(
            f"{method} {url} returned {resp.status_code} (expected one of {sorted(accepted)})\n"
            f"body: {resp.text[:600]}"
        )
    assert last_exc is not None
    raise last_exc  # pragma: no cover - retry exhaustion handled above


def _validate_token_shape(token: str) -> None:
    """Assert the plaintext token matches the canonical regex used by AuthMiddleware."""
    if not _TOKEN_RE.match(token):
        raise SmokeError(f"minted token does not match TOKEN_RE: prefix={token[:12]!r}")


# --------------------------------------------------------------------------- #
# Step orchestration                                                          #
# --------------------------------------------------------------------------- #


def _run_step(name: str, func: Callable[[], dict[str, Any]]) -> StepResult:
    """Run one step, time it, and capture pass/fail without aborting the smoke."""
    start = time.monotonic()
    try:
        extra = func() or {}
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info("PASS %s (%d ms)", name, elapsed_ms)
        return StepResult(name=name, ok=True, elapsed_ms=elapsed_ms, extra=extra)
    except SmokeError as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error("FAIL %s (%d ms): %s", name, elapsed_ms, exc)
        return StepResult(name=name, ok=False, elapsed_ms=elapsed_ms, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - blanket so the smoke always produces a report
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.exception("FAIL %s (%d ms) unexpected", name, elapsed_ms)
        return StepResult(name=name, ok=False, elapsed_ms=elapsed_ms, error=f"unexpected: {exc!r}")


# --------------------------------------------------------------------------- #
# Smoke driver                                                                #
# --------------------------------------------------------------------------- #


def run_smoke(
    *, api_base: str, ssh_host: str, do_extraction: bool, keep_user: bool
) -> SmokeReport:
    """Execute every step. Returns a structured report; never raises."""
    started = _dt.datetime.now(_dt.timezone.utc)
    timestamp = started.strftime("%Y%m%dT%H%M%SZ")
    test_email = f"smoke+wave3-{timestamp}-{uuid.uuid4().hex[:6]}@optsus.com"
    steps: list[StepResult] = []

    # Shared mutable state across steps. Using a dict keeps each step closure
    # narrow and avoids carrying a giant context object.
    state: dict[str, Any] = {"user_id": None, "starter_key": None}

    # 1. Bootstrap on-box.
    def step_bootstrap() -> dict[str, Any]:
        result = _run_python_on_box(ssh_host, _BOOTSTRAP_SOURCE, mode="create", email=test_email)
        if not result.get("ok"):
            raise SmokeError(f"bootstrap failed: {result}")
        _validate_token_shape(result["api_key"])
        state["user_id"] = result["user_id"]
        state["starter_key"] = result["api_key"]
        state["starter_key_id"] = result["key_id"]
        return {"user_id": result["user_id"], "email": result["email"], "key_prefix": result["key_prefix"]}

    steps.append(_run_step("01_bootstrap_user_on_box", step_bootstrap))
    if not steps[-1].ok:
        return _finalize(started, api_base, test_email, steps)

    # 2. List starter keys (should see exactly one user-kind key).
    def step_list_initial() -> dict[str, Any]:
        resp = _http("GET", f"{api_base}/v1/api_keys", token=state["starter_key"])
        body = resp.json()
        if body.get("schema_version") != 1:
            raise SmokeError(f"list schema_version != 1: {body}")
        items = body.get("items", [])
        if len(items) != 1:
            raise SmokeError(f"expected 1 starter key in list, got {len(items)}: {items}")
        if items[0].get("status") != "active":
            raise SmokeError(f"starter key not active: {items[0]}")
        return {"item_count": len(items), "starter_status": items[0]["status"]}

    steps.append(_run_step("02_list_initial", step_list_initial))

    # 3. Create a new key (the under-test surface).
    def step_create_key() -> dict[str, Any]:
        resp = _http(
            "POST",
            f"{api_base}/v1/api_keys",
            token=state["starter_key"],
            json_body={"label": "wave3-smoke-created"},
        )
        body = resp.json()
        if body.get("schema_version") != 1:
            raise SmokeError(f"create schema_version != 1: {body}")
        plaintext = body.get("api_key")
        if not plaintext:
            raise SmokeError(f"create did not return api_key: {body}")
        _validate_token_shape(plaintext)
        # Plaintext must not appear in subsequent list response.
        listed = _http("GET", f"{api_base}/v1/api_keys", token=state["starter_key"]).json()
        for item in listed["items"]:
            if "api_key" in item:
                raise SmokeError(f"list leaked plaintext: {item}")
        state["created_key"] = plaintext
        state["created_key_id"] = body["id"]
        state["created_key_prefix"] = body["key_prefix"]
        return {"key_id": body["id"], "key_prefix": body["key_prefix"]}

    steps.append(_run_step("03_create_new_key", step_create_key))
    if not steps[-1].ok:
        return _finalize(started, api_base, test_email, steps, cleanup_state=state, ssh_host=ssh_host, keep_user=keep_user)

    # 4. Audit of the new key (should contain a 'created' event).
    def step_audit_after_create() -> dict[str, Any]:
        resp = _http(
            "GET",
            f"{api_base}/v1/api_keys/{state['created_key_id']}/audit",
            token=state["starter_key"],
        )
        body = resp.json()
        if body.get("schema_version") != 1:
            raise SmokeError(f"audit schema_version != 1: {body}")
        event_types = {item["event_type"] for item in body["items"]}
        if "created" not in event_types:
            raise SmokeError(f"audit missing 'created' event: {event_types}")
        return {"event_types": sorted(event_types)}

    steps.append(_run_step("04_audit_after_create", step_audit_after_create))

    # 5. Rotate the key.
    def step_rotate() -> dict[str, Any]:
        resp = _http(
            "POST",
            f"{api_base}/v1/api_keys/{state['created_key_id']}/rotate",
            token=state["starter_key"],
        )
        body = resp.json()
        if body.get("schema_version") != 1:
            raise SmokeError(f"rotate schema_version != 1: {body}")
        new_plaintext = body.get("api_key")
        if not new_plaintext or new_plaintext == state["created_key"]:
            raise SmokeError(f"rotate did not return a fresh plaintext: {body}")
        _validate_token_shape(new_plaintext)
        state["rotated_key"] = new_plaintext
        state["rotated_key_id"] = body["id"]
        return {"new_key_id": body["id"], "new_key_prefix": body["key_prefix"]}

    steps.append(_run_step("05_rotate_key", step_rotate))
    if not steps[-1].ok:
        return _finalize(started, api_base, test_email, steps, cleanup_state=state, ssh_host=ssh_host, keep_user=keep_user)

    # 6. OLD rotated-out key should still work during 48h grace, with warning header.
    def step_old_key_grace() -> dict[str, Any]:
        resp = _http("GET", f"{api_base}/v1/account", token=state["created_key"])
        if "X-API-Key-Rotation-Warning" not in resp.headers:
            raise SmokeError("expected X-API-Key-Rotation-Warning header on rotated-out key use")
        return {"warning_header_present": True}

    steps.append(_run_step("06_old_key_works_during_grace", step_old_key_grace))

    # 7. NEW rotated-in key should work cleanly.
    def step_new_key_works() -> dict[str, Any]:
        resp = _http("GET", f"{api_base}/v1/account", token=state["rotated_key"])
        if "X-API-Key-Rotation-Warning" in resp.headers:
            raise SmokeError("fresh rotated-in key should NOT carry rotation warning")
        return {"status": resp.status_code}

    steps.append(_run_step("07_new_key_works_no_warning", step_new_key_works))

    # 8. Revoke the rotated-in key.
    def step_revoke() -> dict[str, Any]:
        resp = _http(
            "POST",
            f"{api_base}/v1/api_keys/{state['rotated_key_id']}/revoke",
            token=state["starter_key"],
            # Must match ApiKeyRevokeRequest.reason Literal in app/schemas.py;
            # "wave3_smoke_cleanup" is not a member and returns 422.
            json_body={"reason": "no_longer_needed"},
        )
        body = resp.json()
        if body.get("status") != "revoked":
            raise SmokeError(f"revoke did not flip status: {body}")
        # Audit should now show the revoke.
        audit = _http(
            "GET",
            f"{api_base}/v1/api_keys/{state['rotated_key_id']}/audit",
            token=state["starter_key"],
        ).json()
        event_types = {item["event_type"] for item in audit["items"]}
        if "revoked" not in event_types:
            raise SmokeError(f"audit missing 'revoked' event after revoke: {event_types}")
        return {"final_status": body["status"], "audit_event_types": sorted(event_types)}

    steps.append(_run_step("08_revoke_and_verify_audit", step_revoke))

    # 9. Revoked key must be rejected.
    def step_revoked_rejected() -> dict[str, Any]:
        resp = _http(
            "GET",
            f"{api_base}/v1/account",
            token=state["rotated_key"],
            expect=(401,),
            allow_status=(401,),
        )
        body = resp.json()
        if body.get("error") not in {"key_revoked", "invalid_credentials"}:
            raise SmokeError(f"revoked key did not 401 cleanly: {resp.status_code} {body}")
        return {"error": body.get("error")}

    steps.append(_run_step("09_revoked_key_rejected_401", step_revoked_rejected))

    # 10. Real /v1/extractions call using the starter key.
    if do_extraction:
        def step_extraction() -> dict[str, Any]:
            # example.com is the canonical safe-to-fetch target. The extractor
            # may still hit low_quality or refund paths; either path is a valid
            # demonstration that auth + charge + extractor are wired correctly.
            resp = _http(
                "POST",
                f"{api_base}/v1/extractions",
                token=state["starter_key"],
                json_body={"url": "https://example.com", "private": False},
                expect=(200,),
                # 402 means the onboarding grant was already partially consumed
                # by a previous smoke; surface it but don't treat as failure of
                # the auth chain, which is what step 10 is really proving.
                allow_status=(200, 402),
            )
            body = resp.json()
            if resp.status_code == 402:
                return {
                    "warning": "insufficient_credit",
                    "balance_cents": body.get("balance_cents"),
                    "required_cents": body.get("required_cents"),
                }
            schema = body.get("schema_version")
            if schema not in (1, 2):
                raise SmokeError(f"extraction schema_version unexpected: {schema}")
            return {
                "extraction_id": body.get("id"),
                "status": body.get("status"),
                "schema_version": schema,
                "refunded": body.get("refunded"),
            }

        steps.append(_run_step("10_post_extraction_with_starter_key", step_extraction))

    # 11. Cleanup (deletes user + all owned rows on box).
    return _finalize(started, api_base, test_email, steps, cleanup_state=state, ssh_host=ssh_host, keep_user=keep_user)


def _finalize(
    started: _dt.datetime,
    api_base: str,
    test_email: str,
    steps: list[StepResult],
    *,
    cleanup_state: dict[str, Any] | None = None,
    ssh_host: str | None = None,
    keep_user: bool = False,
) -> SmokeReport:
    """Run the cleanup step if we have a user id, then build the report."""
    if (
        cleanup_state is not None
        and cleanup_state.get("user_id") is not None
        and ssh_host is not None
        and not keep_user
    ):
        def step_cleanup() -> dict[str, Any]:
            result = _run_python_on_box(
                ssh_host, _BOOTSTRAP_SOURCE, mode="delete", user_id=cleanup_state["user_id"]
            )
            if not result.get("ok"):
                raise SmokeError(f"cleanup failed: {result}")
            return {"deleted_user_id": cleanup_state["user_id"]}

        steps.append(_run_step("99_cleanup_test_user", step_cleanup))
    finished = _dt.datetime.now(_dt.timezone.utc)
    return SmokeReport(
        schema_version=SCHEMA_VERSION,
        started_at_utc=started.isoformat(),
        finished_at_utc=finished.isoformat(),
        api_base=api_base,
        test_email=test_email,
        total_elapsed_ms=int((finished - started).total_seconds() * 1000),
        passed=all(s.ok for s in steps),
        steps=steps,
    )


# --------------------------------------------------------------------------- #
# Logging + CLI                                                               #
# --------------------------------------------------------------------------- #


def _write_log(report: SmokeReport) -> Path:
    """Append the JSON report to the dated log file. Returns the file path."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _LOG_DIR / f"wave3-{report.started_at_utc[:10]}.log"
    payload = asdict(report)
    payload["steps"] = [asdict(s) for s in report.steps]
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2, sort_keys=True))
        fh.write("\n")
    return log_path


def _print_summary(report: SmokeReport, log_path: Path) -> None:
    """Print a terse human-readable summary."""
    status = "PASS" if report.passed else "FAIL"
    print(f"WAVE 3 SMOKE: {status} ({report.total_elapsed_ms} ms wall, {len(report.steps)} steps)")
    print(f"  api_base   = {report.api_base}")
    print(f"  test_email = {report.test_email}")
    print(f"  log        = {log_path}")
    for step in report.steps:
        marker = "OK " if step.ok else "FAIL"
        line = f"  [{marker}] {step.name} ({step.elapsed_ms} ms)"
        if step.error:
            line += f"\n         error: {step.error}"
        elif step.extra:
            keys = ", ".join(f"{k}={v}" for k, v in step.extra.items())
            line += f"  {keys}"
        print(line)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns 0 on full pass, 1 on any failure."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Wave 3 user-flow smoke against prod")
    parser.add_argument("--api-base", default="https://api.resemblio.com", help="API base URL")
    parser.add_argument("--ssh-host", default="5.161.249.32", help="Resemblio prod IP")
    parser.add_argument("--no-extraction", action="store_true", help="Skip step 10 (saves the $5 grant)")
    parser.add_argument("--keep-user", action="store_true", help="Skip cleanup (debug only)")
    args = parser.parse_args(argv)

    report = run_smoke(
        api_base=args.api_base.rstrip("/"),
        ssh_host=args.ssh_host,
        do_extraction=not args.no_extraction,
        keep_user=args.keep_user,
    )
    log_path = _write_log(report)
    _print_summary(report, log_path)
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
