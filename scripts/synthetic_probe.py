"""CLI wrapper for the Resemblio synthetic prod probe (Stage 1).

Purpose
-------
Run the synthetic user-flow probe once and dispatch a Resend alert if the
state machine says we should. Designed to be triggered every 5 minutes by
``resemblio-synthetic-probe.timer`` on ``resemblio-prod-01``.

Logic lives in ``app.monitoring.synthetic_probe``; this script is a thin
wrapper that wires environment + filesystem to the pure functions there.
The split keeps the unit tests at
``tests/test_synthetic_probe.py`` import-clean (no sys.path tricks) while
the CLI stays operable from the box's venv per the OPS conventions in
``code/api/OPS.md`` Section 4.

Usage
-----
::

    /opt/resemblio-api/venv/bin/python -m scripts.synthetic_probe
    /opt/resemblio-api/venv/bin/python -m scripts.synthetic_probe --dry-run

Environment
-----------
``RESEND_API_KEY``         required (else the probe runs and logs but cannot alert)
``RESEMBLIO_PROBE_STATE_DIR``  optional override for the state-file dir
                                (default ``/var/lib/resemblio``)
``RESEMBLIO_PROBE_LOG_DIR``    optional override for the log dir
                                (default ``/var/log/resemblio``)
``RESEMBLIO_PROBE_WEB_ORIGIN`` optional override (default
                                ``https://resemblio.com``); useful for staging
``RESEMBLIO_PROBE_API_ORIGIN`` optional override (default
                                ``https://api.resemblio.com``)
``RESEMBLIO_PROBE_BRAND``      optional override for the Library brand the
                                deep-page check exercises (default ``aeon``)

Exit codes
----------
``0`` always when the tick completes (red or green). The state machine
already handles "what to do about red" via the alert sink. A non-zero exit
would cause the systemd unit to mark the timer as failed, which is the wrong
signal: the timer doing its job IS the success criterion, even on a red
probe outcome. Mirrors the ENC ``check_and_alert.sh`` convention.

Failure to write state, or a crash in the probe module itself, exits 2 so
the systemd unit surfaces the operator-actionable bug.

Schema
------
``schema_version=synthetic_probe_cli_v1`` (in the printed summary block).

Quality floor notes
-------------------
- Logger configured; this runs unattended on a 5-min timer.
- Network retries via the underlying probe module's ``PROBE_RETRY_DELAYS_SEC``.
- All path constants flow from environment with safe defaults.
- Single dashes only; per workspace CLAUDE.md.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import httpx

from app.monitoring.synthetic_probe import (
    DEFAULT_LIBRARY_BRAND,
    DEFAULT_LOG_DIR,
    DEFAULT_STATE_DIR,
    default_checks,
    run_tick,
    send_alert_via_resend,
)

SUMMARY_SCHEMA_VERSION = "synthetic_probe_cli_v1"

logger = logging.getLogger("resemblio.synthetic_probe.cli")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Resemblio synthetic prod probe once."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe and log but never call Resend, even on a state transition.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 on tick completion, 2 on operator-visible bug."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = _build_arg_parser().parse_args(argv)

    state_dir = Path(os.environ.get("RESEMBLIO_PROBE_STATE_DIR", str(DEFAULT_STATE_DIR)))
    log_dir = Path(os.environ.get("RESEMBLIO_PROBE_LOG_DIR", str(DEFAULT_LOG_DIR)))
    web_origin = os.environ.get("RESEMBLIO_PROBE_WEB_ORIGIN", "https://resemblio.com")
    api_origin = os.environ.get("RESEMBLIO_PROBE_API_ORIGIN", "https://api.resemblio.com")
    brand = os.environ.get("RESEMBLIO_PROBE_BRAND", DEFAULT_LIBRARY_BRAND)
    resend_api_key = os.environ.get("RESEND_API_KEY", "")

    state_path = state_dir / "synthetic-probe-state.json"
    checks = default_checks(
        web_origin=web_origin, api_origin=api_origin, library_brand=brand
    )

    def _alert_sink(subject: str, body: str) -> bool:
        """Bound alert dispatch closure; honors --dry-run and missing key."""
        if args.dry_run:
            logger.info("dry-run: would alert subject=%r", subject)
            return False
        if not resend_api_key:
            logger.error(
                "alert would fire (subject=%r) but RESEND_API_KEY is unset; "
                "operator: set it via /etc/resemblio/probe.env",
                subject,
            )
            return False
        return send_alert_via_resend(
            subject=subject, body=body, api_key=resend_api_key
        )

    try:
        with httpx.Client(timeout=15.0) as client:
            outcome = run_tick(
                checks=checks,
                client=client,
                state_path=state_path,
                log_dir=log_dir,
                alert_sink=_alert_sink,
            )
    except Exception as exc:  # noqa: BLE001 - top-level guard, log + exit 2
        logger.exception("synthetic probe crashed: %s", exc)
        return 2

    logger.info(
        "%s status=%s reason=%s alert_sent=%s log=%s",
        SUMMARY_SCHEMA_VERSION,
        outcome.report.overall_status,
        outcome.decision.reason,
        outcome.alert_sent,
        outcome.log_path,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
