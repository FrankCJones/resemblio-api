"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request

from app.auth import AuthMiddleware
from app.config import get_settings, validate_startup_settings
from app.routes import account, api_keys, credit, extractions, health, webhooks


def validate_worker_concurrency() -> None:
    """Fail startup if the process is configured for multi-worker uvicorn.

    The in-memory rate limiter in ``app.rate_limit`` stores token buckets in a
    process-local dict, so each uvicorn worker keeps an independent ceiling.
    Running with N workers silently multiplies the documented per-key rate by
    N. Until the limiter is migrated to a shared Redis backend, the service
    MUST run single-worker. This guard reads the two common env vars that
    spawn extra workers (``WEB_CONCURRENCY`` honored by uvicorn/gunicorn, and
    ``UVICORN_WORKERS`` used by some deploy scripts) and refuses to start if
    either is set above 1. Pin ``--workers 1`` in the systemd unit; see
    ``scripts/resemblio-api.service.example`` for the canonical unit body.
    """
    for env_var in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw = os.environ.get(env_var)
        if raw is None or raw.strip() == "":
            continue
        try:
            workers = int(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"{env_var} must be an integer; got {raw!r}"
            ) from exc
        if workers > 1:
            raise RuntimeError(
                f"{env_var}={workers} is unsafe: the in-memory rate limiter is "
                "process-local and multi-worker deploys silently multiply the "
                "effective ceiling. Pin to 1 in the systemd unit or migrate "
                "rate_limit.py to a Redis-backed store first."
            )


def create_app() -> FastAPI:
    """Create and configure the FastAPI app for uvicorn and tests.

    The interactive docs endpoints (``/docs``, ``/redoc``, ``/openapi.json``) are
    disabled by default so production never advertises the full API surface to
    unauthenticated callers. Set ``RESEMBLIO_DOCS_ENABLED=true`` in the
    environment to re-enable them for local development.
    """
    settings = get_settings()
    validate_startup_settings(settings)
    validate_worker_concurrency()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    # Operators rely on this single line in `journalctl -u resemblio-api` to
    # confirm which Stripe mode the running process is actually bound to. Do
    # not silence it; do not move it before validate_startup_settings (which
    # is what guarantees the mode/key pair is self-consistent).
    logging.getLogger(__name__).info("Stripe mode: %s", settings.stripe_mode)
    docs_enabled = os.environ.get("RESEMBLIO_DOCS_ENABLED", "false").lower() == "true"
    app = FastAPI(
        title="Resemblio API",
        version="0.1.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    @app.middleware("http")
    async def _strip_server_header(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Backstop strip of the ``Server`` response header.

        The primary mechanism is uvicorn's ``--no-server-header`` flag in the
        systemd unit (``scripts/resemblio-api.service.example``). Uvicorn
        writes the Server header at the HTTP protocol layer AFTER the ASGI
        middleware chain runs, so deleting it here is a no-op in the
        production uvicorn stack. This middleware still matters for
        Starlette ``TestClient`` runs and for any future deployment behind a
        non-uvicorn ASGI server, where header deletion in middleware does
        take effect.
        """
        response = await call_next(request)
        if "server" in response.headers:
            del response.headers["server"]
        return response

    app.add_middleware(AuthMiddleware)
    app.include_router(health.router, prefix="/v1", tags=["health"])
    app.include_router(account.router, prefix="/v1", tags=["account"])
    app.include_router(api_keys.router, prefix="/v1", tags=["api_keys"])
    app.include_router(credit.router, prefix="/v1", tags=["credit"])
    app.include_router(extractions.router, prefix="/v1", tags=["extractions"])
    app.include_router(webhooks.router, prefix="/v1", tags=["webhooks"])
    return app


app = create_app()
