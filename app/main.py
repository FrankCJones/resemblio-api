"""FastAPI application entrypoint."""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app.auth import AuthMiddleware
from app.config import get_settings, validate_startup_settings
from app.routes import account, api_keys, extractions, health


def create_app() -> FastAPI:
    """Create and configure the FastAPI app for uvicorn and tests."""
    settings = get_settings()
    validate_startup_settings(settings)
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    app = FastAPI(title="Resemblio API", version="0.1.0")
    app.add_middleware(AuthMiddleware)
    app.include_router(health.router, prefix="/v1", tags=["health"])
    app.include_router(account.router, prefix="/v1", tags=["account"])
    app.include_router(api_keys.router, prefix="/v1", tags=["api_keys"])
    app.include_router(extractions.router, prefix="/v1", tags=["extractions"])
    return app


app = create_app()

