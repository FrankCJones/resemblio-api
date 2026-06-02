"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from app.auth import AuthMiddleware
from app.config import get_settings, validate_startup_settings
from app.request_id import REQUEST_ID_HEADER, RequestIdMiddleware
from app.routes import account, api_keys, billing, convert, credit, extractions, health, internal_auth, library, webhooks


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
    # Starlette wraps middlewares LIFO, so the LAST middleware added is the
    # OUTERMOST in the chain. Add RequestIdMiddleware last so it sees every
    # request first, attaches ``request.state.request_id`` before
    # AuthMiddleware runs (auth 401/403 responses then carry the header), and
    # echoes the header on every response including error responses.
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(  # type: ignore[no-untyped-def]
        request: Request, exc: Exception
    ):
        """Return a structured 500 with the request id on every unhandled error.

        The audit-IP 500 on 2026-06-02 took a ``journalctl`` dig because the
        body was an opaque FastAPI default and the response had no caller-
        usable correlation token. With this handler in place every unhandled
        exception:

        * logs ``unhandled_exception`` at ERROR with the request id, request
          path, and exception class for grep-against-journalctl;
        * returns ``{"error": "internal_error", "request_id": "<id>"}`` so
          the customer sending the bug report includes the id by accident.

        Auth middleware short-circuits 401/403 with its own well-formed JSON
        body before ever reaching this handler; this only catches the truly
        unhandled cases.
        """
        request_id = getattr(request.state, "request_id", None) or ""
        logging.getLogger(__name__).error(
            "unhandled_exception request_id=%s path=%s exc=%s",
            request_id,
            request.url.path,
            exc.__class__.__name__,
        )
        headers = {REQUEST_ID_HEADER: request_id} if request_id else None
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "request_id": request_id},
            headers=headers,
        )
    app.include_router(health.router, prefix="/v1", tags=["health"])
    app.include_router(account.router, prefix="/v1", tags=["account"])
    app.include_router(api_keys.router, prefix="/v1", tags=["api_keys"])
    app.include_router(credit.router, prefix="/v1", tags=["credit"])
    app.include_router(extractions.router, prefix="/v1", tags=["extractions"])
    app.include_router(convert.router, prefix="/v1", tags=["convert"])
    app.include_router(webhooks.router, prefix="/v1", tags=["webhooks"])
    app.include_router(internal_auth.router, prefix="/v1", tags=["internal_auth"])
    app.include_router(billing.router, prefix="/v1", tags=["internal_billing"])
    app.include_router(library.router, prefix="/v1", tags=["library"])
    return app


app = create_app()
