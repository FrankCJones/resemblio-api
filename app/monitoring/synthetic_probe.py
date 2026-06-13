"""Synthetic user-flow prod probe + state-machine alert dedup.

Why this module exists
======================
On 2026-06-02 the Library v1.1 metadata-route went silent for three hours;
the existing `/v1/healthz` cron returned 200 the entire time (Next.js was up,
the route tree had not crashed) but the user-visible Library pages were
serving an empty body fragment and rendering raw URN tokens. Frank caught the
outage by refreshing his browser, not by an alert. Closes Stage 1 of the CTO
TDD recovery plan dated 2026-06-03 (failure inventory items #16 and #17).

What the probe checks
=====================
Every tick (5 min by default; see ``probe-timer`` systemd unit) the probe
exercises the canonical user-flow surfaces and asserts content markers, not
just status codes:

  1. ``GET https://resemblio.com/``                 -> 200, body marker
  2. ``GET https://resemblio.com/library/``         -> 200, >=1 brand card
  3. ``GET https://resemblio.com/library/<brand>/buttons/``
                                                    -> 200, CSS rule + body
                                                       fragment + no raw URN
  4. ``GET https://api.resemblio.com/v1/healthz``   -> 200
  5. ``GET https://api.resemblio.com/v1/readyz``    -> 200

The body-marker assertions are what catches the 2026-06-02 failure mode that
status-only healthz missed: route renders 200 but body is empty, or tokens
leak into the rendered HTML as raw ``urn:`` strings.

State-machine alert dedup
=========================
A naive "alert on every failed tick" floods the inbox during a real outage
(180 alerts for a 3-hour outage at 1 min cadence; 36 at 5 min). The dedup
rules mirror the ENC Explorer pattern locked into the workspace standard:

  - green -> red transition: alert immediately (new failure)
  - red  -> red with same failure_detail, within DEDUP_WINDOW_SEC: suppress
  - red  -> red with same failure_detail, past DEDUP_WINDOW_SEC: re-nag once
  - red  -> red with DIFFERENT failure_detail: alert (new failure mode)
  - red  -> green: alert (recovered)

Each probe tick is logged to a per-day file under
``RESEMBLIO_PROBE_LOG_DIR`` (default ``/var/log/resemblio/``). Logs are
rotated by `logrotate` not by this module (workspace convention; the daily
file naming is the rotation surface).

Schema
======
The on-disk state JSON carries ``schema_version=synthetic_probe_state_v1``.
The per-tick report dataclass carries ``schema_version=synthetic_probe_report_v1``.

Testability
===========
Network is injected via an ``httpx.Client`` argument; tests pass a client
backed by ``httpx.MockTransport`` so every probe surface is exercised against
synthetic responses with no live IO. The alert sink is also injected; tests
pass a list-appending fake to assert state-transition behavior.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# --------------------------------------------------------------------------- #
# Schema versions (centralized; never inline literals)                        #
# --------------------------------------------------------------------------- #

STATE_SCHEMA_VERSION = "synthetic_probe_state_v1"
REPORT_SCHEMA_VERSION = "synthetic_probe_report_v1"

# --------------------------------------------------------------------------- #
# Tuning constants                                                            #
# --------------------------------------------------------------------------- #

# How long to wait per HTTP request before declaring the surface down.
PROBE_TIMEOUT_SEC = 10.0

# Retry policy. We retry only on transport / 5xx; a 4xx is a real failure
# because the probe URLs are not auth-gated. The brief specifies retry+backoff
# on each GET; keep the sequence short so a 5-min tick never blocks a second
# tick on the timer.
PROBE_RETRY_DELAYS_SEC: tuple[float, ...] = (0.5, 1.5, 3.0)

# Dedup window. 15 min mirrors the ENC pattern: long enough to silence a
# steady-state outage, short enough that Frank still gets a fresh ping if the
# system stays down after he's had time to triage the first alert.
DEDUP_WINDOW_SEC = 15 * 60

# How recently we will trust the state file before re-treating the next tick
# as "unknown". 25 hours guards against a stale state file after a long box
# downtime.
STATE_FRESHNESS_SEC = 25 * 60 * 60

# Default file locations on the box. Overridden in tests + by env vars.
DEFAULT_STATE_DIR = Path("/var/lib/resemblio")
DEFAULT_LOG_DIR = Path("/var/log/resemblio")

# Recognized URN-leak prefixes. The 2026-06-02 H1 raw-URN render leaked a
# literal ``urn:resemblio:...`` string into rendered HTML. Any of these
# appearing in a body fragment is a known regression signature.
URN_LEAK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\burn:[a-z][a-z0-9+.\-]*:", re.IGNORECASE),
)

# Canonical brand the probe exercises for the Library deep-page check. Aeon
# is the reference brand that has shipped through every Library phase to
# date and is asserted-correct in the OPS verification queries.
DEFAULT_LIBRARY_BRAND = "aeon"

logger = logging.getLogger("resemblio.synthetic_probe")

# --------------------------------------------------------------------------- #
# Data shapes                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeCheck:
    """One synthetic check the probe runs.

    `marker_predicate` returns ``""`` (empty string) when the body passes,
    or a one-line failure reason when it does not. Predicates explicitly
    do not raise; the probe converts exceptions into a generic ``check
    raised`` reason so a buggy predicate can never silence a real outage.
    """

    name: str
    method: str
    url: str
    expect_status: int
    marker_predicate: Callable[[str], str]
    # When True, the body is fetched in full; when False, only the status
    # line and headers are checked. We always fetch in full today; the flag
    # exists so a future read-heavy probe can opt out.
    fetch_body: bool = True


@dataclass
class ProbeResult:
    """Outcome of one check inside one tick."""

    name: str
    ok: bool
    elapsed_ms: int
    status_code: int | None
    detail: str  # one-line failure description if not ok; empty if ok
    body_snippet: str = ""  # first 600 chars of body for forensic logging


@dataclass
class ProbeReport:
    """Aggregate outcome of one tick. Persisted to the per-day log."""

    schema_version: str
    started_at_utc: str
    finished_at_utc: str
    overall_status: str  # "green" or "red"
    failure_detail: str  # empty when green; first-failed-check detail when red
    checks: list[ProbeResult]


@dataclass
class ProbeState:
    """Persisted state across ticks. Drives alert dedup."""

    schema_version: str
    last_status: str  # "green" | "red" | "unknown"
    last_failure_detail: str
    last_alert_sent_at: str  # ISO 8601 UTC or empty
    consecutive_red: int
    updated_at: str  # ISO 8601 UTC

    @classmethod
    def fresh(cls) -> ProbeState:
        """Empty state for first run on a never-probed box."""
        return cls(
            schema_version=STATE_SCHEMA_VERSION,
            last_status="unknown",
            last_failure_detail="",
            last_alert_sent_at="",
            consecutive_red=0,
            updated_at=_now_iso(),
        )


@dataclass
class AlertDecision:
    """Output of the state machine. Drives whether an alert fires this tick."""

    should_alert: bool
    reason: str  # short tag for logs: new_failure | recovered | renag | failure_mode_changed | suppressed_dedup | steady_green
    subject: str
    body: str


# --------------------------------------------------------------------------- #
# Time helpers (factored for test override)                                   #
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    """UTC now as RFC3339 with Z suffix."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> _dt.datetime | None:
    """Parse an ISO 8601 UTC string; return None on empty/garbage rather than raise."""
    if not value:
        return None
    try:
        # Tolerate both 'Z' and '+00:00' suffix forms.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Marker predicates                                                           #
