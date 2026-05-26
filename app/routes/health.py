"""Auth-free health route."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Return liveness without touching the database."""
    return {"status": "ok"}
