"""Auth-free health and webhook stub routes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Return liveness without touching the database."""
    return {"status": "ok"}


@router.post("/webhooks/stripe")
async def stripe_webhook_stub(request: Request) -> Response:
    """Accept Stripe webhook bodies in S1 while S2 owns real handling."""
    body = await request.body()
    logger.info("stripe webhook stub accepted bytes=%s", len(body))
    return Response(status_code=202)

