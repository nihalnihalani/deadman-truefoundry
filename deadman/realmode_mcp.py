"""Real-mode MCP client — TFY MCP Gateway (Cedar default-deny + idempotency + OTel audit).

OWNER: Vault. This module is fully implemented by the MCP/Guardrails teammate. The
placeholder below documents the required interface so other modules can import it.
"""
from __future__ import annotations


def call_tool(tool: str, args: dict, idempotency_key: str) -> dict:
    """Invoke an MCP tool through the gateway with exactly-once idempotency.

    Returns: {"status_code": int, "body": object, "skipped_idempotent": bool}
    """
    raise NotImplementedError("realmode_mcp.call_tool — implemented by Vault")
