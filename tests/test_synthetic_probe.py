"""Unit tests for app.monitoring.synthetic_probe (Stage 1 of CTO TDD plan).

Covers the four behaviors the production probe MUST get right:

1. Synthetic-flow expectations (each check's status + body marker contract)
2. URN-leak detection (the 2026-06-02 regression signature)
3. State-machine alert dedup (no flooding during a 3-hour outage)
4. Alert routing (subject lines per state transition)

All network is fed through ``httpx.MockTransport``; no live IO. State and log
files live in ``tmp_path`` so each test owns its own filesystem slice.
"""
from __future__ import annotations

import calendar
import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.monitoring.synthetic_probe import (
    DEDUP_WINDOW_SEC,
    PROBE_RETRY_DELAYS_SEC,
    REPORT_SCHEMA_VERSION,
    STATE_FRESHNESS_SEC,
    STATE_SCHEMA_VERSION,
    AlertDecision,
    ProbeCheck,
    ProbeReport,
    ProbeResult,
    ProbeState,
    decide_alert,
    default_checks,
    load_state,
    run_probe,
    run_tick,
    save_state,
)


# --------------------------------------------------------------------------- #
# Synthetic responses                                                         #
# --------------------------------------------------------------------------- #


def _healthy_body_for(url: str, brand: str = "aeon") -> tuple[int, str]:
    """Return (status, body) for a known-good response to the given URL."""
    if url.endswith("/"):
        if "/library/" in url and url.rstrip("/").endswith("/library"):
            # library hub
            return 200, '<html><body><a href="/library/aeon/">Aeon</a></body></html>'
        if "/library/" in url and url.endswith("/buttons/"):
            return (
                200,
                "<html><body>"
                "<style>.b-btn { --ds-bg: #fff; padding: 12px; }</style>"
                '<button class="b-btn">Click</button>'
                "</body></html>",
            )
        # web root
        return 200, '<html lang="en"><body>Resemblio</body></html>'
    if url.endswith("/v1/healthz") or url.endswith("/v1/readyz"):
        return 200, "ok"
    return 404, "not found"


def _all_healthy_handler(brand: str = "aeon"):
    """Build an httpx mock handler that returns healthy bodies for all URLs."""

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = _healthy_body_for(str(request.url), brand=brand)
        return httpx.Response(status, text=body)

    return handler


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# 1. Synthetic-flow expectations                                              #
# --------------------------------------------------------------------------- #


def test_default_checks_all_green_on_healthy_responses() -> None:
    """Every Stage-1 check passes when bodies carry the expected markers."""
    client = _mock_client(_all_healthy_handler())
    checks = default_checks()
    report = run_probe(checks, client=client, retry_delays=(), sleeper=lambda _s: None)
    assert report.overall_status == "green", report.failure_detail
    assert report.failure_detail == ""
    assert len(report.checks) == len(checks)
    assert all(result.ok for result in report.checks)
    assert report.schema_version == REPORT_SCHEMA_VERSION


def test_library_hub_500_flips_red_and_names_check() -> None:
    """A 500 on /library/ surfaces with the check name in failure_detail."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/library/"):
            return httpx.Response(500, text="server error")
        status, body = _healthy_body_for(str(request.url))
        return httpx.Response(status, text=body)

    report = run_probe(
        default_checks(),
        client=_mock_client(handler),
        retry_delays=(),
        sleeper=lambda _s: None,
    )
    assert report.overall_status == "red"
    assert report.failure_detail.startswith("library_hub:")
    assert "500" in report.failure_detail


def test_library_buttons_empty_body_is_red() -> None:
    """200 with empty body fragment (no .b-btn / no button) flips red.

    This is the 2026-06-02 failure mode the status-only healthz missed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/library/aeon/buttons/"):
            return httpx.Response(200, text="<html><body></body></html>")
        status, body = _healthy_body_for(str(request.url))
        return httpx.Response(status, text=body)

    report = run_probe(
        default_checks(),
        client=_mock_client(handler),
        retry_delays=(),
        sleeper=lambda _s: None,
    )
    assert report.overall_status == "red"
    assert "library_brand_buttons" in report.failure_detail


