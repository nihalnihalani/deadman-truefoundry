"""Pure, reusable guardrail validators for the DEADMAN MCP Gateway.

These functions have NO side-effects (no I/O, no state mutation) so they are
trivially unit-testable and callable from both the mock and real execution paths.

Design notes
------------
* GuardrailBlock / ScopeDenied are defined here as the single source of truth.
  mcp_gateway.py imports them and re-exports them so existing callers are unaffected.
* pre_tool_validate  — runs BEFORE a tool's side-effect (Cedar Pre-Tool hook).
  Maps to guardrails.yaml rules:
    - block-scale-to-zero          → asg.scale replicas < MIN_REPLICA_FLOOR
    - prod-drain-needs-elevation   → k8s.cordon_drain without elevation token
    - redact-secrets-before-statuspage → hook point, documented below (no-op, handled in yaml)
* post_tool_validate — runs AFTER a tool returns, BEFORE the model sees the result
  (Cedar Post-Tool hook).  Maps to guardrails.yaml rule:
    - catch-corrupt-tool-output    → cw./logs. JSON validity / truncation check
"""
from __future__ import annotations
import json
import deadman.config as config


# ── Exception hierarchy ──────────────────────────────────────────────────────

class GuardrailBlock(Exception):
    """Raised when a pre- or post-tool guardrail rejects a call or its result."""


class ScopeDenied(Exception):
    """Raised when a destructive tool is outside the agent's current allowed scope."""


# ── Pre-Tool validator ────────────────────────────────────────────────────────

# Prod-critical namespaces that require an elevation token before cordon/drain.
PROD_CRITICAL_NAMESPACES: frozenset[str] = frozenset({
    "production", "prod", "prod-eu", "prod-us", "prod-ap", "prod-critical",
})


def pre_tool_validate(tool: str, args: dict) -> None:
    """Validate tool + args BEFORE the side-effect is executed.

    Raises GuardrailBlock if the call violates a guardrail.
    Returns None when all checks pass (callers may ignore the return value).

    Rules (match guardrails.yaml):
    ① asg.scale with replicas < MIN_REPLICA_FLOOR
        → block-scale-to-zero: prevents a hallucinated "scale to 0" from
          draining every replica in production.
    ② k8s.cordon_drain on a prod-critical namespace without elevation
        → prod-drain-needs-elevation: destructive drain of a live namespace
          requires an explicit elevation token in the call args.
    ③ statuspage.post secret/PII redaction
        → redact-secrets-before-statuspage: in the real TFY gateway the
          YAML rule handles this server-side; here we document the hook point
          so a future Python-side pass can strip secrets before the call
          reaches the gateway (currently no-op, defense-in-depth if needed).
    """
    # ① asg.scale floor check
    if tool == "asg.scale":
        replicas = args.get("replicas", 0)
        if replicas < config.MIN_REPLICA_FLOOR:
            raise GuardrailBlock(
                f"Pre-Tool: asg.scale replicas={replicas} below floor "
                f"{config.MIN_REPLICA_FLOOR} (MIN_REPLICA_FLOOR)"
            )

    # ② cordon/drain elevation check
    if tool == "k8s.cordon_drain":
        namespace = args.get("namespace", "")
        if namespace in PROD_CRITICAL_NAMESPACES and not args.get("elevation"):
            raise GuardrailBlock(
                f"Pre-Tool: k8s.cordon_drain on prod-critical namespace "
                f"'{namespace}' requires args['elevation'] to be truthy"
            )

    # ③ statuspage secret-redaction hook point
    # When tool == "statuspage.post" we could strip secrets/PII here before the
    # payload reaches the gateway.  The TFY guardrails.yaml rule
    # (redact-secrets-before-statuspage) handles this server-side; this is a
    # belt-and-suspenders client-side no-op placeholder.
    # Future: scan args["message"] for AWS key patterns, JWTs, etc. and redact.


# ── Post-Tool validator ───────────────────────────────────────────────────────

def _is_balanced_json(text: str) -> bool:
    """Return True iff `text` parses as valid JSON (no truncation / unbalanced braces)."""
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, TypeError):
        return False


def _looks_truncated(text: str) -> bool:
    """Heuristic: unbalanced braces/brackets indicate a truncated payload."""
    opens = text.count("{") + text.count("[")
    closes = text.count("}") + text.count("]")
    return opens != closes


def post_tool_validate(tool: str, raw, *, corrupt: bool = False):
    """Inspect a tool result BEFORE the model sees it.

    Raises GuardrailBlock when the result is unsafe to pass upstream.
    Returns raw unchanged when valid.

    Rules (match guardrails.yaml → catch-corrupt-tool-output):
    ① corrupt=True: caller (MCPGateway) explicitly signals chaos/corruption;
      block for cw.* / logs.* tools only.
    ② cw.*/logs.* str payloads: must parse as valid JSON and must not look truncated
      (unbalanced braces).  A degraded CloudWatch / log API commonly returns partial
      payloads.  NON-metrics tools (e.g. github.revert_pr) are NOT JSON-validated —
      a plain-text success body is legitimate and must pass.
    """
    is_metrics_or_logs = tool.startswith(("cw.", "logs."))

    # ① explicit corruption flag — only meaningful for metrics/log tools
    if corrupt and is_metrics_or_logs:
        raise GuardrailBlock(
            f"Post-Tool: corrupt/truncated output from {tool} — forcing re-fetch"
        )

    # ② structural validation for string payloads — METRICS/LOGS TOOLS ONLY.
    # Non-metrics tools (e.g. github.revert_pr) may legitimately return a plain-text
    # success body that is not JSON. Validating those would wrongly block AFTER the
    # side effect ran but BEFORE the COMMIT, manufacturing the exact
    # PENDING-not-COMMITTED state that enables double-execution. So we gate the
    # structural check on cw./logs. tools only, exactly like the `corrupt` branch.
    if is_metrics_or_logs and isinstance(raw, str):
        if not _is_balanced_json(raw) or _looks_truncated(raw):
            raise GuardrailBlock(
                f"Post-Tool: result from {tool} failed JSON/truncation check — "
                "payload appears malformed or truncated"
            )

    return raw
