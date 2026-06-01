"""DEADMAN config — the Bedrock fallback chain, tool scopes, and thresholds.

Mock mode (default) runs the whole thing on the stdlib. Set DEADMAN_MODE=real and
fill .env to route through the real TrueFoundry AI Gateway + MCP Gateway + Bedrock.

Model IDs (June 2026): the fallback chain leads with Claude Opus 4.8 and falls cross-region
then cross-provider. Because Bedrock deprecates/renames model IDs and newer models use
inference-profile prefixes (global./us.), real mode resolves the *exact* modelId at startup
via boto3 (see deadman.realmode_ai.resolve_model_id) rather than trusting a hardcoded string.
The `model` field below is the FAMILY HINT used for that resolution and for trace tagging.
"""
import os

MODE = os.getenv("DEADMAN_MODE", "mock")

# The Bedrock fallback chain — each tier is tagged in the AI Gateway trace.
# `family` is the resolution hint (substring matched against ListFoundationModels / inference
# profiles); `model` is the best-known explicit id (used as-is if resolution is unavailable).
# tier 0 -> 1 is the literal May 7-8 2026 us-east-1 cross-region failover.
FALLBACK_CHAIN = [
    {"tier": 0, "family": "claude-opus-4-8", "model": "anthropic.claude-opus-4-8", "region": "us-east-1", "provider": "anthropic"},
    {"tier": 1, "family": "claude-opus-4-8", "model": "anthropic.claude-opus-4-8", "region": "us-west-2", "provider": "anthropic"},
    {"tier": 2, "family": "llama4-maverick", "model": "meta.llama4-maverick-17b-instruct-v1:0", "region": "us-west-2", "provider": "meta"},
    {"tier": 3, "family": "mistral-large", "model": "mistral.mistral-large-2407-v1:0", "region": "us-west-2", "provider": "mistral"},
    {"tier": 4, "family": "command-r-plus", "model": "cohere.command-r-plus-v1:0", "region": "us-west-2", "provider": "cohere"},
]
SEMANTIC_CACHE_TIER = 5  # last resort: the "runbook brain"

# Latency budget (ms). Breaching p99 sheds to the next tier BEFORE a hard 5xx.
P99_LATENCY_BUDGET_MS = int(os.getenv("DEADMAN_P99_BUDGET_MS", "1500"))

# Destructive tools require elevation and are the first to lose authority on degradation.
DESTRUCTIVE_TOOLS = {"k8s.cordon_drain", "asg.scale", "github.revert_pr"}

# Agent Gateway: when the brain has fallen back this far, revoke destructive authority.
AUTONOMY_REVOKE_AT_DEPTH = int(os.getenv("DEADMAN_REVOKE_AT_DEPTH", "2"))

# Min replica floor — a Pre-Tool guardrail rejects asg.scale below this (kills "scale to 0").
MIN_REPLICA_FLOOR = int(os.getenv("DEADMAN_MIN_REPLICA_FLOOR", "2"))

STATE_DIR = os.getenv("DEADMAN_STATE_DIR", ".deadman_state")

# ---- Real-mode wiring (only read when MODE == "real") -----------------------------------
# TrueFoundry AI Gateway (OpenAI-compatible) + MCP Gateway endpoints and the virtual model
# whose fallback chain is declared in infra/ai_gateway.yaml.
TFY_API_KEY = os.getenv("TFY_API_KEY", "")
TFY_GATEWAY_BASE_URL = os.getenv("TFY_GATEWAY_BASE_URL", "")
TFY_MCP_GATEWAY_URL = os.getenv("TFY_MCP_GATEWAY_URL", "")
TFY_RESILIENT_MODEL = os.getenv("TFY_RESILIENT_MODEL", "deadman-resilient-bedrock")
TFY_METADATA = os.getenv("TFY_METADATA", "app=deadman,role=incident-commander")

# AWS / Bedrock — used by the live model-id resolver and (optionally) the DynamoDB state store.
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_FALLBACK_REGION = os.getenv("AWS_FALLBACK_REGION", "us-west-2")

# State backend: "file" (default, survives process death) or "dynamodb" (production).
STATE_BACKEND = os.getenv("DEADMAN_STATE_BACKEND", "file")
DYNAMODB_TABLE = os.getenv("DEADMAN_DYNAMODB_TABLE", "deadman-incident-state")

# OpenTelemetry — when set, the MCP audit log + spans export here (Grafana/Datadog/Splunk).
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "deadman-incident-commander")


def is_real() -> bool:
    return MODE == "real"
