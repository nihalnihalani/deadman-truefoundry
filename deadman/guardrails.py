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
import re
import deadman.config as config


# ── Exception hierarchy ──────────────────────────────────────────────────────

class GuardrailBlock(Exception):
    """Raised when a pre- or post-tool guardrail rejects a call or its result."""


class GatewayGuardrailBlock(GuardrailBlock):
    """Raised when the TFY AI Gateway blocks an LLM call at its own guardrail layer.

    This is the input-side counterpart to the Pre/Post-Tool guardrails above: it
    corresponds to the gateway-enforced rules declared in infra/guardrails.yaml that
    have no Python detector (notably ``block-prompt-injection``). The real-mode AI
    client (deadman.realmode_ai) detects a guardrail violation in the gateway's error
    response and raises this so the incident commander can treat hostile/blocked input
    as a *handled* failure — degrade to a safe hold — rather than crashing or, worse,
    reasoning on injected content. Subclasses GuardrailBlock so existing handlers that
    already catch guardrail blocks keep working.
    """


class ScopeDenied(Exception):
    """Raised when a destructive tool is outside the agent's current allowed scope."""


# ── Pre-Tool validator ────────────────────────────────────────────────────────

# Prod-critical namespaces that require an elevation token before cordon/drain.
PROD_CRITICAL_NAMESPACES: frozenset[str] = frozenset({
    "production", "prod", "prod-eu", "prod-us", "prod-ap", "prod-critical",
})

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_ACCESS_KEY]"),
    (re.compile(r"(?i)(aws_secret_access_key\s*=\s*)[A-Za-z0-9/+=]{24,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(token|api[_-]?key|password|secret)(\s*[:=]\s*)[^\s,;]+"), r"\1\2[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[REDACTED_JWT]"),
)


def redact_text(text: str) -> str:
    """Redact common secret forms before status updates or model-visible text."""
    redacted = text
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _redact_payload(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        redacted = {}
        for k, v in value.items():
            if re.search(r"(?i)(token|api[_-]?key|password|secret)", str(k)):
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = _redact_payload(v)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(v) for v in value]
    return value


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
    ③ statuspage.post secret redaction
        → redact-secrets-before-statuspage: strip common credentials from the
          client payload before it reaches either the model-visible audit trail
          or the real gateway. The TFY gateway rule remains the server-side
          backstop.
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

    # ③ statuspage secret redaction. Mutates args in place so callers keep the
    # same API while sending the safer payload downstream.
    if tool == "statuspage.post":
        args.update(_redact_payload(dict(args)))


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
