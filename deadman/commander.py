"""The two agents.

NaiveAgent: raw Bedrock (us-east-1 only), in-process state, no gateway governance. On a
provider outage it loses everything; on a kill it restarts from scratch and DOUBLE-EXECUTES.

Deadman: routes every model call through the AI Gateway (fallback chain) and every tool
through the MCP Gateway (scoped + audited + idempotent), with the Agent Gateway revoking
authority as the brain degrades. Survives provider death and resumes exactly-once.

chaos=None is a valid and supported signature for Deadman. When chaos is None:
  - AIGateway runs without injected failures (real/healthy path).
  - MCPGateway runs without kill injection or corrupt-output simulation.
  - Phase B deepening fires a real AI re-plan instead of toggling a chaos knob.
  - All invariants (exactly-once, resume, scope, audit) are identical.

run_agentic() — the REAL agentic loop (Cortex deliverable):
  The LLM actually decides which tool to call via a text-based ReAct (Reason+Act)
  pattern over the existing AIGateway.complete() interface.  The planner parses
  the model's JSON-action text and drives reason→act→observe until resolved or
  budget exhausted.  Exactly-once and auto-leash are fully preserved.
"""
from __future__ import annotations
from dataclasses import dataclass, field

import deadman.config as config
from deadman.ai_gateway import AIGateway, ModelOutage
from deadman.mcp_gateway import MCPGateway, KillSignal, GuardrailBlock, ScopeDenied
from deadman.agent_gateway import AgentGateway
from deadman.state import DurableState, AuditLog
import deadman.tools as tools
import deadman.planner as planner
import deadman.guardrails as _guardrails

# [PULSE] Observability — lazy imports so mock mode / tests without the libs never crash
import deadman.otel as _otel
import deadman.metrics as _metrics


@dataclass
class Scoreboard:
    backend: str = "claude@us-east-1"
    fallback_depth: int = 0
    state_losses: int = 0
    double_executions: int = 0
    guardrail_blocks: int = 0
    drain_authority: str = "ON"
    survived: bool = False
    notes: list = field(default_factory=list)


# The guardrail-block-rate kill-switch is meant to catch a *spike* of blocks (a brain
# repeatedly attempting unsafe actions), not a single isolated block. Require a minimum
# sample of tool attempts before the rate is meaningful, so one blocked diagnostic can't
# latch full destructive-scope revocation.
KILL_SWITCH_MIN_ATTEMPTS = 3


def action_key(incident_id: str, action: str, target: str) -> str:
    """Stable per-incident idempotency key for provider-side deduplication."""
    return f"{config.validate_incident_id(incident_id)}::{action}::{target}"


# Backwards-compatible demo constants. Production code uses per-incident keys below.
DIAG_KEY = action_key("incident-42", "cw.get_metrics", "read")
CORDON_KEY = action_key("incident-42", "cordon_drain", "prod-node-7")
REVERT_KEY = action_key("incident-42", "revert_pr", "PR-1337")


class NaiveAgent:
    """No fallback, no durable state, no idempotency, no governance."""

    def __init__(self, world):
        self.world = world
        self.done_in_memory: list[str] = []   # lost on restart

    def run(self, chaos) -> Scoreboard:
        sb = Scoreboard()
        # Reasoning on raw us-east-1 — if the region is down, the agent just dies.
        if not chaos.tier_healthy(0, "us-east-1") or chaos.all_bedrock_down:
            sb.state_losses += 1
            sb.notes.append("raw Bedrock us-east-1 down -> agent stalled, in-process plan lost")
            # On restart it has no memory of what it did -> re-applies the rollback.
            self.world.revert_pr("PR-1337")           # 1st (pre-crash, assumed)
            self.world.revert_pr("PR-1337")           # 2nd on naive restart -> DOUBLE
            sb.double_executions = self.world.count("revert_pr") - 1
            sb.notes.append("restarted from step 1 -> re-fired the rollback (DOUBLE-EXECUTION)")
            return sb
        return sb


