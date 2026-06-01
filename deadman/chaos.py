"""Chaos injector — every failure in the challenge list is a toggle.

This is what makes the demo DETERMINISTIC (vs a live-network voice gamble): the judge
sees each failure injected on command and the exact recovery, reproducibly.
"""
from __future__ import annotations


class Chaos:
    def __init__(self):
        self.down_regions: set[str] = set()    # e.g. {"us-east-1"}
        self.down_tiers: set[int] = set()       # specific fallback tiers offline
        self.all_bedrock_down = False
        self.slow_tiers: dict[int, int] = {}    # tier -> latency ms (latency-shed trigger)
        self.corrupt_output = False             # degraded API returns garbage JSON
        self.kill_after: str | None = None      # SIGKILL right after this idempotency key's side effect

    # ---- toggles (the demo's buttons) ----
    def correlated_blackout(self):
        """One button: the us-east-1 EVENT — region + provider + tools degrade together."""
        self.down_regions.add("us-east-1")

    def rate_limit_storm(self):
        self.slow_tiers[0] = 99999  # tier-0 effectively throttled

    def kill_bedrock(self):
        self.all_bedrock_down = True

    def kill_process_after(self, key: str):
        self.kill_after = key

    # ---- queries the gateways use ----
    def tier_healthy(self, tier: int, region: str) -> bool:
        if self.all_bedrock_down:
            return False
        if tier in self.down_tiers:
            return False
        if region in self.down_regions:
            return False
        return True

    def latency_ms(self, tier: int) -> int:
        return self.slow_tiers.get(tier, 100)
