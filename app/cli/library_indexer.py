"""CLI entry point for the library indexer.

Run as ``python -m app.cli.library_indexer``. Drains up to
``LIBRARY_INDEX_BATCH_SIZE`` pending rows from ``library_index_jobs`` and
exits with code 0 on success, 1 on session-level failure (DB unreachable,
import error, unhandled exception outside ``drain_pending``).

Invoked every 60 seconds by ``deploy/systemd/resemblio-library-indexer.timer``
on ``resemblio-prod-01``. The systemd timer is the only operational scheduler;
no in-process daemon, no cron entry.

Per-job compose failures are NOT session-level failures: they update the
job row's status to ``pending`` (for retry) or ``failed`` (when attempts
exceed the retry budget) and the CLI still exits 0. Only failures that
prevent the worker from running at all (DB connect failure, library
indexer import error) cause a non-zero exit.

Authority: GREEN per ``projects/Resemblio/AUTHORITY.yml`` (read-write on
``library_index_jobs`` and ``library_pages`` is the indexer's documented
scope; no Stripe or credit-ledger writes).
"""
from __future__ import annotations

import argparse
import logging
import sys

from app.db import SessionLocal
from app.library_indexer import drain_pending


logger = logging.getLogger("resemblio.cli.library_indexer")


def _build_parser() -> argparse.ArgumentParser:
    """Argparse parser. Exists so ``--help`` exits 0 without DB access.

    The CI entrypoint smoke (`ci/entrypoints.sh`) runs ``python -m
    app.cli.library_indexer --help`` in a clean subprocess to prove the
    module imports cleanly. Without a parser, ``--help`` would be silently
    ignored and the worker would try to open a DB session in CI.
    """
    return argparse.ArgumentParser(
        prog="library_indexer",
        description=(
            "Drain pending library_index_jobs rows. Invoked every 60s by "
            "resemblio-library-indexer.timer on resemblio-prod-01."
        ),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns a shell exit code.

    The log line shape is intentionally stable so an operator can grep
    ``library_indexer_tick_complete`` from journald without parsing prose.
    """
    _build_parser().parse_args(argv)  # exits 0 on --help; no flags accepted
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    session = SessionLocal()
    try:
        result = drain_pending(session)
    except Exception:  # pragma: no cover - logged-and-reraised for journald
        logger.exception("library_indexer_tick_failed")
        session.rollback()
        session.close()
        return 1
    session.close()
    logger.info(
        "library_indexer_tick_complete jobs_run=%d pages_written=%d schema_version=%s",
        result.jobs_run,
        result.pages_written,
        result.schema_version,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via systemd
    raise SystemExit(main(sys.argv[1:]))
