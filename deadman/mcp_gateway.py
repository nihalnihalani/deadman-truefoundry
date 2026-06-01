"""Mock TrueFoundry MCP Gateway + Guardrails.

Sits between the agent and tools. Enforces: Cedar-style DEFAULT-DENY scope, Pre-Tool
guardrails (validate args before execution), idempotency via the audit log (exactly-once),
and Post-Tool guardrails (inspect tool results before the model sees them — the
"bad intermediate output" cascade-breaker). Every call is audited.
"""
from __future__ import annotations
from dataclasses import dataclass
import deadman.config as config


class GuardrailBlock(Exception):
    pass


class ScopeDenied(Exception):
    pass


@dataclass
class ToolResult:
    status: str            # EXECUTED | SKIPPED_IDEMPOTENT
    value: object = None


class MCPGateway:
    def __init__(self, world, audit, chaos=None):
        self.world = world
        self.audit = audit
        self.chaos = chaos
        self.guardrail_blocks = 0

    # ---- guardrails ----
    def _pre_tool(self, tool: str, args: dict):
        if tool == "asg.scale" and args.get("replicas", 0) < config.MIN_REPLICA_FLOOR:
            self.guardrail_blocks += 1
            raise GuardrailBlock(f"Pre-Tool: asg.scale replicas={args['replicas']} below floor {config.MIN_REPLICA_FLOOR}")
        # (redaction of secrets/PII before statuspage.post would live here)

    def _post_tool(self, tool: str, raw):
        # The cascade-breaker: a degraded API returns truncated/garbage JSON.
        if self.chaos and self.chaos.corrupt_output and tool.startswith(("cw.", "logs.")):
            self.guardrail_blocks += 1
            raise GuardrailBlock(f"Post-Tool: corrupt/truncated output from {tool} — forcing re-fetch")
        return raw

    # ---- the governed execute path ----
    def execute(self, tool: str, args: dict, key: str, allowed_scope: set) -> ToolResult:
        # Cedar default-deny: destructive verbs must be in the (possibly degraded) allowed scope.
        if tool in config.DESTRUCTIVE_TOOLS and tool not in allowed_scope:
            self.audit.write({"status": "DENIED", "tool": tool, "key": key, "reason": "scope/autonomy"})
            raise ScopeDenied(f"{tool} denied — not in allowed scope {sorted(allowed_scope)}")

        # exactly-once: if the audit log already shows COMMITTED for this key, skip the side effect.
        if self.audit.is_committed(key):
            self.audit.write({"status": "SKIPPED_IDEMPOTENT", "tool": tool, "key": key})
            return ToolResult("SKIPPED_IDEMPOTENT")

        self._pre_tool(tool, args)
        self.audit.write({"status": "PENDING", "tool": tool, "key": key})

        # ---- the actual side effect ----
        if tool == "github.revert_pr":
            self.world.revert_pr(args["pr"], key)
        elif tool == "k8s.cordon_drain":
            self.world.cordon_drain(args["node"], key)
        elif tool == "asg.scale":
            self.world.asg_scale(args["asg"], args["replicas"], key)
        raw = args.get("_returns")

        # chaos can SIGKILL right here — between the side effect and COMMITTED.
        if self.chaos and self.chaos.kill_after == key:
            raise KillSignal(key)

        value = self._post_tool(tool, raw)
        self.audit.write({"status": "COMMITTED", "tool": tool, "key": key})
        return ToolResult("EXECUTED", value)


class KillSignal(Exception):
    """Simulates a SIGKILL mid-action (process death between side effect and COMMIT)."""
