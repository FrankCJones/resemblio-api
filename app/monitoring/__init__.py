"""Resemblio production monitoring primitives.

This package owns the synthetic prod-probe + state-machine alert dedup that
closes the detection gap CTO surfaced after the 2026-06-02 Library three-hour
outage (caught by Frank's browser refresh, not by an alert). Stage 1 of the
TDD recovery plan at
``projects/OptSus Team/cto-reviews/2026-06-03-resemblio-back-on-track-tdd-plan.md``.

The probe runs as a one-shot script under a 5-minute systemd timer on
``resemblio-prod-01``. The CLI wrapper lives at
``scripts/synthetic_probe.py``; the logic + types live here so tests can
import without sys.path tricks.
"""
