"""Real-mode AI client — direct AWS Bedrock (boto3) fallback chain.

OWNER: Atlas.

This is the second real-mode LLM backend (selected with DEADMAN_LLM_BACKEND=bedrock).
It calls Amazon Bedrock's Converse API directly via boto3 using the ambient AWS
credentials (env vars, shared ~/.aws config, instance role, …) — no TrueFoundry AI
Gateway required.  It walks config.BEDROCK_FALLBACK_CHAIN tier by tier: the first tier
to answer wins, and its tier index becomes the fallback_depth that the Agent Gateway
auto-leash subscribes to (same contract as deadman.realmode_ai.complete).

There is no semantic-cache layer on the direct path, so from_cache is always False; the
AIGateway's degradation tracking still works because fallback_depth climbs as live tiers
drop out.

complete() returns the same dict shape as deadman.realmode_ai.complete so AIGateway can
treat the two backends interchangeably:
    {"text", "served_by", "fallback_depth", "from_cache", "raw"}
"""
from __future__ import annotations

import logging
from typing import Any

import deadman.config as config

log = logging.getLogger(__name__)


class BedrockOutage(Exception):
    """Raised when every tier in the Bedrock fallback chain fails."""


# Module-level client cache: region -> bedrock-runtime client.  boto3 clients are
# thread-safe for calls and creating one per request is wasteful, so we memoise per region.
_clients: dict[str, Any] = {}


def _runtime_client(region: str) -> Any:
    """Return a cached bedrock-runtime client for *region* (lazy boto3 import)."""
    client = _clients.get(region)
    if client is None:
        import boto3  # type: ignore[import]  # lazy: keeps mock mode free of boto3

        client = boto3.client("bedrock-runtime", region_name=region)
        _clients[region] = client
    return client


def _extract_text(response: dict) -> str:
    """Pull the assistant text out of a Converse response, tolerating shape drift."""
    try:
        blocks = response["output"]["message"]["content"]
    except (KeyError, TypeError):
        return ""
    parts = [b["text"] for b in blocks if isinstance(b, dict) and "text" in b]
    return "".join(parts)


def complete(prompt: str) -> dict:
    """One governed completion through the Bedrock Converse API with tier fallback.

    Walks config.BEDROCK_FALLBACK_CHAIN in order.  The first tier whose Converse call
    succeeds answers the request; its tier index is returned as fallback_depth so the
    Agent Gateway can revoke destructive authority once degradation is deep enough.

    Returns:
        {
          "text": str,
          "served_by": str,           # "<family>@<region>" of the tier that answered
          "fallback_depth": int,      # 0 = primary tier; increments per fallback hop
          "from_cache": bool,         # always False (no cache layer on the direct path)
          "raw": dict                 # the raw boto3 Converse response
        }

    Raises:
        BedrockOutage — when every tier in the chain fails (caller should catch).
    """
    chain = config.BEDROCK_FALLBACK_CHAIN
    last_error: Exception | None = None

    for entry in chain:
        region = entry["region"]
        model_id = entry["model"]
        family = entry["family"]
        try:
            client = _runtime_client(region)
            response = client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={
                    "maxTokens": config.BEDROCK_MAX_TOKENS,
                    "temperature": 0,
                },
            )
            text = _extract_text(response)
            served_by = f"{family}@{region}"
            log.info(
                "Bedrock served tier %d (%s) for prompt[:40]=%r",
                entry["tier"], served_by, prompt[:40],
            )
            return {
                "text": text,
                "served_by": served_by,
                "fallback_depth": entry["tier"],
                "from_cache": False,
                "raw": response,
            }
        except Exception as exc:  # noqa: BLE001 — any tier failure should shed to the next
            last_error = exc
            log.warning(
                "Bedrock tier %d (%s@%s) failed: %s; trying next tier",
                entry["tier"], family, region, exc,
            )
            continue

    raise BedrockOutage(
        f"all {len(chain)} Bedrock tiers failed; last error: {last_error}"
    )
