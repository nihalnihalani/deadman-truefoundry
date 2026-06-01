"""TrueFoundry MCP Gateway + Guardrails — mock AND real paths.

Sits between the agent and tools.  Enforces: Cedar-style DEFAULT-DENY scope,
Pre-Tool guardrails (validate args before execution), idempotency via the audit
log (exactly-once), and Post-Tool guardrails (inspect tool results before the
model sees them — the "bad intermediate output" cascade-breaker).  Every call
is audited.

Mode selection
--------------
  Mock  (config.MODE != "real"):  side-effects via self.world.*; chaos kill/
                                   corrupt injection active.
  Real  (config.is_real()):       side-effects via realmode_mcp.call_tool();
                                   no chaos injection.  The TFY gateway provides
                                   its own Idempotency-Key enforcement; the local
                                   audit-log dedup is an additional defense layer.

Exactly-once model (honest, three layers of defense)
----------------------------------------------------
  1. Provider-side Idempotency-Key — PRIMARY guarantee, enforced by the TFY MCP
     Gateway. A replayed call with the same key never re-runs the side effect
     (returns 409 / idempotent_replay).
  2. Race-safe durable claim ledger — DEFENSE-IN-DEPTH. The COMMIT step uses
     AuditLog.claim_commit(key, tool), an ATOMIC check-and-write that closes the
     check-then-act TOCTOU race: only one concurrent worker / replay can win the
     claim for a given key; the loser is folded into SKIPPED_IDEMPOTENT. The cheap
     is_committed() pre-check at the top of execute() is just a fast path; claim_commit
     is the authoritative guard.
  3. System-of-record reconcile on resume — after a kill mid-action (PENDING but not
     COMMITTED), the resume path reconciles against the live system-of-record before
     re-acting, so a side effect that already landed is never replayed (RealWorld;
     owned by Anchor).

Compatibility
-------------
GuardrailBlock, ScopeDenied, KillSignal, ToolResult, and MCPGateway are all
importable directly from this module — no existing caller needs to change.
GuardrailBlock and ScopeDenied live in guardrails.py (single source of truth)
and are re-exported here.
"""
from __future__ import annotations
from dataclasses import dataclass
import deadman.config as config

# ── Re-export the exception classes from guardrails so that all existing imports
#    (e.g. `from deadman.mcp_gateway import GuardrailBlock, ScopeDenied`) keep
#    working without any changes to commander.py or other teammates' files.
from deadman.guardrails import GuardrailBlock, ScopeDenied  # noqa: F401
import deadman.guardrails as _guardrails

# [PULSE] Observability helpers — lazy, no-op when prometheus_client absent
import deadman.metrics as _metrics
import deadman.otel as _otel


