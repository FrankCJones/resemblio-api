"""Auth-free health and readiness routes.

Two-path liveness/readiness split (audit M-API-3, `projects/OptSus Team/
security-audits/2026-05-26-initial.md`):

- ``/v1/healthz`` answers "is the process up?" with no external dependency
  checks. Cheap, always-fast; safe for the GitHub Actions post-deploy poll
  loop (`.github/workflows/deploy.yml`) which only needs to know systemd
  successfully started the unit.
- ``/v1/readyz`` answers "can the process actually serve requests?" by
  exercising Postgres and Cloudflare R2 with bounded, side-effect-free probes.
  Uptime Kuma (S8 of the v1 build) and any external alerting that gates on
  "real customer impact" should point here, not at ``healthz``. The two
  endpoints distinguish "API process up but DB down" from "all good", which
  ``healthz`` alone cannot do.

Probes are deliberately cheap (``SELECT 1`` on Postgres, ``head_bucket`` on
R2 - the same shape ``ensure_bucket`` uses) and never mutate state.
"""
from __future__ import annotations

import logging
from typing import TypedDict

from fastapi import APIRouter
from sqlalchemy import text
from starlette.responses import JSONResponse

from app.config import get_settings
from app.constants import SCHEMA_V1
from app.db import SessionLocal
from app.storage import R2Storage

router = APIRouter()
logger = logging.getLogger(__name__)


class _ReadyComponent(TypedDict):
    """Per-component readiness result."""

    status: str
    detail: str | None


class _ReadyResponse(TypedDict):
    """Aggregate readiness payload returned to callers."""

    status: str
    database: _ReadyComponent
    storage: _ReadyComponent
    schema_version: int


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Return liveness without touching the database."""
    return {"status": "ok"}


def _check_database() -> _ReadyComponent:
    """Run a trivial ``SELECT 1`` against the configured database.

    Returns ok/fail with a redacted detail string. The session is always
    closed; transient errors are logged at WARNING so an alerting page lands
    next to the cause.
    """
    session = SessionLocal()
    try:
        session.execute(text("SELECT 1"))
        return {"status": "ok", "detail": None}
    except Exception as exc:  # pragma: no cover - exercised via test stub
        logger.warning("readyz database probe failed: %s", exc.__class__.__name__)
        return {"status": "fail", "detail": exc.__class__.__name__}
    finally:
        session.close()


def _check_storage() -> _ReadyComponent:
    """Run a ``head_bucket`` against the configured R2 bucket.

    A missing R2 configuration is treated as a hard failure: in production
    the API depends on R2 to hand back extraction artifacts. If credentials
    are absent, the service cannot fulfill its contract even if the process
    is up.
    """
    settings = get_settings()
    try:
        storage = R2Storage(settings)
        storage.client.head_bucket(Bucket=storage.bucket)
        return {"status": "ok", "detail": None}
    except Exception as exc:  # pragma: no cover - exercised via test stub
        logger.warning("readyz storage probe failed: %s", exc.__class__.__name__)
        return {"status": "fail", "detail": exc.__class__.__name__}


@router.get("/readyz")
def readyz() -> JSONResponse:
    """Return readiness across database and object storage.

    Returns HTTP 200 when every dependency reports ok; HTTP 503 otherwise,
    with a per-component breakdown in the body so an alert pager sees which
    dependency failed without grepping logs.
    """
    database = _check_database()
    storage = _check_storage()
    overall = "ok" if database["status"] == "ok" and storage["status"] == "ok" else "fail"
    body: _ReadyResponse = {
        "status": overall,
        "database": database,
        "storage": storage,
        "schema_version": SCHEMA_V1,
    }
    status_code = 200 if overall == "ok" else 503
    return JSONResponse(status_code=status_code, content=body)