# --------------------------------------------------------------------------- #


def _require_substring(needle: str, label: str) -> Callable[[str], str]:
    """Predicate: body must contain ``needle``; failure label names the surface."""

    def _check(body: str) -> str:
        if needle not in body:
            return f"{label}: marker {needle!r} missing from body"
        return ""

    return _check


def _require_regex(pattern: re.Pattern[str], label: str) -> Callable[[str], str]:
    """Predicate: body must match ``pattern`` at least once."""

    def _check(body: str) -> str:
        if not pattern.search(body):
            return f"{label}: pattern {pattern.pattern!r} did not match body"
        return ""

    return _check


def _require_no_urn_leak(body: str) -> str:
    """Body must not contain a raw ``urn:`` token; that's a known 2026-06-02 regression."""
    for pat in URN_LEAK_PATTERNS:
        match = pat.search(body)
        if match:
            return f"raw URN leak detected: {match.group(0)!r}"
    return ""


def _and_predicates(*predicates: Callable[[str], str]) -> Callable[[str], str]:
    """Compose predicates; first non-empty failure wins."""

    def _check(body: str) -> str:
        for predicate in predicates:
            detail = predicate(body)
            if detail:
                return detail
        return ""

    return _check


# --------------------------------------------------------------------------- #
# Check catalog                                                               #
# --------------------------------------------------------------------------- #