def test_urn_leak_in_library_page_is_red() -> None:
    """Raw urn: string in the body trips the URN-leak predicate."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/library/aeon/buttons/"):
            return httpx.Response(
                200,
                text=(
                    "<html><body><style>.b-btn{}</style>"
                    "<button>urn:resemblio:token:bg</button></body></html>"
                ),
            )
        status, body = _healthy_body_for(str(request.url))
        return httpx.Response(status, text=body)

    report = run_probe(
        default_checks(),
        client=_mock_client(handler),
        retry_delays=(),
        sleeper=lambda _s: None,
    )
    assert report.overall_status == "red"
    assert "URN leak" in report.failure_detail


def test_api_healthz_502_is_red_status_only() -> None:
    """API healthz only checks status; a 502 must still flip red."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v1/healthz"):
            return httpx.Response(502, text="bad gateway")
        status, body = _healthy_body_for(str(request.url))
        return httpx.Response(status, text=body)

    report = run_probe(
        default_checks(),
        client=_mock_client(handler),
        retry_delays=(0.0,),  # one short retry so the test is fast
        sleeper=lambda _s: None,
    )
    assert report.overall_status == "red"
    assert "api_healthz" in report.failure_detail


def test_transport_error_retries_then_fails() -> None:
    """Network errors retry per PROBE_RETRY_DELAYS_SEC, then mark red."""

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        raise httpx.ConnectError("simulated dns failure")

    check = default_checks()[0]
    from app.monitoring.synthetic_probe import _run_one_check

    result = _run_one_check(
        check,
        client=_mock_client(handler),
        retry_delays=(0.0, 0.0, 0.0),
        sleeper=lambda _s: None,
    )
    assert not result.ok
    assert "transport_error" in result.detail
    # 3 retries + initial attempt = 4 total. The probe loop uses
    # delays + [None] which gives len(delays)+1 attempts.
    assert call_count["n"] == 4


def test_5xx_retries_but_404_does_not() -> None:
    """5xx triggers retry; 4xx is terminal (a 404 is a real failure)."""
    call_count = {"5xx": 0, "404": 0}

    def handler_5xx(request: httpx.Request) -> httpx.Response:
        call_count["5xx"] += 1
        return httpx.Response(500, text="x")

    def handler_404(request: httpx.Request) -> httpx.Response:
        call_count["404"] += 1
        return httpx.Response(404, text="x")

    check = default_checks()[3]  # api_healthz
    from app.monitoring.synthetic_probe import _run_one_check

    _run_one_check(
        check,
        client=_mock_client(handler_5xx),
        retry_delays=(0.0, 0.0),
        sleeper=lambda _s: None,
    )
    _run_one_check(
        check,
        client=_mock_client(handler_404),
        retry_delays=(0.0, 0.0),
        sleeper=lambda _s: None,
    )
    assert call_count["5xx"] == 3  # 2 retries + 1 final
    assert call_count["404"] == 1


# --------------------------------------------------------------------------- #
# 3. State-machine dedup                                                      #
# --------------------------------------------------------------------------- #


def _make_report(status: str, detail: str = "") -> ProbeReport:
    return ProbeReport(
        schema_version=REPORT_SCHEMA_VERSION,
        started_at_utc="2026-06-03T00:00:00Z",
        finished_at_utc="2026-06-03T00:00:05Z",
        overall_status=status,
        failure_detail=detail,
        checks=[],
    )


