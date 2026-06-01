"""Mock TrueFoundry Agent Gateway (launched May 27, 2026) — the WOW layer.

Couples AUTHORITY to CONFIDENCE: it subscribes to the AI Gateway's fallback-depth signal
and automatically narrows the agent's allowed action scope as the brain degrades. A dumber
brain gets a shorter leash — governed centrally, not in app code.
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

    def allowed_scope(self, fallback_depth: int) -> set:
        """Revoke destructive authority once the brain has fallen back too far."""
        if fallback_depth >= config.AUTONOMY_REVOKE_AT_DEPTH:
            self.revoked = True
            return READ_ONLY            # destructive verbs are gone
        return FULL_SCOPE

    @property
    def drain_authority(self) -> str:
        return "OFF" if self.revoked else "ON"
