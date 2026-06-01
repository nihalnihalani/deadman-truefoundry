"""Tests 7: Fallback chain behavior.

Drive AIGateway with a Chaos that downs tiers progressively; assert it sheds to the
next tier, then to the semantic cache, then raises ModelOutage when cache is cold.
Assert max_depth tracks correctly.
Assert latency-shed (slow_tiers beyond P99 budget) skips a healthy-but-slow tier.
"""
from __future__ import annotations
import pytest

import deadman.config as config
from deadman.ai_gateway import AIGateway, Completion, ModelOutage
from deadman.chaos import Chaos


class TestFallbackChain:

    def test_healthy_chain_uses_tier_0(self):
        """No chaos -> tier 0 is used; max_depth stays 0."""
        chaos = Chaos()
        gw = AIGateway(chaos)
        c = gw.complete("diagnose")
        assert c.tier == 0
        assert gw.max_depth == 0
        assert c.from_cache is False

    def test_tier0_down_falls_to_tier1(self):
        """us-east-1 down -> tier 1 (us-west-2) serves; max_depth=1."""
        chaos = Chaos()
        chaos.down_regions.add("us-east-1")
        gw = AIGateway(chaos)
        c = gw.complete("diagnose")
        assert c.tier == 1
        assert gw.max_depth == 1

    def test_tiers_0_and_1_down_falls_to_tier2(self):
        """Tiers 0 and 1 down (both Claude regions gone) -> tier 2 (llama4); max_depth=2."""
        chaos = Chaos()
        chaos.down_regions.add("us-east-1")
        chaos.down_tiers.add(1)
        gw = AIGateway(chaos)
        c = gw.complete("diagnose")
        assert c.tier == 2
        assert gw.max_depth == 2

    def test_progressive_fallback_tiers_0_through_3(self):
        """Taking down tiers 0-3 falls through to tier 4."""
        chaos = Chaos()
        chaos.down_regions.add("us-east-1")
        chaos.down_tiers.update({1, 2, 3})
        gw = AIGateway(chaos)
        c = gw.complete("diagnose")
        assert c.tier == 4
        assert gw.max_depth == 4

    def test_all_tiers_down_uses_semantic_cache(self):
        """All live tiers down -> semantic cache serves if warm; max_depth == SEMANTIC_CACHE_TIER."""
        chaos = Chaos()
        gw = AIGateway(chaos)

        # Warm the cache first with a healthy call
        gw.complete("warm cache")

        # Now kill everything
        chaos.all_bedrock_down = True
        c2 = gw.complete("warm cache")
        assert c2.from_cache is True
        assert gw.max_depth == config.SEMANTIC_CACHE_TIER

    def test_all_tiers_down_cache_cold_raises_model_outage(self):
        """All tiers down and cache cold -> ModelOutage."""
        chaos = Chaos()
        chaos.all_bedrock_down = True
        gw = AIGateway(chaos)  # cold cache

        with pytest.raises(ModelOutage):
            gw.complete("this will fail")

    def test_latency_shed_skips_slow_tier(self):
        """Tier 0 slow beyond P99 budget -> shed to tier 1; tier 1 returned."""
        chaos = Chaos()
        chaos.slow_tiers[0] = config.P99_LATENCY_BUDGET_MS + 500  # way over budget
        gw = AIGateway(chaos)
        c = gw.complete("diagnose with slow tier 0")
        # Tier 0 is skipped due to latency; tier 1 should serve
        assert c.tier >= 1, f"Expected to skip tier 0, got tier {c.tier}"
        assert gw.max_depth >= 1

    def test_latency_shed_within_budget_stays_tier0(self):
        """Tier 0 latency within P99 budget -> no shed, tier 0 serves."""
        chaos = Chaos()
        chaos.slow_tiers[0] = config.P99_LATENCY_BUDGET_MS - 100  # under budget
        gw = AIGateway(chaos)
        c = gw.complete("fast enough")
        assert c.tier == 0

    def test_max_depth_tracks_across_multiple_calls(self):
        """max_depth increases monotonically as failures deepen across calls."""
        chaos = Chaos()
        gw = AIGateway(chaos)

        # First call: tier 0 healthy
        c0 = gw.complete("call 1")
        assert gw.max_depth == 0

        # Degrade tier 0: next call falls to tier 1
        chaos.down_regions.add("us-east-1")
        c1 = gw.complete("call 2")
        assert gw.max_depth == 1

        # Further degrade
        chaos.down_tiers.add(1)
        c2 = gw.complete("call 3")
        assert gw.max_depth == 2

    def test_fallbacks_counter_increments(self):
        """gw.fallbacks increments each time a non-tier-0 response is served."""
        chaos = Chaos()
        chaos.down_regions.add("us-east-1")
        gw = AIGateway(chaos)
        assert gw.fallbacks == 0
        gw.complete("first fallback")
        assert gw.fallbacks == 1
        gw.complete("second fallback")
        assert gw.fallbacks == 2

    def test_completion_dataclass_fields(self):
        """Completion returned from complete() has all required fields."""
        chaos = Chaos()
        gw = AIGateway(chaos)
        c = gw.complete("test")
        assert isinstance(c, Completion)
        assert isinstance(c.text, str)
        assert isinstance(c.backend, str)
        assert isinstance(c.tier, int)
        assert isinstance(c.from_cache, bool)

    def test_no_chaos_gateway_healthy(self):
        """AIGateway with chaos=None completes successfully on tier 0."""
        gw = AIGateway(chaos=None)
        c = gw.complete("healthy call")
        assert c.tier == 0
        assert gw.max_depth == 0
