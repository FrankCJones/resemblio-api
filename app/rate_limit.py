"""In-memory token-bucket rate limiting for S1."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock

from app.constants import (
    RATE_LIMIT_DAY_WINDOW_SECONDS,
    RATE_LIMIT_MIN_WINDOW_SECONDS,
    RATE_LIMIT_PER_DAY,
    RATE_LIMIT_PER_MIN,
)


@dataclass
class TokenBucket:
    """Simple refillable bucket used by per-key and per-user limits."""

    capacity: int
    refill_seconds: int
    tokens: float
    updated_at: float = field(default_factory=time.monotonic)

    def allow(self, cost: int = 1, now: float | None = None) -> bool:
        """Consume tokens when available after time-based refill."""
        current = time.monotonic() if now is None else now
        elapsed = max(0.0, current - self.updated_at)
        refill = elapsed * (self.capacity / self.refill_seconds)
        self.tokens = min(float(self.capacity), self.tokens + refill)
        self.updated_at = current
        if self.tokens < cost:
            return False
        self.tokens -= cost
        return True

    def seconds_until_token(self, cost: int = 1, now: float | None = None) -> int:
        """Return whole seconds until ``cost`` tokens would refill into the bucket.

        Used to populate the HTTP ``Retry-After`` header on 429 responses.
        Returns at least 1 so a client honoring the header never tight-loops.
        The math mirrors ``allow``'s refill rate (``capacity / refill_seconds``)
        and reads ``tokens`` after the most recent ``allow`` call updated it,
        so callers MUST invoke this immediately after ``allow`` returned False
        without intervening mutations on the same bucket.
        """
        deficit = max(0.0, float(cost) - self.tokens)
        if deficit <= 0:
            return 1
        seconds = deficit * (self.refill_seconds / self.capacity)
        # Ceil to whole seconds; Retry-After is an integer per RFC 9110.
        return max(1, int(seconds) + (1 if seconds % 1 else 0))


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of a rate-limit check."""

    allowed: bool
    error: str | None = None
    retry_after_seconds: int | None = None


class InMemoryRateLimiter:
    """S1 limiter keyed by API key hash and user id.

    The process-local store is sufficient for local S1 validation. Redis can
    replace this class later without changing the auth middleware contract.
    """

    def __init__(self) -> None:
        """Create empty bucket stores guarded by a process-local lock."""
        self._lock = Lock()
        self._minute_buckets: dict[str, TokenBucket] = {}
        self._day_buckets: dict[str, TokenBucket] = {}

    def check(self, key_hash: str, user_id: int) -> RateLimitResult:
        """Consume one request from per-key minute and per-user day buckets."""
        with self._lock:
            minute_key = f"key:{key_hash}:minute"
            day_key = f"user:{user_id}:day"
            minute = self._minute_buckets.setdefault(
                minute_key,
                TokenBucket(RATE_LIMIT_PER_MIN, RATE_LIMIT_MIN_WINDOW_SECONDS, float(RATE_LIMIT_PER_MIN)),
            )
            day = self._day_buckets.setdefault(
                day_key,
                TokenBucket(RATE_LIMIT_PER_DAY, RATE_LIMIT_DAY_WINDOW_SECONDS, float(RATE_LIMIT_PER_DAY)),
            )
            if not minute.allow():
                return RateLimitResult(
                    False, "rate_limit_minute", minute.seconds_until_token()
                )
            if not day.allow():
                return RateLimitResult(
                    False, "rate_limit_day", day.seconds_until_token()
                )
            return RateLimitResult(True)

    def reset(self) -> None:
        """Clear all buckets for deterministic tests."""
        with self._lock:
            self._minute_buckets.clear()
            self._day_buckets.clear()


rate_limiter = InMemoryRateLimiter()


def reset_rate_limiter() -> None:
    """Reset the process-wide limiter used by tests."""
    rate_limiter.reset()