def default_checks(
    *,
    web_origin: str = "https://resemblio.com",
    api_origin: str = "https://api.resemblio.com",
    library_brand: str = DEFAULT_LIBRARY_BRAND,
) -> list[ProbeCheck]:
    """Return the canonical Stage-1 synthetic-flow check list.

    Origins are parameterized so the same probe can target a staging surface
    or a local dev server without forking the check definitions. The Library
    brand defaults to Aeon (see DEFAULT_LIBRARY_BRAND).
    """
    # Brand-card marker on the Library hub. The Library hub renders a grid of
    # brand cards; every card carries a per-brand data attribute on its
    # anchor. A 200 with zero matches means the hub is rendering but empty,
    # which IS the 2026-06-02 regression mode.
    brand_card_marker = 'href="/library/'

    # Buttons-page marker. CTO Stage 1 spec: "at least one CSS rule rendered
    # + body fragment present + no raw URN." The button-override pipeline
    # writes a `data-resemblio-button-override` attribute on every brand
    # whose computed-style snapshot is loaded; pages always render the DRL
    # default `.b-btn` selector even when the override is absent, so the
    # `.b-btn` class is the structural marker that proves CSS composition
    # fired. `--ds-` is the namespaced CSS-variable prefix that proves token
    # composition emitted variables, matching OPS query #4.
    return [
        ProbeCheck(
            name="web_root",
            method="GET",
            url=f"{web_origin}/",
            expect_status=200,
            marker_predicate=_and_predicates(
                # The root page is a Next.js render; <html lang= proves the
                # standalone Next server emitted real HTML, not the 500 page.
                _require_regex(re.compile(r"<html[^>]*lang=", re.IGNORECASE), "web_root"),
                _require_no_urn_leak,
            ),
        ),
        ProbeCheck(
            name="library_hub",
            method="GET",
            url=f"{web_origin}/library/",
            expect_status=200,
            marker_predicate=_and_predicates(
                _require_substring(brand_card_marker, "library_hub"),
                _require_no_urn_leak,
            ),
        ),
        ProbeCheck(
            name="library_brand_buttons",
            method="GET",
            url=f"{web_origin}/library/{library_brand}/buttons/",
            expect_status=200,
            marker_predicate=_and_predicates(
                # CSS rule fired (DRL composed at least the .b-btn selector
                # or a token variable).
                _require_regex(
                    re.compile(r"(\.b-btn|--ds-)", re.IGNORECASE),
                    "library_brand_buttons.css_rule",
                ),
                # Body fragment present: the buttons page renders at least
                # one button element. <button or role="button" both qualify.
                _require_regex(
                    re.compile(r"<button|role=\"button\"", re.IGNORECASE),
                    "library_brand_buttons.body_fragment",
                ),
                _require_no_urn_leak,
            ),
        ),
        ProbeCheck(
            name="api_healthz",
            method="GET",
            url=f"{api_origin}/v1/healthz",
            expect_status=200,
            marker_predicate=lambda _body: "",  # status-only check
            fetch_body=False,
        ),
        ProbeCheck(
            name="api_readyz",
            method="GET",
            url=f"{api_origin}/v1/readyz",
            expect_status=200,
            marker_predicate=lambda _body: "",  # status-only check
            fetch_body=False,
        ),
    ]


# --------------------------------------------------------------------------- #
# Network probe core                                                          #
# --------------------------------------------------------------------------- #


