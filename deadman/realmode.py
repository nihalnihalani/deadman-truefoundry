"""Real-mode compatibility shim.

The real-mode clients were split into focused modules so the AI-Gateway path and the
MCP-Gateway path evolve independently:

    deadman.realmode_ai   -> TFY AI Gateway (OpenAI-compatible) -> AWS Bedrock fallback chain
    deadman.realmode_mcp  -> TFY MCP Gateway (Cedar default-deny + idempotency + OTel audit)

This shim preserves the original `realmode.complete` / `realmode.call_tool` entrypoints.
"""
from __future__ import annotations

from deadman.realmode_ai import complete, RESILIENT_MODEL  # noqa: F401
from deadman.realmode_mcp import call_tool  # noqa: F401

__all__ = ["complete", "call_tool", "RESILIENT_MODEL"]
