"""Tests for rra.rate_limit.

Timing-sensitive tests allow ±20 % tolerance; exact wall-clock times will
vary with scheduler jitter and CI load.
"""
from __future__ import annotations

import threading
import time

import pytest

from rra.rate_limit import RateLimiter, RateLimitStats


# ─── Burst behaviour ──────────────────────────────────────────────────────────

def test_burst_all_immediate() -> None:
    """10 acquires with burst=10 complete without waiting."""
    limiter = RateLimiter(rate_per_second=1.0, burst=10)  # slow rate; burst covers all
    start = time.monotonic()
    for _ in range(10):
        limiter.acquire()
    elapsed = time.monotonic() - start
    # All 10 came from the burst — should be essentially instant.
    assert elapsed < 0.5, f"expected < 0.5 s for burst, got {elapsed:.3f} s"


# ─── Sustained rate ───────────────────────────────────────────────────────────

def test_sustained_rate_timing() -> None:
    """20 acquires at rate=5/burst=5 takes ≈3 s (15 post-burst acquires at 5/s)."""
    limiter = RateLimiter(rate_per_second=5.0, burst=5)
    start = time.monotonic()
    for _ in range(20):
        limiter.acquire()
    elapsed = time.monotonic() - start
    # First 5 use burst; next 15 at 5/s ≈ 3 s.  Allow ±20 %.
    expected = 3.0
    assert expected * 0.8 <= elapsed <= expected * 1.2, (
        f"expected {expected * 0.8:.2f}–{expected * 1.2:.2f} s, got {elapsed:.3f} s"
    )


# ─── Stats reporting ──────────────────────────────────────────────────────────

def test_stats_requests_made() -> None:
    """requests_made increments once per acquire call."""
    limiter = RateLimiter(rate_per_second=100.0, burst=20)
    for _ in range(10):
        limiter.acquire()
    assert limiter.stats.requests_made == 10


def test_stats_no_wait_when_burst_covers_all() -> None:
    """total_wait_seconds and longest_wait_seconds are near zero when burst covers all requests."""
    limiter = RateLimiter(rate_per_second=100.0, burst=20)
    for _ in range(10):
        limiter.acquire()
    s = limiter.stats
    assert s.total_wait_seconds < 0.1
    assert s.longest_wait_seconds < 0.1


def test_stats_accumulate_correctly() -> None:
    """total_wait_seconds is at least the sum of non-trivial waits."""
    limiter = RateLimiter(rate_per_second=5.0, burst=5)
    for _ in range(10):
        limiter.acquire()
    s = limiter.stats
    assert s.requests_made == 10
    # 5 post-burst acquires × ≈0.2 s each ≈ 1 s total wait.
    assert s.total_wait_seconds >= 0.8
    assert s.longest_wait_seconds <= s.total_wait_seconds


def test_stats_is_a_snapshot() -> None:
    """stats property returns a copy — mutations don't affect the limiter."""
    limiter = RateLimiter(rate_per_second=100.0, burst=5)
    limiter.acquire()
    snapshot = limiter.stats
    snapshot.requests_made = 999  # mutate the copy
    assert limiter.stats.requests_made == 1  # limiter unchanged


def test_stats_returns_rate_limit_stats_instance() -> None:
    limiter = RateLimiter(rate_per_second=10.0, burst=5)
    assert isinstance(limiter.stats, RateLimitStats)


# ─── Thread safety ────────────────────────────────────────────────────────────

def test_thread_safety_total_acquires() -> None:
    """All 50 acquires complete and requests_made is exactly 50."""
    limiter = RateLimiter(rate_per_second=10.0, burst=10)
    completed: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(10):
            wait = limiter.acquire()
            with lock:
                completed.append(wait)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(completed) == 50
    assert limiter.stats.requests_made == 50


def test_thread_safety_wall_clock() -> None:
    """5 threads × 10 acquires at rate=10/burst=10 completes in ≈4 s.

    Burst covers the first 10; the remaining 40 at 10/s take ≈4 s total.
    """
    limiter = RateLimiter(rate_per_second=10.0, burst=10)

    def worker() -> None:
        for _ in range(10):
            limiter.acquire()

    start = time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start

    expected = 4.0  # 40 post-burst acquires at 10/s
    assert expected * 0.8 <= elapsed <= expected * 1.2, (
        f"expected {expected * 0.8:.2f}–{expected * 1.2:.2f} s, got {elapsed:.3f} s"
    )


# ─── acquire return value ─────────────────────────────────────────────────────

def test_acquire_returns_float() -> None:
    limiter = RateLimiter(rate_per_second=100.0, burst=5)
    result = limiter.acquire()
    assert isinstance(result, float)


def test_acquire_returns_zero_ish_when_no_wait() -> None:
    limiter = RateLimiter(rate_per_second=100.0, burst=5)
    wait = limiter.acquire()
    assert wait < 0.1
