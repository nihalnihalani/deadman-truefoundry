"""Tests: token-bucket rate limiter + webhook 429 integration.

Coverage:
- TokenBucket allows up to burst, then blocks.
- TokenBucket refills correctly over time (monkeypatched clock).
- RateLimiter isolates keys (different clients don't share a bucket).
- Disabled mode (rate_per_sec <= 0) always allows.
- Webhook POST /incident: tiny limit yields 429 with Retry-After.
- Default config (DEADMAN_RATE_LIMIT_RPS=0) does NOT throttle the test suite.
"""
from __future__ import annotations

import time

import pytest

from deadman.ratelimit import RateLimiter, TokenBucket


# ---------------------------------------------------------------------------
# TokenBucket — unit tests
# ---------------------------------------------------------------------------


class TestTokenBucket:

    def test_allows_up_to_burst(self):
        """A fresh bucket allows exactly `burst` requests before blocking."""
        burst = 5
        bucket = TokenBucket(rate_per_sec=10.0, burst=burst)
        results = [bucket.allow() for _ in range(burst)]
        assert all(results), "All burst requests should be allowed"

    def test_blocks_when_exhausted(self):
        """Request burst+1 is denied when the bucket has been emptied."""
        bucket = TokenBucket(rate_per_sec=10.0, burst=3)
        for _ in range(3):
            bucket.allow()
        assert bucket.allow() is False

    def test_refills_over_time(self, monkeypatch):
        """Monkeypatching time.monotonic shows the bucket refills at the correct rate."""
        _fake_time = [0.0]

        monkeypatch.setattr(time, "monotonic", lambda: _fake_time[0])

        bucket = TokenBucket(rate_per_sec=10.0, burst=10)
        # Drain the bucket
        for _ in range(10):
            bucket.allow()
        assert bucket.allow() is False  # empty

        # Advance 0.5 s -> 5 new tokens
        _fake_time[0] = 0.5
        results = [bucket.allow() for _ in range(5)]
        assert all(results), "Should have 5 new tokens after 0.5 s at 10 rps"
        assert bucket.allow() is False  # back to empty

    def test_does_not_exceed_burst_on_refill(self, monkeypatch):
        """Tokens do not accumulate beyond `burst` during a long idle period."""
        _fake_time = [0.0]
        monkeypatch.setattr(time, "monotonic", lambda: _fake_time[0])

        bucket = TokenBucket(rate_per_sec=5.0, burst=5)
        # Drain
        for _ in range(5):
            bucket.allow()

        # Wait 100 seconds (would be 500 tokens, but cap at burst=5)
        _fake_time[0] = 100.0
        ok_count = sum(bucket.allow() for _ in range(10))
        assert ok_count == 5, f"Should be capped at burst=5, got {ok_count}"

    def test_invalid_rate_raises(self):
        with pytest.raises(ValueError):
            TokenBucket(rate_per_sec=0.0, burst=10)

    def test_invalid_negative_rate_raises(self):
        with pytest.raises(ValueError):
            TokenBucket(rate_per_sec=-1.0, burst=10)

    def test_retry_after_zero_when_tokens_available(self):
        bucket = TokenBucket(rate_per_sec=100.0, burst=10)
        assert bucket.retry_after() == 0.0

    def test_retry_after_positive_when_empty(self):
        bucket = TokenBucket(rate_per_sec=2.0, burst=1)
        bucket.allow()  # drain
        ra = bucket.retry_after()
        assert ra > 0.0


# ---------------------------------------------------------------------------
# RateLimiter — multi-key registry
# ---------------------------------------------------------------------------