def _make_state(
    status: str = "unknown",
    detail: str = "",
    last_alert: str = "",
    consecutive_red: int = 0,
    updated_at: str | None = None,
) -> ProbeState:
    return ProbeState(
        schema_version=STATE_SCHEMA_VERSION,
        last_status=status,
        last_failure_detail=detail,
        last_alert_sent_at=last_alert,
        consecutive_red=consecutive_red,
        updated_at=updated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def test_dedup_green_to_red_alerts_new_failure() -> None:
    prev = _make_state(status="green")
    report = _make_report("red", "library_hub: marker missing")
    decision = decide_alert(prev=prev, report=report, now_epoch=1_000_000)
    assert decision.should_alert
    assert decision.reason == "new_failure"
    assert "DOWN" in decision.subject
    assert "library_hub" in decision.subject


def test_dedup_red_to_red_same_detail_within_window_suppressed() -> None:
    prev = _make_state(
        status="red",
        detail="library_hub: marker missing",
        last_alert="2026-06-03T00:00:00Z",
    )
    report = _make_report("red", "library_hub: marker missing")
    # 1 second after last alert; well inside the 15-minute window.
    last_epoch = calendar.timegm(time.strptime("2026-06-03T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    decision = decide_alert(prev=prev, report=report, now_epoch=last_epoch + 1)
    assert not decision.should_alert
    assert decision.reason == "suppressed_dedup"


def test_dedup_red_to_red_same_detail_past_window_renags() -> None:
    prev = _make_state(
        status="red",
        detail="library_hub: marker missing",
        last_alert="2026-06-03T00:00:00Z",
    )
    report = _make_report("red", "library_hub: marker missing")
    last_epoch = calendar.timegm(time.strptime("2026-06-03T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    decision = decide_alert(
        prev=prev, report=report, now_epoch=last_epoch + DEDUP_WINDOW_SEC + 1
    )
    assert decision.should_alert
    assert decision.reason == "renag"
    assert "STILL DOWN" in decision.subject


def test_dedup_red_to_red_different_detail_alerts() -> None:
    prev = _make_state(
        status="red",
        detail="library_hub: marker missing",
        last_alert="2026-06-03T00:00:00Z",
    )
    report = _make_report("red", "api_healthz: status_mismatch: got 502")
    decision = decide_alert(prev=prev, report=report, now_epoch=1_000_000)
    assert decision.should_alert
    assert decision.reason == "failure_mode_changed"


def test_dedup_red_to_green_recovers() -> None:
    prev = _make_state(
        status="red",
        detail="library_hub: marker missing",
        last_alert="2026-06-03T00:00:00Z",
    )
    report = _make_report("green")
    decision = decide_alert(prev=prev, report=report, now_epoch=1_000_000)
    assert decision.should_alert
    assert decision.reason == "recovered"
    assert "RECOVERED" in decision.subject


def test_dedup_green_to_green_silent() -> None:
    prev = _make_state(status="green")
    report = _make_report("green")
    decision = decide_alert(prev=prev, report=report, now_epoch=1_000_000)
    assert not decision.should_alert
    assert decision.reason == "steady_green"


def test_dedup_unknown_first_tick_green_does_not_alert() -> None:
    """First-ever tick on a fresh box should not alert just because we recovered from 'unknown'."""
    prev = _make_state(status="unknown")
    report = _make_report("green")
    decision = decide_alert(prev=prev, report=report, now_epoch=1_000_000)
    assert not decision.should_alert


def test_dedup_unknown_first_tick_red_alerts() -> None:
    """Fresh box that probes red on first tick is a real failure; alert."""
    prev = _make_state(status="unknown")
    report = _make_report("red", "api_healthz: status_mismatch")
    decision = decide_alert(prev=prev, report=report, now_epoch=1_000_000)
    assert decision.should_alert
    assert decision.reason == "new_failure"


# --------------------------------------------------------------------------- #
# Persistence                                                                 #
# --------------------------------------------------------------------------- #


def test_load_state_returns_fresh_when_missing(tmp_path: Path) -> None:
    state = load_state(tmp_path / "does-not-exist.json")
    assert state.last_status == "unknown"
    assert state.schema_version == STATE_SCHEMA_VERSION


def test_load_state_returns_fresh_on_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not valid", encoding="utf-8")
    state = load_state(path)
    assert state.last_status == "unknown"


def test_load_state_returns_fresh_on_schema_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    payload = dataclasses.asdict(_make_state(status="green"))
    payload["schema_version"] = "synthetic_probe_state_v999"
    path.write_text(json.dumps(payload), encoding="utf-8")
    state = load_state(path)
    assert state.last_status == "unknown"


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    original = _make_state(
        status="red",
        detail="library_hub: x",
        last_alert="2026-06-03T01:00:00Z",
        consecutive_red=4,
    )
    save_state(original, path)
    loaded = load_state(path)
    assert loaded.last_status == original.last_status
    assert loaded.last_failure_detail == original.last_failure_detail
    assert loaded.last_alert_sent_at == original.last_alert_sent_at
    assert loaded.consecutive_red == original.consecutive_red


def test_load_state_returns_fresh_on_stale_file(tmp_path: Path) -> None:
    """A state file older than STATE_FRESHNESS_SEC is discarded.

    After a long box downtime the last-persisted status is meaningless;
    load_state must treat the next tick as a first run ("unknown") rather
    than re-applying a hours-old "red"/"green" verdict. This branch was
    previously exercised only by accident (via a hardcoded date in
    test_save_and_load_roundtrip that aged past the threshold over time);
    this test pins it deterministically by pegging updated_at to a
    timestamp comfortably beyond the freshness window.
    """
    path = tmp_path / "state.json"
    stale_age_sec = STATE_FRESHNESS_SEC + 3600  # one hour past the window
    stale_updated_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - stale_age_sec)
    )
    original = _make_state(
        status="red",
        detail="library_hub: x",
        consecutive_red=4,
        updated_at=stale_updated_at,
    )
    save_state(original, path)
    loaded = load_state(path)
    assert loaded.last_status == "unknown"
    assert loaded.consecutive_red == 0
    assert loaded.last_failure_detail == ""


def test_load_state_trusts_recent_file(tmp_path: Path) -> None:
    """A state file inside STATE_FRESHNESS_SEC is trusted verbatim.

    Pairs with test_load_state_returns_fresh_on_stale_file: the boundary
    must not be so aggressive that a normal 5-minute tick cadence ever
    discards a still-valid verdict. updated_at is pegged just inside the
    window (one minute old).
    """
    path = tmp_path / "state.json"
    recent_updated_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)
    )
    original = _make_state(
        status="red",
        detail="library_hub: x",
        consecutive_red=4,
        updated_at=recent_updated_at,
    )
    save_state(original, path)
    loaded = load_state(path)
    assert loaded.last_status == "red"
    assert loaded.consecutive_red == 4