def _run_one_check(
    check: ProbeCheck,
    *,
    client: httpx.Client,
    retry_delays: Iterable[float] = PROBE_RETRY_DELAYS_SEC,
    sleeper: Callable[[float], None] = time.sleep,
) -> ProbeResult:
    """Execute one check with retry+backoff. Never raises; returns a ProbeResult.

    Transport errors and 5xx retry; non-2xx and predicate failures do NOT
    retry (a 404 is not a flake, it's a real failure). The probe converts
    every terminal outcome into a ``ProbeResult`` so the caller's state
    machine sees one shape regardless of what went wrong.
    """
    start = time.monotonic()
    delays = list(retry_delays) + [None]  # None terminates the loop on the last try
    last_status: int | None = None
    last_detail: str = ""
    last_body: str = ""
    for attempt, delay in enumerate(delays):
        try:
            response = client.request(
                check.method,
                check.url,
                timeout=PROBE_TIMEOUT_SEC,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            last_detail = f"transport_error: {type(exc).__name__}: {exc}"
            if delay is None:
                break
            sleeper(delay)
            continue

        last_status = response.status_code
        last_body = response.text if check.fetch_body else ""

        if response.status_code != check.expect_status:
            # 5xx is transient by convention; retry. Anything else is terminal.
            if 500 <= response.status_code < 600 and delay is not None:
                last_detail = f"http_{response.status_code}"
                sleeper(delay)
                continue
            last_detail = (
                f"status_mismatch: got {response.status_code}, "
                f"expected {check.expect_status}"
            )
            break

        # Status matched. Run the body predicate.
        try:
            marker_detail = check.marker_predicate(last_body)
        except Exception as exc:  # noqa: BLE001 - never let a predicate bug silence the probe
            marker_detail = f"predicate_raised: {type(exc).__name__}: {exc}"
        if marker_detail:
            last_detail = marker_detail
            break

        # All passed.
        return ProbeResult(
            name=check.name,
            ok=True,
            elapsed_ms=int((time.monotonic() - start) * 1000),
            status_code=last_status,
            detail="",
            body_snippet=last_body[:600],
        )

    return ProbeResult(
        name=check.name,
        ok=False,
        elapsed_ms=int((time.monotonic() - start) * 1000),
        status_code=last_status,
        detail=last_detail or "unknown_failure",
        body_snippet=last_body[:600],
    )


def run_probe(
    checks: list[ProbeCheck],
    *,
    client: httpx.Client,
    retry_delays: Iterable[float] = PROBE_RETRY_DELAYS_SEC,
    sleeper: Callable[[float], None] = time.sleep,
) -> ProbeReport:
    """Run every check; build the aggregate report. Always returns a report."""
    started = _now_iso()
    results: list[ProbeResult] = []
    failure_detail = ""
    for check in checks:
        result = _run_one_check(
            check, client=client, retry_delays=retry_delays, sleeper=sleeper
        )
        results.append(result)
        if not result.ok and not failure_detail:
            # First-failed-check wins for the alert subject; the full set is
            # in the log file for forensic context.
            failure_detail = f"{result.name}: {result.detail}"
    overall = "green" if not failure_detail else "red"
    return ProbeReport(
        schema_version=REPORT_SCHEMA_VERSION,
        started_at_utc=started,
        finished_at_utc=_now_iso(),
        overall_status=overall,
        failure_detail=failure_detail,
        checks=results,
    )


# --------------------------------------------------------------------------- #
# State machine                                                               #
# --------------------------------------------------------------------------- #


def decide_alert(
    *,
    prev: ProbeState,
    report: ProbeReport,
    now_epoch: float,
    dedup_window_sec: int = DEDUP_WINDOW_SEC,
    hostname: str = "resemblio-prod-01",
) -> AlertDecision:
    """Pure function: prev state + this tick -> alert decision.

    No IO. Tests parametrize the inputs and assert the output. Mirrors the
    ENC ``decide_alert()`` shape so any future operator who has read the ENC
    runbook can pattern-match the rules.
    """
    new_status = report.overall_status
    new_detail = report.failure_detail

    # green -> green: cheapest case, nothing to send.
    if prev.last_status == "green" and new_status == "green":
        return AlertDecision(False, "steady_green", subject="", body="")

    # red -> green: recovery (also covers unknown -> green on a clean first
    # tick, which we DO NOT alert on; suppress that case below).
    if prev.last_status == "red" and new_status == "green":
        subject = f"[Resemblio] RECOVERED on {hostname}"
        body = (
            f"Recovered at {report.finished_at_utc}.\n\n"
            f"Prior failure: {prev.last_failure_detail}\n"
        )
        return AlertDecision(True, "recovered", subject=subject, body=body)

    # unknown -> green on a first-ever tick is steady_green by another name.
    if new_status == "green":
        return AlertDecision(False, "steady_green", subject="", body="")

    # From here on, new_status == "red".
    if prev.last_status != "red":
        # New failure, including unknown -> red.
        subject = f"[Resemblio] DOWN on {hostname}: {_truncate(new_detail, 80)}"
        body = (
            f"Detected at {report.finished_at_utc}.\n"
            f"Reason: new_failure\n"
            f"Detail: {new_detail}\n"
        )
        return AlertDecision(True, "new_failure", subject=subject, body=body)

    # prev red, new red. Two sub-cases.
    if prev.last_failure_detail != new_detail:
        subject = f"[Resemblio] FAILURE MODE CHANGED on {hostname}: {_truncate(new_detail, 80)}"
        body = (
            f"Detected at {report.finished_at_utc}.\n"
            f"Reason: failure_mode_changed\n"
            f"Prior detail: {prev.last_failure_detail}\n"
            f"New detail: {new_detail}\n"
        )
        return AlertDecision(True, "failure_mode_changed", subject=subject, body=body)

    # Same red, same detail. Re-nag only past the dedup window.
    last_alert_dt = _parse_iso(prev.last_alert_sent_at)
    if last_alert_dt is None:
        # We somehow lost the alert timestamp; do not flood, but do nag once.
        subject = f"[Resemblio] STILL DOWN on {hostname}: {_truncate(new_detail, 80)}"
        body = (
            f"Detected at {report.finished_at_utc}.\n"
            f"Reason: no_prior_alert\n"
            f"Detail: {new_detail}\n"
        )
        return AlertDecision(True, "no_prior_alert", subject=subject, body=body)
    elapsed = now_epoch - last_alert_dt.timestamp()
    if elapsed >= dedup_window_sec:
        subject = f"[Resemblio] STILL DOWN on {hostname}: {_truncate(new_detail, 80)}"
        body = (
            f"Detected at {report.finished_at_utc}.\n"
            f"Reason: renag (down for {int(elapsed)}s)\n"
            f"Detail: {new_detail}\n"
        )
        return AlertDecision(True, "renag", subject=subject, body=body)
    return AlertDecision(False, "suppressed_dedup", subject="", body="")


def _truncate(value: str, limit: int) -> str:
    """One-line truncation suitable for an email subject line."""
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


# --------------------------------------------------------------------------- #
# Persistence                                                                 #
# --------------------------------------------------------------------------- #


def load_state(state_path: Path) -> ProbeState:
    """Load state from disk. Return a fresh state if missing/corrupt/stale."""
    if not state_path.exists():
        return ProbeState.fresh()
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("state file %s unreadable; starting fresh", state_path)
        return ProbeState.fresh()
    schema = raw.get("schema_version")
    if schema != STATE_SCHEMA_VERSION:
        logger.warning(
            "state schema %r != %r; starting fresh", schema, STATE_SCHEMA_VERSION
        )
        return ProbeState.fresh()
    updated = _parse_iso(raw.get("updated_at", ""))
    if updated is not None:
        age = _dt.datetime.now(_dt.timezone.utc) - updated
        if age.total_seconds() > STATE_FRESHNESS_SEC:
            logger.warning(
                "state file is %.0fs old; starting fresh (threshold %ds)",
                age.total_seconds(),
                STATE_FRESHNESS_SEC,
            )
            return ProbeState.fresh()
    return ProbeState(
        schema_version=schema,
        last_status=raw.get("last_status", "unknown"),
        last_failure_detail=raw.get("last_failure_detail", ""),
        last_alert_sent_at=raw.get("last_alert_sent_at", ""),
        consecutive_red=int(raw.get("consecutive_red", 0)),
        updated_at=raw.get("updated_at", _now_iso()),
    )


def save_state(state: ProbeState, state_path: Path) -> None:
    """Atomic write: tmp file + rename. Mirrors check_and_alert.sh."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    payload = dataclasses.asdict(state)
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(tmp, state_path)


def append_report_log(report: ProbeReport, log_dir: Path) -> Path:
    """Append the per-tick report to the day's log file. Returns the path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"synthetic-probe-{report.started_at_utc[:10]}.log"
    payload: dict[str, Any] = dataclasses.asdict(report)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True))
        fh.write("\n")
    return log_path


