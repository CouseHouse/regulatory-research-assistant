"""Token-bucket rate limiter — shared module for all HTTP callers.

Implemented with stdlib threading + time only; no new dependencies.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass
class RateLimitStats:
    """Observability snapshot from a RateLimiter."""

    requests_made: int = 0
    total_wait_seconds: float = 0.0
    longest_wait_seconds: float = 0.0


class RateLimiter:
    """Thread-safe token-bucket rate limiter.

    Args:
        rate_per_second: Steady-state refill rate (tokens / second).
        burst: Bucket capacity and initial fill level.
    """

    def __init__(self, rate_per_second: float, burst: int) -> None:
        self._rate = rate_per_second
        self._burst = burst
        self._tokens: float = float(burst)
        self._last_refill: float = time.monotonic()
        self._lock = threading.Lock()
        self._stats_data = RateLimitStats()

    @property
    def stats(self) -> RateLimitStats:
        """Return a thread-safe snapshot of current stats."""
        with self._lock:
            return RateLimitStats(
                requests_made=self._stats_data.requests_made,
                total_wait_seconds=self._stats_data.total_wait_seconds,
                longest_wait_seconds=self._stats_data.longest_wait_seconds,
            )

    def _refill(self) -> None:
        """Add tokens based on elapsed time. Must be called with _lock held."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(float(self._burst), self._tokens + elapsed * self._rate)
        self._last_refill = now

    def acquire(self, tokens: int = 1) -> float:
        """Block until *tokens* are available. Returns wait duration in seconds."""
        start = time.monotonic()
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    wait = time.monotonic() - start
                    self._stats_data.requests_made += 1
                    self._stats_data.total_wait_seconds += wait
                    if wait > self._stats_data.longest_wait_seconds:
                        self._stats_data.longest_wait_seconds = wait
                    if wait > 1.0:
                        log.info(
                            "rate_limit.long_wait",
                            wait_seconds=round(wait, 3),
                            tokens_remaining=round(self._tokens, 2),
                        )
                    else:
                        log.debug(
                            "rate_limit.acquire",
                            wait_seconds=round(wait, 4),
                            tokens_remaining=round(self._tokens, 2),
                        )
                    return wait
                # Compute sleep outside the lock to avoid holding it while idle.
                sleep_for = (tokens - self._tokens) / self._rate
            time.sleep(max(0.001, sleep_for))
