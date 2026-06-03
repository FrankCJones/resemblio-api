"""CLI wrapper for the Stage 9 refund-reconciliation daily check.

Purpose
-------
Run the pure-data reconciliation in ``app.refund_reconcile`` against the
prod Postgres for yesterday's UTC window. On non-zero drift, dispatch a
Resend alert to ``frank@optsus.com`` so a stranded customer charge
surfaces within 24 hours rather than via a support email.

Closes Stage 9 of the 2026-06-03 back-on-track TDD plan (failure
inventory item #18, "no observability that proves refund-on-failure
actually fires for every fail").

Usage
-----
::

    /opt/resemblio-api/venv/bin/python -m scripts.refund_reconcile
    /opt/resemblio-api/venv/bin/python -m scripts.refund_reconcile --dry-run
    /opt/resemblio-api/venv/bin/python -m scripts.refund_reconcile --date 2026-06-01

``--dry-run`` runs the reconciliation and logs the report but never calls
Resend, even if drift is detected. Useful for backfilling history or for
operator-driven catch-up after a Resend outage.

``--date`` (YYYY-MM-DD, UTC) overrides the default "yesterday". Useful
for re-checking a specific day after a manual repair.

Environment
-----------
``RESEND_API_KEY``        required for alert dispatch (else logs and
                          exits 0; drift is still surfaced in stdout +
                          /var/log/resemblio/refund-reconcile.log)
``RESEMBLIO_DB_URL``      required Postgres DSN; same value the API
                          server reads via ``app.config``
``RESEMBLIO_RECONCILE_LOG_DIR``
                          optional override (default ``/var/log/resemblio``)

Exit codes
----------
``0`` always when the reconciliation completes, regardless of drift.
Drift is an OPERATIONAL signal, not a script failure; the alert is the
proper escalation surface. A non-zero exit would mark the systemd timer
as failed and freeze the cadence, which is the wrong response to a real
drift event (we want tomorrow's check to fire normally).

``2`` only when the script itself crashes (DB unreachable, unhandled
exception). Matches the synthetic-probe convention.

Schema
------
``schema_version=refund_reconcile_cli_v1`` (in stdout summary block).

Quality floor notes
-------------------
- Logger configured; runs unattended on a 24h timer.
- Network retries on Resend handled inside ``send_alert_via_resend``.
- All thresholds and constants live in ``app.refund_reconcile``.
- Single dashes only; per workspace CLAUDE.md.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date as _date_type
from datetime import datetime, timezone
from pathlib import Path

# scripts/ folder self-inserts its parent so `from app...` resolves
# without requiring `pip install -e .` in dev.
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))

import httpx  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.monitoring.synthetic_probe import send_alert_via_resend  # noqa: E402
from app.refund_reconcile import (  # noqa: E402
    ReconciliationReport,
    format_alert_body,
    format_alert_subject,
    reconcile,
    yesterday_utc,
)

SUMMARY_SCHEMA_VERSION = "refund_reconcile_cli_v1"
DEFAULT_LOG_DIR = Path("/var/log/resemblio")
LOG_FILENAME = "refund-reconcile.log"

logger = logging.getLogger("resemblio.refund_reconcile.cli")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Resemblio refund-reconciliation daily check.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and log the report but never call Resend.",
    )
    parser.add_argument(
        "--date",
        dest="date",
        default=None,
        help="Override the report date (YYYY-MM-DD UTC). Default: yesterday UTC.",
    )
    return parser


def _parse_date(raw: str | None) -> _date_type | None:
    """Parse a YYYY-MM-DD CLI arg into a date; None passes through."""
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc).date()


def _log_report_line(log_dir: Path, report: ReconciliationReport) -> Path:
    """Append a one-line summary to the per-day reconciliation log file.

    The summary carries the schema version so future log-parsing tools
    can switch on it. The file is append-only; logrotate handles size.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILENAME
    line = (
        f"{datetime.now(timezone.utc).isoformat()} "
        f"schema={SUMMARY_SCHEMA_VERSION} "
        f"window_date={report.window_date.isoformat()} "
        f"failed={report.failed_count} "
        f"refunded={report.refunded_count} "
        f"drift={report.drift} "
        f"unreconciled={len(report.unreconciled)}\n"
    )
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return log_path


def _dispatch_alert(report: ReconciliationReport, *, dry_run: bool) -> bool:
    """Send the Resend drift alert. Return True on a successful send.

    Honors ``--dry-run`` and a missing ``RESEND_API_KEY`` by logging and
    returning False. Network retries live inside ``send_alert_via_resend``.
    """
    subject = format_alert_subject(report)
    body = format_alert_body(report)
    if dry_run:
        logger.info("dry-run: would alert subject=%r", subject)
        return False
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.error(
            "drift detected (drift=%d) but RESEND_API_KEY is unset; "
            "set it via /etc/resemblio/probe.env to enable alerts",
            report.drift,
        )
        return False
    with httpx.Client(timeout=15.0) as client:
        return send_alert_via_resend(
            subject=subject, body=body, api_key=api_key, client=client
        )


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 on completion, 2 on operator-visible bug."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = _build_arg_parser().parse_args(argv)
    log_dir = Path(
        os.environ.get("RESEMBLIO_RECONCILE_LOG_DIR", str(DEFAULT_LOG_DIR))
    )

    try:
        report_date = _parse_date(args.date) or yesterday_utc()
    except ValueError as exc:
        logger.error("invalid --date value: %s", exc)
        return 2

    try:
        with SessionLocal() as session:
            report = reconcile(session, report_date=report_date)
    except Exception as exc:  # noqa: BLE001 - top-level guard
        logger.exception("refund reconciliation crashed: %s", exc)
        return 2

    try:
        log_path = _log_report_line(log_dir, report)
    except OSError as exc:
        # Log-write failures are non-fatal; the report still surfaces in
        # stdout and the alert path runs regardless.
        logger.warning("could not append reconciliation log: %s", exc)
        log_path = log_dir / LOG_FILENAME

    alert_sent = False
    if report.drift != 0:
        alert_sent = _dispatch_alert(report, dry_run=args.dry_run)

    print(
        "{schema} window_date={date} failed={failed} refunded={refunded} "
        "drift={drift} alert_sent={sent} log={log}".format(
            schema=SUMMARY_SCHEMA_VERSION,
            date=report.window_date.isoformat(),
            failed=report.failed_count,
            refunded=report.refunded_count,
            drift=report.drift,
            sent=alert_sent,
            log=log_path,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