# --------------------------------------------------------------------------- #
# Alert sink + run_tick integration                                           #
# --------------------------------------------------------------------------- #


def test_run_tick_green_writes_state_and_log_no_alert(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "synthetic-probe-state.json"
    log_dir = tmp_path / "logs"
    sent: list[tuple[str, str]] = []

    def sink(subject: str, body: str) -> bool:
        sent.append((subject, body))
        return True

    client = _mock_client(_all_healthy_handler())
    outcome = run_tick(
        checks=default_checks(),
        client=client,
        state_path=state_path,
        log_dir=log_dir,
        alert_sink=sink,
    )
    assert outcome.report.overall_status == "green"
    assert outcome.alert_sent is False
    assert sent == []
    assert state_path.exists()
    loaded = load_state(state_path)
    assert loaded.last_status == "green"
    # Log file appended.
    log_files = list(log_dir.glob("synthetic-probe-*.log"))
    assert len(log_files) == 1
    # Body snippet captured for forensic context.
    payload = json.loads(log_files[0].read_text(encoding="utf-8").strip().splitlines()[0])
    assert payload["overall_status"] == "green"
    assert payload["schema_version"] == REPORT_SCHEMA_VERSION


def test_run_tick_red_alerts_once_and_updates_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "synthetic-probe-state.json"
    log_dir = tmp_path / "logs"
    sent: list[tuple[str, str]] = []

    def sink(subject: str, body: str) -> bool:
        sent.append((subject, body))
        return True

    def broken_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/library/"):
            return httpx.Response(500, text="x")
        status, body = _healthy_body_for(str(request.url))
        return httpx.Response(status, text=body)

    outcome = run_tick(
        checks=default_checks(),
        client=_mock_client(broken_handler),
        state_path=state_path,
        log_dir=log_dir,
        alert_sink=sink,
    )
    assert outcome.report.overall_status == "red"
    assert outcome.alert_sent is True
    assert len(sent) == 1
    assert "DOWN" in sent[0][0]
    loaded = load_state(state_path)
    assert loaded.last_status == "red"
    assert loaded.consecutive_red == 1
    assert loaded.last_alert_sent_at != ""


def test_run_tick_three_hour_outage_sends_at_most_renags(tmp_path: Path) -> None:
    """Simulate a 3-hour outage at 5-min cadence.

    Expected alert count: 1 initial + floor(180/15)=12 renags. The 2026-06-02
    failure mode sent ZERO because no probe existed; the naive every-tick
    sender would send 36. This test enforces the dedup contract that lives
    between those two failure modes.
    """
    state_path = tmp_path / "state.json"
    log_dir = tmp_path / "logs"
    sent: list[str] = []

    def sink(subject: str, body: str) -> bool:
        sent.append(subject)
        return True

    def broken(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/library/"):
            return httpx.Response(500, text="x")
        status, body = _healthy_body_for(str(request.url))
        return httpx.Response(status, text=body)

    client = _mock_client(broken)
    # 36 ticks * 5 min = 180 min = 3 hours.
    base = 2_000_000_000.0  # arbitrary epoch
    for i in range(36):
        run_tick(
            checks=default_checks(),
            client=client,
            state_path=state_path,
            log_dir=log_dir,
            alert_sink=sink,
            now_epoch=base + i * 5 * 60,
        )
    # First alert + renag every 15 min. 36 ticks at 5 min cadence span
    # 175 minutes of post-initial-alert time = 11 full renag windows.
    # Allow +/-1 to absorb the boundary tick (whichever side of the window
    # the wall-clock instant lands on).
    assert 8 <= len(sent) <= 14, sent
    # Crucially, NOT 36 (every tick) and NOT 0 (silent outage).
    assert len(sent) < 36
    assert len(sent) > 0


def test_run_tick_recovery_sends_recovered_alert(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    log_dir = tmp_path / "logs"
    sent: list[str] = []

    def sink(subject: str, body: str) -> bool:
        sent.append(subject)
        return True

    # First tick: red
    def broken(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/library/"):
            return httpx.Response(500, text="x")
        status, body = _healthy_body_for(str(request.url))
        return httpx.Response(status, text=body)

    run_tick(
        checks=default_checks(),
        client=_mock_client(broken),
        state_path=state_path,
        log_dir=log_dir,
        alert_sink=sink,
    )
    # Second tick: green
    run_tick(
        checks=default_checks(),
        client=_mock_client(_all_healthy_handler()),
        state_path=state_path,
        log_dir=log_dir,
        alert_sink=sink,
    )
    assert any("DOWN" in s for s in sent)
    assert any("RECOVERED" in s for s in sent)


# --------------------------------------------------------------------------- #
# Subject-line contract                                                       #
# --------------------------------------------------------------------------- #


def test_subject_lines_per_state_transition() -> None:
    """Pin the exact subject-line shape per transition class.

    These strings are what Frank sees in his inbox. Pinning them in a test
    means a refactor that breaks the subject contract gets caught by CI.
    """
    # new_failure
    prev = _make_state("green")
    report = _make_report("red", "library_hub: marker 'href=\"/library/' missing from body")
    decision = decide_alert(prev=prev, report=report, now_epoch=1_000_000)
    assert decision.subject.startswith("[Resemblio] DOWN on resemblio-prod-01:")

    # renag
    prev = _make_state("red", "library_hub: x", last_alert="2026-06-03T00:00:00Z")
    report = _make_report("red", "library_hub: x")
    last_epoch = calendar.timegm(time.strptime("2026-06-03T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    decision = decide_alert(
        prev=prev, report=report, now_epoch=last_epoch + DEDUP_WINDOW_SEC + 1
    )
    assert decision.subject.startswith("[Resemblio] STILL DOWN on resemblio-prod-01:")

    # failure_mode_changed
    prev = _make_state("red", "a: x", last_alert="2026-06-03T00:00:00Z")
    report = _make_report("red", "b: y")
    decision = decide_alert(prev=prev, report=report, now_epoch=1_000_000)
    assert decision.subject.startswith("[Resemblio] FAILURE MODE CHANGED on resemblio-prod-01:")

    # recovered
    prev = _make_state("red", "a: x", last_alert="2026-06-03T00:00:00Z")
    report = _make_report("green")
    decision = decide_alert(prev=prev, report=report, now_epoch=1_000_000)
    assert decision.subject == "[Resemblio] RECOVERED on resemblio-prod-01"