# --------------------------------------------------------------------------- #
# Alert sink (Resend) - injectable for tests                                  #
# --------------------------------------------------------------------------- #


def send_alert_via_resend(
    *,
    subject: str,
    body: str,
    api_key: str,
    from_address: str = "alerts@resemblio.com",
    to_address: str = "frank@optsus.com",
    client: httpx.Client | None = None,
) -> bool:
    """POST one transactional email to Resend. Return True on 2xx.

    Network errors and non-2xx are logged but do not raise; the state machine
    still updates so a flaky Resend outage does not freeze the probe. The
    NEXT tick will detect the still-red state and may attempt to alert again
    if dedup conditions allow.
    """
    payload = {
        "from": from_address,
        "to": [to_address],
        "subject": subject,
        "text": body,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=15.0)
    try:
        for attempt, delay in enumerate(PROBE_RETRY_DELAYS_SEC):
            try:
                resp = client.post(
                    "https://api.resend.com/emails", json=payload, headers=headers
                )
            except httpx.HTTPError as exc:
                logger.warning("resend transport error attempt=%d: %s", attempt, exc)
                time.sleep(delay)
                continue
            if 200 <= resp.status_code < 300:
                return True
            logger.warning(
                "resend non-2xx attempt=%d status=%d body=%s",
                attempt,
                resp.status_code,
                resp.text[:300],
            )
            time.sleep(delay)
        return False
    finally:
        if owns_client and client is not None:
            client.close()


