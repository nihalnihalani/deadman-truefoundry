"""DEADMAN config — the Bedrock fallback chain, tool scopes, and thresholds.

Mock mode (default) runs the whole thing on the stdlib. Set DEADMAN_MODE=real and
fill .env to route through the real TrueFoundry AI Gateway + MCP Gateway + Bedrock.
"""
import os

MODE = os.getenv("DEADMAN_MODE", "mock")

# The Bedrock fallback chain — each tier is tagged in the AI Gateway trace.
# (region-aware: tier 1->2 is the literal May 7-8 2026 us-east-1 cross-region failover)
FALLBACK_CHAIN = [
    {"tier": 0, "model": "anthropic.claude-3-5-sonnet", "region": "us-east-1"},
    {"tier": 1, "model": "anthropic.claude-3-5-sonnet", "region": "us-west-2"},
    {"tier": 2, "model": "meta.llama3-1-70b", "region": "us-west-2"},
    {"tier": 3, "model": "mistral.mistral-large", "region": "us-west-2"},
    {"tier": 4, "model": "cohere.command-r-plus", "region": "us-west-2"},
]
SEMANTIC_CACHE_TIER = 5  # last resort: the "runbook brain"

# Latency budget (ms). Breaching p99 sheds to the next tier BEFORE a hard 5xx.
P99_LATENCY_BUDGET_MS = 1500

# Destructive tools require elevation and are the first to lose authority on degradation.
DESTRUCTIVE_TOOLS = {"k8s.cordon_drain", "asg.scale", "github.revert_pr"}

# Agent Gateway: when the brain has fallen back this far, revoke destructive authority.
AUTONOMY_REVOKE_AT_DEPTH = 2

# Min replica floor — a Pre-Tool guardrail rejects asg.scale below this (kills "scale to 0").
MIN_REPLICA_FLOOR = 2

STATE_DIR = os.getenv("DEADMAN_STATE_DIR", ".deadman_state")
