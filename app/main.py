"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request

from app.auth import AuthMiddleware
from app.config import get_settings, validate_startup_settings
from app.routes import account, api_keys, extractions, health


def create_app() -> FastAPI:
    """Create and configure the FastAPI app for uvicorn and tests.

    The interactive docs endpoints (``/docs``, ``/redoc``, ``/openapi.json``) are
    disabled by default so production never advertises the full API surface to
    unauthenticated callers. Set ``RESEMBLIO_DOCS_ENABLED=true`` in the
    environment to re-enable them for local development.
    """
    settings = get_settings()
    validate_startup_settings(settings)
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
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
        """Remove the default ``Server: uvicorn`` header to avoid leaking stack details."""
        response = await call_next(request)
        if "server" in response.headers:
            del response.headers["server"]
        return response

    app.add_middleware(AuthMiddleware)
    app.include_router(health.router, prefix="/v1", tags=["health"])
    app.include_router(account.router, prefix="/v1", tags=["account"])
    app.include_router(api_keys.router, prefix="/v1", tags=["api_keys"])
    app.include_router(extractions.router, prefix="/v1", tags=["extractions"])
    return app


app = create_app()

