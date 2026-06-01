"""Mock TrueFoundry AI Gateway — the resilient brain.

Cross-region + cross-provider Bedrock fallback chain, latency-based shedding (degrade
BEFORE a hard 5xx), and a semantic cache that becomes the "runbook brain" when every
live model is down. Tracks the max fallback depth reached (the scoreboard signal the
Agent Gateway subscribes to). In real mode this is the OpenAI-compatible TFY endpoint.
"""
from __future__ import annotations
from dataclasses import dataclass
import deadman.config as config


@dataclass
class Completion:
    text: str
    backend: str
    tier: int
    from_cache: bool


class ModelOutage(Exception):
    pass


class AIGateway:
    def __init__(self, chaos=None):
        self.chaos = chaos
        self.max_depth = 0
        self.fallbacks = 0
        self.semantic_cache: dict[str, str] = {}

    def _tier_healthy(self, t: dict) -> bool:
        if self.chaos is None:
            return True
        return self.chaos.tier_healthy(t["tier"], t["region"])

    def complete(self, prompt: str) -> Completion:
        """Try each tier in order; shed on outage OR latency-budget breach; cache last."""
        if config.MODE == "real":
            # The real TFY AI Gateway does fallback/retry/cache; we just call the virtual model.
            # realmode_ai returns richer metadata (fallback_depth, from_cache) so the Agent
            # Gateway auto-leash works correctly in real mode.
            from deadman import realmode_ai
            r = realmode_ai.complete(prompt)
            # Track degradation depth so AgentGateway can revoke destructive authority.
            self.max_depth = max(self.max_depth, r["fallback_depth"])
            if r["fallback_depth"] > 0:
                self.fallbacks += 1
            return Completion(r["text"], r["served_by"], tier=r["fallback_depth"], from_cache=r["from_cache"])
        for t in config.FALLBACK_CHAIN:
            healthy = self._tier_healthy(t)
            slow = self.chaos.latency_ms(t["tier"]) > config.P99_LATENCY_BUDGET_MS if self.chaos else False
            if healthy and not slow:
                if t["tier"] > 0:
                    self.fallbacks += 1
                self.max_depth = max(self.max_depth, t["tier"])
                answer = f"[plan for: {prompt[:40]}]"
                self.semantic_cache[prompt] = answer        # warm the cache while healthy
                backend = f'{t["model"]}@{t["region"]}'
                return Completion(answer, backend, t["tier"], from_cache=False)
        # every live tier down -> semantic cache "runbook brain"
        self.max_depth = config.SEMANTIC_CACHE_TIER
        if prompt in self.semantic_cache:
            return Completion(self.semantic_cache[prompt], "semantic-cache", config.SEMANTIC_CACHE_TIER, True)
        # nothing cached and everything down -> closest prior validated mitigation
        if self.semantic_cache:
            text = next(iter(self.semantic_cache.values()))
            return Completion(text, "semantic-cache(nearest)", config.SEMANTIC_CACHE_TIER, True)
        raise ModelOutage("all Bedrock tiers down and cache cold")