# --------------------------------------------------------------------------- #
# Top-level tick                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class TickOutcome:
    """Return shape from ``run_tick``; the CLI uses this to set exit code."""

    report: ProbeReport
    decision: AlertDecision
    alert_sent: bool
    state_after: ProbeState
    log_path: Path


def run_tick(
    *,
    checks: list[ProbeCheck],
    client: httpx.Client,
    state_path: Path,
    log_dir: Path,
    alert_sink: Callable[[str, str], bool],
    hostname: str = "resemblio-prod-01",
    now_epoch: float | None = None,
) -> TickOutcome:
    """Run one full tick: probe, decide, alert, persist. Pure-ish (state IO only)."""
    if now_epoch is None:
        now_epoch = time.time()
    # When the caller pins now_epoch (tests, time-travel scenarios), pin the
    # persisted ISO strings to the same instant so dedup math stays internally
    # consistent. In production now_epoch is unset and both clocks are
    # wall-clock UTC anyway.
    now_iso_pinned = _dt.datetime.fromtimestamp(now_epoch, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    report = run_probe(checks, client=client)
    prev_state = load_state(state_path)
    decision = decide_alert(
        prev=prev_state, report=report, now_epoch=now_epoch, hostname=hostname
    )
    alert_sent = False
    if decision.should_alert:
        try:
            alert_sent = alert_sink(decision.subject, decision.body)
        except Exception as exc:  # noqa: BLE001
            logger.exception("alert sink raised; treating as not-sent: %s", exc)
            alert_sent = False

    next_state = ProbeState(
        schema_version=STATE_SCHEMA_VERSION,
        last_status=report.overall_status,
        last_failure_detail=report.failure_detail,
        last_alert_sent_at=(
            now_iso_pinned if alert_sent else prev_state.last_alert_sent_at
        ),
        consecutive_red=(
            prev_state.consecutive_red + 1 if report.overall_status == "red" else 0
        ),
        updated_at=now_iso_pinned,
    )
    save_state(next_state, state_path)
    log_path = append_report_log(report, log_dir)
    logger.info(
        "tick status=%s decision=%s alert_sent=%s log=%s",
        report.overall_status,
        decision.reason,
        alert_sent,
        log_path,
    )
    return TickOutcome(
        report=report,
        decision=decision,
        alert_sent=alert_sent,
        state_after=next_state,
        log_path=log_path,
    )