class KillSignal(Exception):
    """Simulates a SIGKILL mid-action (process death between side effect and COMMIT)."""


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

    # ── guardrails ────────────────────────────────────────────────────────────

    def _pre_tool(self, tool: str, args: dict):
        """Delegate to guardrails.pre_tool_validate; track block count."""
        try:
            _guardrails.pre_tool_validate(tool, args)
        except GuardrailBlock:
            self.guardrail_blocks += 1
            raise

    def _post_tool(self, tool: str, raw):
        """Delegate to guardrails.post_tool_validate.

        In mock mode the chaos.corrupt_output flag is the `corrupt` signal so
        the existing chaos-driven behaviour is fully preserved.
        """
        corrupt = bool(
            self.chaos and self.chaos.corrupt_output and tool.startswith(("cw.", "logs."))
        )
        try:
            return _guardrails.post_tool_validate(tool, raw, corrupt=corrupt)
        except GuardrailBlock:
            self.guardrail_blocks += 1
            raise

    # ── the governed execute path ─────────────────────────────────────────────

    def execute(self, tool: str, args: dict, key: str, allowed_scope: set) -> ToolResult:
        """Execute a tool call with Cedar scope enforcement + guardrails + exactly-once.

        The mock path and the real path share the same Cedar / audit / guardrail
        wrapper; only the inner side-effect call differs.
        """
        # Cedar default-deny: destructive verbs must be in the (possibly degraded)
        # allowed scope.
        if tool in config.DESTRUCTIVE_TOOLS and tool not in allowed_scope:
            self.audit.write({"status": "DENIED", "tool": tool, "key": key,
                              "reason": "scope/autonomy"})
            # [PULSE] Record scope-denied metric (no-op when prometheus_client absent)
            _metrics.record_scope_denied(tool)
            raise ScopeDenied(
                f"{tool} denied — not in allowed scope {sorted(allowed_scope)}"
            )

        # Exactly-once: if the audit log already shows COMMITTED for this key,
        # skip the side effect entirely.  This is defense-in-depth on top of the
        # gateway Idempotency-Key in real mode; it's the primary guard in mock mode.
        if self.audit.is_committed(key):
            self.audit.write({"status": "SKIPPED_IDEMPOTENT", "tool": tool, "key": key})
            # [PULSE] Record skipped-idempotent tool call metric
            _metrics.observe_tool(tool, "SKIPPED_IDEMPOTENT", 0.0)
            return ToolResult("SKIPPED_IDEMPOTENT")

        # Pre-tool guardrail (raises GuardrailBlock if invalid).
        self._pre_tool(tool, args)
        self.audit.write({"status": "PENDING", "tool": tool, "key": key})

        # ── branch on mode ────────────────────────────────────────────────────
        # [PULSE] Time the inner execution; record metric regardless of outcome
        import time as _time
        _t0 = _time.monotonic()
        _result_label = "ERROR"
        try:
            with _otel.span("mcp.execute", tool=tool, key=key):
                if config.is_real():
                    _result = self._execute_real(tool, args, key)
                else:
                    _result = self._execute_mock(tool, args, key)
            _result_label = _result.status
            return _result
        except GuardrailBlock:
            _result_label = "GUARDRAIL_BLOCK"
            _metrics.record_guardrail_block(tool)
            raise
        except ScopeDenied:
            _result_label = "SCOPE_DENIED"
            raise
        except Exception:
            raise
        finally:
            _latency = _time.monotonic() - _t0
            _metrics.observe_tool(tool, _result_label, _latency)

    # ── real execution path ───────────────────────────────────────────────────

    def _execute_real(self, tool: str, args: dict, key: str) -> ToolResult:
        """Route to the live TFY MCP Gateway.

        No chaos injection in real mode — chaos is demo-only.
        The TFY gateway handles Cedar policy; we still hold the pre-tool
        guardrail above as a client-side guard.

        After a confirmed successful (non-skipped) destructive tool call,
        self.world.<verb> is called to record intent — matching the mock path.
        RealWorld.<verb> only RECORDS intent (it does NOT call the gateway again),
        so there is no double-execution risk.  This makes reconciliation path (a)
        (in-memory intent log) live in real mode, matching the docstring claim.
        """
        from deadman import realmode_mcp  # lazy import — only needed in real mode

        result = realmode_mcp.call_tool(tool, args, key)

        # The gateway may honour the Idempotency-Key and signal a replay.
        if result.get("skipped_idempotent"):
            self.audit.write({"status": "SKIPPED_IDEMPOTENT", "tool": tool, "key": key,
                              "source": "gateway"})
            return ToolResult("SKIPPED_IDEMPOTENT")

        body = result["body"]

        # Post-tool guardrail — no chaos flag in real mode.
        value = self._post_tool(tool, body)

        # Atomic claim — closes the check-then-act TOCTOU on COMMIT. Loser of a
        # concurrent/replayed claim folds into SKIPPED_IDEMPOTENT (exactly-once).
        won = self.audit.claim_commit(key, tool)
        if not won:
            return ToolResult("SKIPPED_IDEMPOTENT")

        # Record intent on the world adapter — RECORDS only, no re-execution.
        # Guard with hasattr so a world without a given method (e.g. a stub in tests)
        # will not crash.  This mirrors the mock path and makes RealWorld._applied live.
        if tool == "github.revert_pr":
            if hasattr(self.world, "revert_pr"):
                self.world.revert_pr(args.get("pr", ""), key)
        elif tool == "k8s.cordon_drain":
            if hasattr(self.world, "cordon_drain"):
                self.world.cordon_drain(args.get("node", ""), key)
        elif tool == "asg.scale":
            if hasattr(self.world, "asg_scale"):
                self.world.asg_scale(args.get("asg", ""), args.get("replicas", 0), key)

        return ToolResult("EXECUTED", value)

    # ── mock execution path (UNCHANGED behaviour) ─────────────────────────────

    def _execute_mock(self, tool: str, args: dict, key: str) -> ToolResult:
        """Run against the in-process World stub; inject chaos as before."""
        # ---- the actual side effect ----
        if tool == "github.revert_pr":
            self.world.revert_pr(args["pr"], key)
        elif tool == "k8s.cordon_drain":
            self.world.cordon_drain(args["node"], key)
        elif tool == "asg.scale":
            self.world.asg_scale(args["asg"], args["replicas"], key)
        raw = args.get("_returns")

        # Chaos can SIGKILL right here — between the side effect and COMMITTED.
        if self.chaos and self.chaos.kill_after == key:
            raise KillSignal(key)

        value = self._post_tool(tool, raw)

        # Atomic claim — closes the check-then-act TOCTOU on COMMIT. Loser of a
        # concurrent/replayed claim folds into SKIPPED_IDEMPOTENT (exactly-once).
        # NOTE: the KillSignal above fires BEFORE this point, so claim_commit is
        # simply not reached on the kill path; resume reconcile (Anchor) handles it.
        won = self.audit.claim_commit(key, tool)
        if not won:
            return ToolResult("SKIPPED_IDEMPOTENT")
        return ToolResult("EXECUTED", value)