class Deadman:
    """The commander that survives its own outage.

    Parameters
    ----------
    incident_id : str
        Unique identifier for this incident (used as the durable-state key).
    world : World | RealWorld
        System-of-record adapter. Mock World for demo/tests; RealWorld for production.
    chaos : Chaos | None
        Chaos injection handle. Pass None in production / webhook path — every chaos
        access in run() is guarded so that chaos=None never raises AttributeError.
    """

    def __init__(self, incident_id: str, world, chaos=None):
        self.incident_id = config.validate_incident_id(incident_id)
        self.world = world
        self.chaos = chaos
        self.state = DurableState(self.incident_id)
        self.audit = AuditLog(self.incident_id)
        self.ai = AIGateway(chaos)
        self.agentgw = AgentGateway()
        self.mcp = MCPGateway(world, self.audit, chaos)
        self.diag_key = action_key(self.incident_id, "cw.get_metrics", "read")
        self.revert_key = action_key(self.incident_id, "revert_pr", "PR-1337")
        self.cordon_key = action_key(self.incident_id, "cordon_drain", "prod-node-7")
        # Input-side (AI Gateway) guardrail blocks — counted separately from the MCP
        # tool guardrail blocks so the scoreboard tally includes both layers.
        self.input_guardrail_blocks = 0

    def _scope(self) -> set:
        return self.agentgw.allowed_scope(self.ai.max_depth)

    def _reconcile_pending(self, action: str, target: str) -> bool:
        """Per-verb system-of-record check used by the resume path.

        Returns True if the destructive action already landed on the real system, so it
        must NOT be re-run. Generalized across every destructive verb — the original code
        only handled github.revert_pr, which let cordon_drain / asg.scale double-execute
        on resume (the reproduced exactly-once bug).
        """
        if action in ("github.revert_pr", "revert_pr"):
            return bool(self.world.is_reverted(target))
        if action in ("k8s.cordon_drain", "cordon_drain"):
            check = getattr(self.world, "is_cordoned", None)
            return bool(check(target)) if check else False
        if action in ("asg.scale", "asg_scale"):
            check = getattr(self.world, "is_scaled", None)
            return bool(check(target)) if check else False
        return False

    def run(self, resume: bool = False) -> Scoreboard:
        sb = Scoreboard()

        # --- RESUME PATH: rehydrate from durable state + audit log, dedupe in-flight action ---
        if resume:
            sb.notes.append("fresh process: rehydrating from durable state + audit log")
            pending = self.state.pending
            if pending and self.audit.is_committed(pending["key"]):
                sb.notes.append(f"pending {pending['action']} already COMMITTED in audit log -> skip")
            elif pending:
                # PENDING but not COMMITTED: verify the system of record before re-acting.
                # Generalized across ALL destructive verbs (not just revert_pr) so a kill
                # after a cordon_drain / asg.scale side-effect also reconciles exactly-once.
                action = pending["action"]
                target = pending["key"].split("::")[-1]
                if self._reconcile_pending(action, target):
                    self.state.commit(action, pending["key"])
                    self.audit.write({"status": "COMMITTED", "tool": action, "key": pending["key"]})
                    sb.notes.append(
                        f"system-of-record shows {action} on {target} already applied -> reconciled, NOT re-run"
                    )

        # --- diagnose / decide (model calls go through the AI Gateway fallback chain) ---
        # [PULSE] Wrap each LLM step in an OTel span (no-op when OTel unset)
        try:
            with _otel.span("agent.step", step="diagnose", incident_id=self.incident_id):
                self.ai.complete("diagnose the incident from cloudwatch + k8s events")
            with _otel.span("agent.step", step="decide", incident_id=self.incident_id):
                self.ai.complete("decide the mitigation: revert the bad deploy PR-1337")
        except ModelOutage:
            sb.notes.append("all tiers + cold cache down — degraded to safe hold")
        except _guardrails.GatewayGuardrailBlock as e:
            # The TFY AI Gateway tripped an input guardrail (e.g. prompt-injection in
            # hostile log/incident content). Treat it as a handled failure: never reason
            # on injected input — degrade to a safe hold and record the block.
            _metrics.record_guardrail_block("llm.input")
            self.input_guardrail_blocks += 1
            sb.notes.append(f"AI Gateway guardrail blocked hostile input -> safe hold ({e})")

        sb.backend = "tier-%d" % self.ai.max_depth
        sb.fallback_depth = self.ai.max_depth
        scope = self._scope()

        # Track tool attempts for the kill-switch block-rate computation.
        _tool_attempts = 0

        # --- governed diagnostic read; Post-Tool guardrail catches a corrupt/garbage result ---
        _tool_attempts += 1
        try:
            self.mcp.execute("cw.get_metrics", {"_returns": {"cpu": 0.9}}, self.diag_key, scope)
        except GuardrailBlock as e:
            sb.notes.append(str(e))   # caught the bad intermediate output -> would re-fetch

        # --- the destructive action: revert PR-1337, idempotency-keyed, governed, audited ---
        if not self.audit.is_committed(self.revert_key):
            self.state.set_pending("github.revert_pr", self.revert_key)
            _tool_attempts += 1
            try:
                # If authority was revoked, restore it for the *reconciliation* of an in-flight
                # action (we only skip NEW destructive actions; finishing a committed one is safe).
                act_scope = scope | {"github.revert_pr"} if self.world.is_reverted("PR-1337") else scope
                self.mcp.execute("github.revert_pr", {"pr": "PR-1337"}, self.revert_key, act_scope)
                self.state.commit("github.revert_pr", self.revert_key)
            except KillSignal:
                sb.state_losses += 0  # state is durable -> NOT lost
                sb.notes.append("KILLED mid-rollback (between side effect and COMMIT)")
                raise
            except ScopeDenied as e:
                sb.notes.append(str(e))

        # --- Kill-switch: Phase A rate check (after first tool-execution phase) ---
        # Primary control is the TFY gateway block-prompt-injection guardrail; this is
        # defense-in-depth matching infra/guardrails.yaml kill_switch_block_rate_threshold: 0.5.
        _block_rate_a = self.mcp.guardrail_blocks / max(1, _tool_attempts)
        if _tool_attempts >= KILL_SWITCH_MIN_ATTEMPTS and self.agentgw.trip_kill_switch(_block_rate_a):
            sb.notes.append(
                f"kill-switch TRIPPED (phase-A block rate {_block_rate_a:.2f} >= 0.5) "
                "— destructive scope revoked"
            )

        # --- Phase B: the outage DEEPENS -> auto-leash ---
        # In chaos/demo mode: inject tier-1 failure and call the AI gateway for a re-plan.
        # In production (chaos=None): just call the AI gateway — the real fallback depth
        # comes from the AI Gateway's own tier-health probing (no chaos toggle needed).
        if self.chaos is not None:
            self.chaos.down_tiers.add(1)   # both Claude regions gone (demo only)

        # Re-plan on the (now deeper) fallback chain — in real mode this naturally surfaces
        # whatever depth the TFY AI Gateway resolved based on live Bedrock availability.
        try:
            self.ai.complete("re-plan mitigation under a deeper outage")
        except _guardrails.GatewayGuardrailBlock as e:
            _metrics.record_guardrail_block("llm.input")
            self.input_guardrail_blocks += 1
            sb.notes.append(f"AI Gateway guardrail blocked re-plan input -> safe hold ({e})")
        scope2 = self._scope()
        sb.fallback_depth = self.ai.max_depth
        sb.backend = "tier-%d" % self.ai.max_depth
        sb.drain_authority = self.agentgw.drain_authority
        if self.agentgw.revoked:
            sb.notes.append("brain degraded to tier-%d -> Agent Gateway AUTO-LEASH: destructive authority REVOKED" % self.ai.max_depth)
        # Checkpoint the cordon BEFORE executing (only when actually in scope) so a kill
        # mid-cordon is recoverable by the generalized resume reconcile — exactly-once
        # now holds for cordon_drain too, not just revert_pr.
        if "k8s.cordon_drain" in scope2 and not self.audit.is_committed(self.cordon_key):
            self.state.set_pending("k8s.cordon_drain", self.cordon_key)
        _tool_attempts += 1
        try:
            self.mcp.execute("k8s.cordon_drain", {"node": "prod-node-7"}, self.cordon_key, scope2)
        except ScopeDenied as e:
            sb.notes.append("blocked a risky cordon_drain on a degraded brain -> " + str(e))

        # --- Kill-switch: Phase B rate check (after all tool-execution phases) ---
        # Recompute with the full attempt count; the latch means a previously-tripped
        # switch stays revoked even if the rate dropped (harmless for subsequent scope calls).
        _block_rate_b = self.mcp.guardrail_blocks / max(1, _tool_attempts)
        if _tool_attempts >= KILL_SWITCH_MIN_ATTEMPTS and self.agentgw.trip_kill_switch(_block_rate_b):
            if "kill-switch TRIPPED" not in " ".join(sb.notes):
                sb.notes.append(
                    f"kill-switch TRIPPED (phase-B block rate {_block_rate_b:.2f} >= 0.5) "
                    "— destructive scope revoked"
                )

        sb.guardrail_blocks = self.mcp.guardrail_blocks + self.input_guardrail_blocks
        sb.double_executions = max(self.world.count("revert_pr") - 1, 0)
        sb.survived = True
        sb.notes.append("incident resolved; postmortem written from the audit log")
        # [PULSE] Record incident metrics (no-op when prometheus_client absent)
        _metrics.record_incident(mode=config.MODE, outcome="resolved")
        _metrics.record_fallback_depth(sb.fallback_depth)
        if sb.double_executions > 0:
            _metrics.record_double_execution()
        return sb

    # -------------------------------------------------------------------------
    # REAL AGENTIC LOOP (Cortex)
    # -------------------------------------------------------------------------
    def run_agentic(self, summary: str, max_steps: int = 8, resume: bool = False) -> Scoreboard:
        """Reason → act → observe loop where the LLM actually chooses each tool.

        Uses a TEXT-based ReAct pattern over AIGateway.complete(prompt) → Completion.
        The planner parses the model's JSON-action text; exactly-once and auto-leash
        are fully preserved.

        Parameters
        ----------
        summary   : Short incident description (forwarded from the webhook payload).
        max_steps : Budget cap — the loop stops after this many steps even if the
                    model never says done=True.
        resume    : If True, rehydrate from durable state first (same resume logic
                    as run() — the generalized _reconcile_pending handles ALL verbs).
        """
        sb = Scoreboard()
        observations: list[str] = []

        # ── RESUME PATH ───────────────────────────────────────────────────────
        if resume:
            sb.notes.append("agentic-resume: rehydrating from durable state + audit log")
            pending = self.state.pending
            if pending and self.audit.is_committed(pending["key"]):
                note = (
                    f"pending {pending['action']} already COMMITTED in audit log -> skip"
                )
                sb.notes.append(note)
                observations.append(f"[resume] {note}")
            elif pending:
                action_name = pending["action"]
                target = pending["key"].split("::")[-1]
                if self._reconcile_pending(action_name, target):
                    self.state.commit(action_name, pending["key"])
                    self.audit.write({
                        "status": "COMMITTED",
                        "tool": action_name,
                        "key": pending["key"],
                    })
                    note = (
                        f"system-of-record shows {action_name} on {target} already applied "
                        "-> reconciled, NOT re-run"
                    )
                    sb.notes.append(note)
                    observations.append(f"[resume] {note}")

        # ── REASON→ACT→OBSERVE LOOP ───────────────────────────────────────────
        catalog = tools.tool_catalog_prompt()

        # Fix 4: sanitize the raw summary before it reaches the LLM prompt.
        # The TFY gateway block-prompt-injection guardrail is the PRIMARY control;
        # this local redact+cap is defense-in-depth only.
        _safe_summary = _guardrails.redact_text(summary)[:2000]

        # Track total tool attempts across all agentic steps for the kill-switch rate.
        _total_tool_attempts = 0

        for _step in range(max_steps):
            # ── build prompt + call LLM ────────────────────────────────────────
            # Use the sanitized summary in the prompt (defense-in-depth against prompt injection).
            prompt = planner.build_prompt(_safe_summary, observations, catalog)
            # [PULSE] Span per agentic loop step wraps the full step body (LLM call +
            # action handling) — the empty pass block was replaced with the real work.
            with _otel.span("agent.agentic_step", step=str(_step), incident_id=self.incident_id):
                try:
                    comp = self.ai.complete(prompt)
                except ModelOutage:
                    sb.notes.append("all tiers + cold cache down — degraded to safe hold")
                    observations.append("[model-outage] all AI tiers down — stopping loop")
                    break

                # Track fallback depth / backend from the gateway response
                sb.fallback_depth = self.ai.max_depth
                sb.backend = "tier-%d" % self.ai.max_depth

                # ── parse the model's decision ─────────────────────────────────────
                action = planner.parse_action(comp.text)

                # ── DONE ──────────────────────────────────────────────────────────
                if action.done:
                    observations.append(f"[done] {action.rationale}")
                    sb.notes.append(f"model declared incident resolved: {action.rationale}")
                    break

                # ── SAFE-HOLD (unparseable / missing tool) ─────────────────────────
                if action.tool is None:
                    note = f"[safe-hold] {action.rationale}"
                    observations.append(note)
                    sb.notes.append(note)
                    # counts toward budget but does NOT execute any tool
                    continue

                # ── LOOK UP TOOL IN REGISTRY ───────────────────────────────────────
                reg_tool = tools.REGISTRY.get(action.tool)
                if reg_tool is None:
                    note = f"[unknown-tool] '{action.tool}' not in registry — safe-hold"
                    observations.append(note)
                    sb.notes.append(note)
                    continue

                # ── VALIDATE MODEL-SUPPLIED ARGS ───────────────────────────────────
                # LLMs routinely omit required args. Treat a validation failure exactly
                # like a guardrail block: record it, do NOT execute, do NOT crash, and
                # let the model choose again on the next turn.
                try:
                    tools.validate_args(reg_tool, action.args)
                except ValueError as e:
                    note = (
                        f"[invalid-args] {action.tool}: {e} — choose again or pick another tool"
                    )
                    observations.append(note)
                    sb.notes.append(note)
                    continue

                # ── IDEMPOTENCY KEY ───────────────────────────────────────────────
                target = tools.idempotency_target(reg_tool, action.args)
                if reg_tool.destructive:
                    # Destructive tools are exactly-once: key on the chosen action only,
                    # checkpoint BEFORE execute, dedup against the audit log.
                    key = action_key(self.incident_id, action.tool, target)
                    # Fast-path: already committed → tell the model, move on
                    if self.audit.is_committed(key):
                        note = f"[skipped-idempotent] {action.tool}({target}) already committed"
                        observations.append(note)
                        sb.notes.append(note)
                        continue
                else:
                    # Non-destructive tools (diagnostics, statuspage) must NOT be
                    # idempotency-deduped — the model must be able to re-fetch fresh
                    # metrics/logs on every step. Make each call distinct via the loop
                    # step index so it always executes (never SKIPPED_IDEMPOTENT).
                    key = action_key(self.incident_id, action.tool, f"{target}#step{_step}")

                # ── PRE-EXECUTE: checkpoint destructive actions ────────────────────
                # Recompute scope here so a kill-switch trip from a prior step immediately
                # narrows authority for the next destructive attempt.
                scope = self._scope()
                if reg_tool.destructive:
                    self.state.set_pending(action.tool, key)

                # ── EXECUTE via MCP Gateway ────────────────────────────────────────
                _total_tool_attempts += 1
                try:
                    result = self.mcp.execute(action.tool, action.args, key, scope)
                except KillSignal:
                    # Kill semantics are unchanged: re-raise so tests / scripts catch it.
                    sb.notes.append(f"KILLED mid-{action.tool} (between side effect and COMMIT)")
                    raise
                except ScopeDenied as e:
                    note = f"[scope-denied] {action.tool} blocked — {e}"
                    observations.append(note)
                    sb.notes.append(note)
                    sb.guardrail_blocks = self.mcp.guardrail_blocks
                    continue
                except GuardrailBlock as e:
                    note = f"[guardrail-block] {action.tool} blocked — {e}"
                    observations.append(note)
                    sb.notes.append(note)
                    sb.guardrail_blocks = self.mcp.guardrail_blocks
                    # Kill-switch: check rate after every guardrail block so the next
                    # iteration's _scope() call immediately honors the trip.
                    _block_rate = self.mcp.guardrail_blocks / max(1, _total_tool_attempts)
                    if _total_tool_attempts >= KILL_SWITCH_MIN_ATTEMPTS and self.agentgw.trip_kill_switch(_block_rate):
                        sb.notes.append(
                            f"kill-switch TRIPPED (block rate {_block_rate:.2f} >= 0.5) "
                            "— destructive scope revoked for remaining steps"
                        )
                    continue

                # ── POST-EXECUTE: commit + build observation ───────────────────────
                if reg_tool.destructive and result.status == "EXECUTED":
                    self.state.commit(action.tool, key)

                obs_status = result.status  # "EXECUTED" | "SKIPPED_IDEMPOTENT"
                note = (
                    f"[{obs_status.lower()}] {action.tool}({target}) "
                    f"rationale={action.rationale!r}"
                )
                observations.append(note)
                sb.notes.append(note)

        # ── KILL-SWITCH: final rate check across all steps ─────────────────────
        # Catches the case where the rate crossed the threshold late in the loop but
        # no individual guardrail block triggered the per-step check above.
        _final_rate = self.mcp.guardrail_blocks / max(1, _total_tool_attempts)
        if _total_tool_attempts >= KILL_SWITCH_MIN_ATTEMPTS and self.agentgw.trip_kill_switch(_final_rate):
            if not any("kill-switch TRIPPED" in n for n in sb.notes):
                sb.notes.append(
                    f"kill-switch TRIPPED (final block rate {_final_rate:.2f} >= 0.5) "
                    "— destructive scope revoked"
                )

        # ── FINAL SCOREBOARD FIELDS ────────────────────────────────────────────
        sb.guardrail_blocks = self.mcp.guardrail_blocks
        sb.drain_authority = self.agentgw.drain_authority
        # Count double-executions for any destructive verb present in the world
        reverts = getattr(self.world, "count", lambda a: 0)("revert_pr")
        sb.double_executions = max(reverts - 1, 0)
        sb.survived = True
        sb.notes.append(
            "agentic loop finished; postmortem from audit log covers "
            f"{len(observations)} observations over up to {max_steps} steps"
        )
        # [PULSE] Record incident metrics (no-op when prometheus_client absent)
        _metrics.record_incident(mode=config.MODE, outcome="resolved")
        _metrics.record_fallback_depth(sb.fallback_depth)
        if sb.double_executions > 0:
            _metrics.record_double_execution()
        return sb
