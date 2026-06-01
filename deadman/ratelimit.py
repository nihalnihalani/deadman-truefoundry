"""Token-bucket rate limiter — pure stdlib, no external dependencies.

Classes
-------
TokenBucket
    Single-key token bucket with a monotonic clock. Thread-safe via a Lock.
    allow() -> bool: consume one token; returns True if the request is allowed.

RateLimiter
    Multi-key registry (keyed by client identifier, e.g. source IP or incident
    ID). Each key gets its own TokenBucket instance. Idle buckets are cleaned
    up periodically to avoid unbounded memory growth.

Usage
-----
    # Global singleton, created once at module/app startup:
    limiter = RateLimiter(rate_per_sec=10.0, burst=20)

    # Per-request check:
    if not limiter.allow("127.0.0.1"):
        return HTTP 429

Disabling
---------
    Set rate_per_sec <= 0 (or DEADMAN_RATE_LIMIT_RPS=0) to get an always-allow
    limiter — useful in tests and development.
"""
from __future__ import annotations

import threading
import time


# ---------------------------------------------------------------------------
# Single-key token bucket
# ---------------------------------------------------------------------------


class TokenBucket:
    """Token bucket with monotonic-clock refill.

    Parameters
    ----------
    rate_per_sec:
        Token refill rate (tokens per second). Must be > 0 unless you want
        the "disabled / always-allow" variant (use ``rate_per_sec <= 0``).
    burst:
        Maximum token capacity (burst size). A freshly created bucket starts
        full at this capacity.
    """

    def __init__(self, rate_per_sec: float, burst: float):
        if rate_per_sec <= 0:
            raise ValueError(
                "rate_per_sec must be > 0 for a real bucket; "
                "use the disabled path (rate_per_sec <= 0) instead."
            )
        self._rate = rate_per_sec
        self._burst = burst
        self._tokens = burst
        self._last_refill: float = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        """Add tokens for elapsed time since last refill (caller holds lock)."""
        elapsed = now - self._last_refill
        if elapsed > 0:
            new_tokens = elapsed * self._rate
            self._tokens = min(self._burst, self._tokens + new_tokens)
            self._last_refill = now

    def allow(self) -> bool:
        """Consume one token and return True if allowed; False if the bucket is empty."""
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def retry_after(self) -> float:
        """Seconds until the next token is available (upper bound, without consuming)."""
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            if self._tokens >= 1.0:
                return 0.0
            deficit = 1.0 - self._tokens
            return deficit / self._rate


# ---------------------------------------------------------------------------
# Multi-key rate limiter registry
# ---------------------------------------------------------------------------

# How often (in real seconds of wall-clock time) idle buckets are swept.
_CLEANUP_INTERVAL_SECONDS = 60.0

# How long a bucket must be idle (no access) before it is eligible for removal.
_IDLE_EVICTION_SECONDS = 300.0


class RateLimiter:
    """Per-key token-bucket registry.

    Thread-safe. Idle buckets are garbage-collected during allow() calls once
    ``_CLEANUP_INTERVAL_SECONDS`` has elapsed since the last sweep.

    Parameters
    ----------
    rate_per_sec:
        Token refill rate per key. ``<= 0`` means "disabled — always allow."
    burst:
        Burst capacity per key.
    """

    def __init__(self, rate_per_sec: float, burst: float | None = None):
        self._rate = rate_per_sec
        self._burst = burst if burst is not None else max(1.0, rate_per_sec * 2)
        self._disabled = rate_per_sec <= 0
        self._buckets: dict[str, tuple[TokenBucket, float]] = {}  # key -> (bucket, last_seen)
        self._lock = threading.Lock()
        self._last_cleanup: float = time.monotonic()

    def allow(self, key: str) -> bool:
        """Return True if ``key`` is within its rate limit; False otherwise.

        When the limiter is disabled (``rate_per_sec <= 0``), always returns True.
        """
        if self._disabled:
            return True

        now = time.monotonic()
        with self._lock:
            self._maybe_cleanup(now)
            bucket, _ = self._buckets.get(key, (None, 0.0))  # type: ignore[assignment]
            if bucket is None:
                bucket = TokenBucket(self._rate, self._burst)
            self._buckets[key] = (bucket, now)

        # allow() itself is lock-free after we get the bucket reference (the
        # bucket has its own internal lock).
        return bucket.allow()

    def retry_after(self, key: str) -> float:
        """Seconds until ``key`` can make another request (0 if currently allowed)."""
        if self._disabled:
            return 0.0
        with self._lock:
            entry = self._buckets.get(key)
        if entry is None:
            return 0.0
        bucket, _ = entry
        return bucket.retry_after()

    def _maybe_cleanup(self, now: float) -> None:
        """Remove buckets idle for longer than _IDLE_EVICTION_SECONDS.

        Must be called while holding ``self._lock``.
        """
        if now - self._last_cleanup < _CLEANUP_INTERVAL_SECONDS:
            return
        cutoff = now - _IDLE_EVICTION_SECONDS
        stale = [k for k, (_, last_seen) in self._buckets.items() if last_seen < cutoff]
        for k in stale:
            del self._buckets[k]
        self._last_cleanup = now
