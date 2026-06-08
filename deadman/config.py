"""DEADMAN config — the Bedrock fallback chain, tool scopes, and thresholds.

Mock mode (default) runs the whole thing on the stdlib. Set DEADMAN_MODE=real and
fill .env to route through the real TrueFoundry AI Gateway + MCP Gateway + Bedrock.

Model IDs (June 2026): the fallback chain leads with Claude Opus 4.8 and falls cross-region
then cross-provider. Because Bedrock deprecates/renames model IDs and newer models use
inference-profile prefixes (global./us.), real mode resolves the *exact* modelId at startup
via boto3 (see deadman.realmode_ai.resolve_model_id) rather than trusting a hardcoded string.
The `model` field below is the FAMILY HINT used for that resolution and for trace tagging.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, TypedDict

_load_dotenv: Callable[..., bool] | None
try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # mock-mode demo scripts must keep working on the stdlib
    _load_dotenv = None

if _load_dotenv is not None:
    _load_dotenv()

MODE = os.getenv("DEADMAN_MODE", "mock")

class TierConfig(TypedDict):
    """One tier in the Bedrock fallback chain."""
    tier: int
    family: str
    model: str
    region: str
    provider: str


# The Bedrock fallback chain — each tier is tagged in the AI Gateway trace.
# `family` is the resolution hint (substring matched against ListFoundationModels / inference
# profiles); `model` is the best-known explicit id (used as-is if resolution is unavailable).
# tier 0 -> 1 is the literal May 7-8 2026 us-east-1 cross-region failover.
FALLBACK_CHAIN: list[TierConfig] = [
    {"tier": 0, "family": "claude-opus-4-8", "model": "anthropic.claude-opus-4-8", "region": "us-east-1", "provider": "anthropic"},
    {"tier": 1, "family": "claude-opus-4-8", "model": "anthropic.claude-opus-4-8", "region": "us-west-2", "provider": "anthropic"},
    {"tier": 2, "family": "llama4-maverick", "model": "meta.llama4-maverick-17b-instruct-v1:0", "region": "us-west-2", "provider": "meta"},
    {"tier": 3, "family": "mistral-large-3", "model": "mistral.mistral-large-3-2506-v1:0", "region": "us-west-2", "provider": "mistral"},
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
TFY_MCP_TRANSPORT = os.getenv("TFY_MCP_TRANSPORT", "auto")
TFY_RESILIENT_MODEL = os.getenv("TFY_RESILIENT_MODEL", "deadman-resilient-bedrock")
def _parse_metadata(raw: str) -> str:
    """Convert key=value,... or JSON string to a JSON object string for x-tfy-metadata."""
    raw = raw.strip()
    if raw.startswith("{"):
        return raw
    result = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            result[k.strip()] = v.strip()
    return json.dumps(result)


TFY_METADATA = _parse_metadata(os.getenv("TFY_METADATA", "app=deadman,role=incident-commander"))

# AWS / Bedrock — used by the live model-id resolver and (optionally) the DynamoDB state store.
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_FALLBACK_REGION = os.getenv("AWS_FALLBACK_REGION", "us-west-2")

# State backend: "file" (default, survives process death) or "dynamodb" (production).
STATE_BACKEND = os.getenv("DEADMAN_STATE_BACKEND", "file")
DYNAMODB_TABLE = os.getenv("DEADMAN_DYNAMODB_TABLE", "deadman-incident-state")

# OpenTelemetry — when set, the MCP audit log + spans export here (Grafana/Datadog/Splunk).
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "deadman-incident-commander")

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_INCIDENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def is_real() -> bool:
    return MODE == "real"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return default


def webhook_secret() -> str:
    return os.getenv("DEADMAN_WEBHOOK_SECRET", "")


def demo_enabled() -> bool:
    """Return whether demo/chaos endpoints should be exposed.

    The safe default is mode-aware: mock mode exposes the deterministic demo, real mode
    hides it unless the operator explicitly sets DEADMAN_ENABLE_DEMO=1.
    """
    return _env_flag("DEADMAN_ENABLE_DEMO", default=not is_real())


def validate_incident_id(incident_id: str) -> str:
    """Validate an incident id before it is used in file paths or idempotency keys."""
    if not _INCIDENT_ID_RE.fullmatch(incident_id):
        raise ValueError(
            "incident_id must be 1-128 chars and contain only letters, numbers, "
            "underscore, dash, colon, or dot"
        )
    return incident_id


def production_issues() -> list[dict[str, Any]]:
    """Return configuration issues that matter for readiness.

    Entries have shape {"severity": "error"|"warning", "field": str, "message": str}.
    Errors make /readyz return 503. Warnings are surfaced so operators do not miss
    non-blocking production risks.
    """
    issues: list[dict[str, Any]] = []

    if MODE not in {"mock", "real"}:
        issues.append({
            "severity": "error",
            "field": "DEADMAN_MODE",
            "message": "must be either 'mock' or 'real'",
        })

    if STATE_BACKEND not in {"file", "dynamodb"}:
        issues.append({
            "severity": "error",
            "field": "DEADMAN_STATE_BACKEND",
            "message": "must be either 'file' or 'dynamodb'",
        })

    if TFY_MCP_TRANSPORT not in {"auto", "mcp", "rest"}:
        issues.append({
            "severity": "error",
            "field": "TFY_MCP_TRANSPORT",
            "message": "must be one of 'auto', 'mcp', or 'rest'",
        })

    if STATE_BACKEND == "dynamodb" and not DYNAMODB_TABLE:
        issues.append({
            "severity": "error",
            "field": "DEADMAN_DYNAMODB_TABLE",
            "message": "is required when DEADMAN_STATE_BACKEND=dynamodb",
        })

    if is_real():
        required = {
            "TFY_API_KEY": TFY_API_KEY,
            "TFY_GATEWAY_BASE_URL": TFY_GATEWAY_BASE_URL,
            "TFY_MCP_GATEWAY_URL": TFY_MCP_GATEWAY_URL,
        }
        for field, value in required.items():
            if not value:
                issues.append({
                    "severity": "error",
                    "field": field,
                    "message": "is required in DEADMAN_MODE=real",
                })

        for field, value in {
            "TFY_GATEWAY_BASE_URL": TFY_GATEWAY_BASE_URL,
            "TFY_MCP_GATEWAY_URL": TFY_MCP_GATEWAY_URL,
        }.items():
            if value and not value.startswith(("https://", "http://")):
                issues.append({
                    "severity": "error",
                    "field": field,
                    "message": "must be an http(s) URL",
                })

        if not webhook_secret():
            issues.append({
                "severity": "error",
                "field": "DEADMAN_WEBHOOK_SECRET",
                "message": "is required in real mode so /incident fails closed",
            })

        if demo_enabled():
            issues.append({
                "severity": "error",
                "field": "DEADMAN_ENABLE_DEMO",
                "message": "must be unset or 0 in real mode unless intentionally running a live demo",
            })

        if STATE_BACKEND == "file":
            issues.append({
                "severity": "warning",
                "field": "DEADMAN_STATE_BACKEND",
                "message": "file backend is single-host only; use dynamodb for multi-replica production",
            })

        if float(os.getenv("DEADMAN_RATE_LIMIT_RPS", "0")) <= 0:
            issues.append({
                "severity": "warning",
                "field": "DEADMAN_RATE_LIMIT_RPS",
                "message": "rate limiting is disabled in production; set DEADMAN_RATE_LIMIT_RPS=10 to enable",
            })

    return issues


def readiness() -> dict[str, Any]:
    issues = production_issues()
    errors = [issue for issue in issues if issue["severity"] == "error"]
    warnings = [issue for issue in issues if issue["severity"] == "warning"]
    return {
        "ok": not errors,
        "mode": MODE,
        "state_backend": STATE_BACKEND,
        "demo_enabled": demo_enabled(),
        "errors": errors,
        "warnings": warnings,
    }


def require_ai_gateway_config() -> None:
    missing = [
        field for field, value in {
            "TFY_API_KEY": TFY_API_KEY,
            "TFY_GATEWAY_BASE_URL": TFY_GATEWAY_BASE_URL,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(f"Missing AI Gateway config: {', '.join(missing)}")


def require_mcp_gateway_config() -> None:
    missing = [
        field for field, value in {
            "TFY_API_KEY": TFY_API_KEY,
            "TFY_MCP_GATEWAY_URL": TFY_MCP_GATEWAY_URL,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(f"Missing MCP Gateway config: {', '.join(missing)}")
