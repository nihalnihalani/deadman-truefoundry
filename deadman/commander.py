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
"""
from __future__ import annotations
from dataclasses import dataclass, field

import deadman.config as config
from deadman.ai_gateway import AIGateway, ModelOutage
from deadman.mcp_gateway import MCPGateway, KillSignal, GuardrailBlock, ScopeDenied
DIAG_KEY = "incident-42::cw.get_metrics::read"
CORDON_KEY = "incident-42::cordon_drain::prod-node-7"
from deadman.agent_gateway import AgentGateway
from deadman.state import DurableState, AuditLog


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


# The incident plan: the destructive step (revert_pr) is the one the kill targets.
REVERT_KEY = "incident-42::revert_pr::PR-1337"


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
        self.incident_id = incident_id
        self.world = world
        self.chaos = chaos
        self.state = DurableState(incident_id)
        self.audit = AuditLog(incident_id)
        self.ai = AIGateway(chaos)
        self.agentgw = AgentGateway()
        self.mcp = MCPGateway(world, self.audit, chaos)

    def _scope(self) -> set:
        return self.agentgw.allowed_scope(self.ai.max_depth)

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
                if self.world.is_reverted("PR-1337"):
                    self.state.commit(pending["action"], pending["key"])
                    self.audit.write({"status": "COMMITTED", "tool": "github.revert_pr", "key": pending["key"]})
                    sb.notes.append("system-of-record shows PR already reverted -> reconciled, NOT re-run")

        # --- diagnose / decide (model calls go through the AI Gateway fallback chain) ---
        try:
            self.ai.complete("diagnose the incident from cloudwatch + k8s events")
            self.ai.complete("decide the mitigation: revert the bad deploy PR-1337")
        except ModelOutage:
            sb.notes.append("all tiers + cold cache down — degraded to safe hold")

        sb.backend = "tier-%d" % self.ai.max_depth
        sb.fallback_depth = self.ai.max_depth
        scope = self._scope()

        # --- governed diagnostic read; Post-Tool guardrail catches a corrupt/garbage result ---
        try:
            self.mcp.execute("cw.get_metrics", {"_returns": {"cpu": 0.9}}, DIAG_KEY, scope)
        except GuardrailBlock as e:
            sb.notes.append(str(e))   # caught the bad intermediate output -> would re-fetch

        # --- the destructive action: revert PR-1337, idempotency-keyed, governed, audited ---
        if not self.audit.is_committed(REVERT_KEY):
            self.state.set_pending("github.revert_pr", REVERT_KEY)
            try:
                # If authority was revoked, restore it for the *reconciliation* of an in-flight
                # action (we only skip NEW destructive actions; finishing a committed one is safe).
                act_scope = scope | {"github.revert_pr"} if self.world.is_reverted("PR-1337") else scope
                self.mcp.execute("github.revert_pr", {"pr": "PR-1337"}, REVERT_KEY, act_scope)
                self.state.commit("github.revert_pr", REVERT_KEY)
            except KillSignal:
                sb.state_losses += 0  # state is durable -> NOT lost
                sb.notes.append("KILLED mid-rollback (between side effect and COMMIT)")
                raise
            except ScopeDenied as e:
                sb.notes.append(str(e))

        # --- Phase B: the outage DEEPENS -> auto-leash ---
        # In chaos/demo mode: inject tier-1 failure and call the AI gateway for a re-plan.
        # In production (chaos=None): just call the AI gateway — the real fallback depth
        # comes from the AI Gateway's own tier-health probing (no chaos toggle needed).
        if self.chaos is not None:
            self.chaos.down_tiers.add(1)   # both Claude regions gone (demo only)

        # Re-plan on the (now deeper) fallback chain — in real mode this naturally surfaces
        # whatever depth the TFY AI Gateway resolved based on live Bedrock availability.
        self.ai.complete("re-plan mitigation under a deeper outage")
        scope2 = self._scope()
        sb.fallback_depth = self.ai.max_depth
        sb.backend = "tier-%d" % self.ai.max_depth
        sb.drain_authority = self.agentgw.drain_authority
        if self.agentgw.revoked:
            sb.notes.append("brain degraded to tier-%d -> Agent Gateway AUTO-LEASH: destructive authority REVOKED" % self.ai.max_depth)
        try:
            self.mcp.execute("k8s.cordon_drain", {"node": "prod-node-7"}, CORDON_KEY, scope2)
        except ScopeDenied as e:
            sb.notes.append("blocked a risky cordon_drain on a degraded brain -> " + str(e))

        sb.guardrail_blocks = self.mcp.guardrail_blocks
        sb.double_executions = max(self.world.count("revert_pr") - 1, 0)
        sb.survived = True
        sb.notes.append("incident resolved; postmortem written from the audit log")
        return sb