class TestRateLimiter:

    def test_disabled_always_allows(self):
        """rate_per_sec=0 -> always allow."""
        limiter = RateLimiter(rate_per_sec=0)
        for _ in range(1000):
            assert limiter.allow("any-key") is True

    def test_negative_rate_disabled(self):
        limiter = RateLimiter(rate_per_sec=-5)
        assert limiter.allow("x") is True

    def test_isolates_keys(self):
        """Two different keys have independent buckets."""
        limiter = RateLimiter(rate_per_sec=2.0, burst=2)
        # Drain key-A
        limiter.allow("key-A")
        limiter.allow("key-A")
        assert limiter.allow("key-A") is False  # key-A exhausted

        # key-B starts fresh
        assert limiter.allow("key-B") is True

    def test_allows_up_to_burst(self):
        burst = 4
        limiter = RateLimiter(rate_per_sec=100.0, burst=burst)
        results = [limiter.allow("client-1") for _ in range(burst)]
        assert all(results)
        assert limiter.allow("client-1") is False

    def test_retry_after_disabled_is_zero(self):
        limiter = RateLimiter(rate_per_sec=0)
        assert limiter.retry_after("any") == 0.0

    def test_retry_after_unknown_key_is_zero(self):
        limiter = RateLimiter(rate_per_sec=10.0, burst=10)
        assert limiter.retry_after("unseen-key") == 0.0

    def test_retry_after_positive_after_exhaustion(self):
        limiter = RateLimiter(rate_per_sec=2.0, burst=1)
        limiter.allow("c")  # drain
        ra = limiter.retry_after("c")
        assert ra > 0.0

    def test_refills_with_monkeypatched_clock(self, monkeypatch):
        """Bucket behind RateLimiter refills when monotonic clock advances."""
        _fake = [0.0]
        monkeypatch.setattr(time, "monotonic", lambda: _fake[0])

        limiter = RateLimiter(rate_per_sec=10.0, burst=5)
        for _ in range(5):
            limiter.allow("client")
        assert limiter.allow("client") is False

        _fake[0] = 0.5  # 5 new tokens
        results = [limiter.allow("client") for _ in range(5)]
        assert all(results)

    def test_creates_bucket_lazily(self):
        """A new key gets a fresh full bucket."""
        limiter = RateLimiter(rate_per_sec=1.0, burst=3)
        # Allow all 3 burst tokens for a brand-new key
        assert sum(limiter.allow(f"new-{i}") for i in range(3)) == 3


# ---------------------------------------------------------------------------
# Webhook integration — 429 with Retry-After + default no-throttle
# ---------------------------------------------------------------------------


class TestWebhookRateLimit:

    def test_429_with_tiny_limit(self, isolated_state, monkeypatch):
        """A burst=1 limiter on /incident yields 429 on the second rapid request."""
        from fastapi.testclient import TestClient
        import deadman.webhook as webhook_module

        # Replace the module-level limiter with a very tight one (burst=1).
        tight_limiter = RateLimiter(rate_per_sec=0.01, burst=1)
        monkeypatch.setattr(webhook_module, "_incident_limiter", tight_limiter)

        client = TestClient(webhook_module.app)
        resp1 = client.post("/incident", json={"incident_id": "rl-test-1"})
        assert resp1.status_code == 200

        # Second immediate call: bucket is empty -> 429
        resp2 = client.post("/incident", json={"incident_id": "rl-test-2"})
        assert resp2.status_code == 429

    def test_429_has_retry_after_header(self, isolated_state, monkeypatch):
        """429 responses include a Retry-After header."""
        from fastapi.testclient import TestClient
        import deadman.webhook as webhook_module

        tight = RateLimiter(rate_per_sec=0.01, burst=1)
        monkeypatch.setattr(webhook_module, "_incident_limiter", tight)

        client = TestClient(webhook_module.app)
        client.post("/incident", json={"incident_id": "rl-hdr-1"})  # drain
        resp = client.post("/incident", json={"incident_id": "rl-hdr-2"})

        assert resp.status_code == 429
        assert "retry-after" in resp.headers or "Retry-After" in resp.headers

    def test_default_config_does_not_throttle(self, isolated_state):
        """With DEADMAN_RATE_LIMIT_RPS=0 (default) the suite is never throttled."""
        from fastapi.testclient import TestClient
        from deadman.webhook import app

        client = TestClient(app)
        # Post 20 incidents rapidly — all should succeed with the disabled limiter.
        for i in range(20):
            resp = client.post("/incident", json={"incident_id": f"no-throttle-{i}"})
            assert resp.status_code == 200, (
                f"Request {i} was throttled unexpectedly: {resp.status_code}"
            )

    def test_healthz_still_200(self, isolated_state):
        """Rate limiting does not affect /healthz."""
        from fastapi.testclient import TestClient
        from deadman.webhook import app

        client = TestClient(app)
        resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_readyz_still_200_when_not_shutting_down(self, isolated_state):
        """Rate limiting does not affect /readyz when not shutting down."""
        from fastapi.testclient import TestClient
        from deadman.webhook import app

        client = TestClient(app)
        resp = client.get("/readyz")
        assert resp.status_code == 200

    def test_metrics_still_200(self, isolated_state):
        """/metrics endpoint is unaffected by rate limiting."""
        from fastapi.testclient import TestClient
        from deadman.webhook import app

        client = TestClient(app)
        resp = client.get("/metrics")
        assert resp.status_code == 200
