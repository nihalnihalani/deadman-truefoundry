"""Mock TrueFoundry Agent Gateway (launched May 27, 2026) — the WOW layer.

Couples AUTHORITY to CONFIDENCE: it subscribes to the AI Gateway's fallback-depth signal
and automatically narrows the agent's allowed action scope as the brain degrades. A dumber
brain gets a shorter leash — governed centrally, not in app code.

In real mode, fallback_depth comes from AIGateway.max_depth which is set by the TFY AI
Gateway response metadata/headers (parsed by Atlas in realmode_ai.py). The AgentGateway
contract is identical regardless of how max_depth was measured.

Kill-switch
-----------
trip_kill_switch(guardrail_block_rate) revokes ALL destructive scope when the rate of
guardrail blocks from the MCP gateway exceeds 0.5 (matching infra/guardrails.yaml).
This is a defense-in-depth layer that kicks in when Cedar blocks are spiking, indicating
the agent is attempting unsafe operations repeatedly. Once tripped the kill-switch latches
until the AgentGateway is reconstructed (per-incident lifetime).
"""
from __future__ import annotations
import deadman.config as config

# Full scope when the brain is healthy.
FULL_SCOPE = {
    "cw.get_metrics", "logs.query", "k8s.describe",
    "k8s.cordon_drain", "asg.scale", "github.revert_pr", "statuspage.post",
}
READ_ONLY = {"cw.get_metrics", "logs.query", "k8s.describe", "statuspage.post"}


class AgentGateway:
    def __init__(self):
        self.revoked = False
        self._kill_switch_tripped = False

    def allowed_scope(self, fallback_depth: int) -> set:
        """Revoke destructive authority once the brain has fallen back too far.

        Also returns READ_ONLY when the kill-switch has been tripped via
        trip_kill_switch(). Revocation LATCHES: once destructive scope is revoked
        (by depth, kill-switch, or a prior call) it stays revoked for this gateway's
        lifetime — a recovering brain cannot silently re-acquire destructive authority.

        Parameters
        ----------
        fallback_depth : int
            Current AI Gateway max_depth. In real mode this is sourced from TFY
            response headers (e.g. X-Fallback-Depth) parsed by realmode_ai.py and
            stored in AIGateway.max_depth. The value is authoritative regardless of
            how it was obtained.
        """
        # Revocation LATCHES (monotonic): once destructive scope is revoked it never
        # silently re-grants on a transient fallback-depth dip. Honor the already-latched
        # self.revoked so a recovering brain cannot re-acquire destructive authority for
        # the lifetime of this gateway instance (reconstruct AgentGateway() to reset).
        if self._kill_switch_tripped or self.revoked or fallback_depth >= config.AUTONOMY_REVOKE_AT_DEPTH:
            self.revoked = True
            return READ_ONLY            # destructive verbs are gone
        return FULL_SCOPE

    def trip_kill_switch(self, guardrail_block_rate: float) -> bool:
        """Revoke ALL destructive scope if the guardrail block rate is >= 0.5.

        Called by the commander (or a monitoring loop) when it observes that Cedar
        pre/post-tool blocks are occurring at a high rate, signalling the agent is
        attempting dangerous operations in a degraded environment.

        Returns True if the kill-switch was tripped (now or previously), False if
        the rate was below the threshold and the switch remains off.

        This matches the threshold in infra/guardrails.yaml:
            kill_switch_block_rate_threshold: 0.5
        """
        if guardrail_block_rate >= 0.5:
            self._kill_switch_tripped = True
            self.revoked = True
        return self._kill_switch_tripped

    @property
    def drain_authority(self) -> str:
        return "OFF" if self.revoked else "ON"
