"""Tests for the in-memory token-bucket limiter."""
from __future__ import annotations

from app.rate_limit import InMemoryRateLimiter, TokenBucket


def test_token_bucket_refills_over_time() -> None:
    """A bucket blocks when empty and allows again after refill time."""
    bucket = TokenBucket(capacity=2, refill_seconds=10, tokens=2, updated_at=0)
    assert bucket.allow(now=0)
    assert bucket.allow(now=0)
    assert not bucket.allow(now=0)
    assert bucket.allow(now=5)


def test_rate_limiter_blocks_minute_exhaustion() -> None:
    """The limiter denies after the per-minute bucket is consumed."""
    limiter = InMemoryRateLimiter()
    for _ in range(60):
        assert limiter.check("hash", 1).allowed
    result = limiter.check("hash", 1)
    assert not result.allowed
    assert result.error == "rate_limit_minute"

