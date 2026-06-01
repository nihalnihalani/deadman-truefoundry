"""Tests 8: Agent Gateway auto-leash.

allowed_scope drops destructive verbs once fallback_depth >= AUTONOMY_REVOKE_AT_DEPTH.
drain_authority flips ON->OFF. trip_kill_switch(>=0.5) revokes.
"""
from __future__ import annotations
import pytest

import deadman.config as config
from deadman.agent_gateway import AgentGateway, FULL_SCOPE, READ_ONLY


class TestAgentGateway:

    def test_healthy_brain_full_scope(self):
        """fallback_depth=0 -> FULL_SCOPE returned."""
        gw = AgentGateway()
        scope = gw.allowed_scope(0)
        assert scope == FULL_SCOPE

    def test_shallow_degradation_still_full_scope(self):
        """fallback_depth < AUTONOMY_REVOKE_AT_DEPTH -> FULL_SCOPE."""
        gw = AgentGateway()
        depth = config.AUTONOMY_REVOKE_AT_DEPTH - 1
        scope = gw.allowed_scope(depth)
        assert scope == FULL_SCOPE

    def test_at_revoke_depth_drops_destructive(self):
        """fallback_depth == AUTONOMY_REVOKE_AT_DEPTH -> READ_ONLY (destructive verbs gone)."""
        gw = AgentGateway()
        scope = gw.allowed_scope(config.AUTONOMY_REVOKE_AT_DEPTH)
        assert scope == READ_ONLY
        # Destructive verbs absent
        for tool in config.DESTRUCTIVE_TOOLS:
            assert tool not in scope, f"{tool} still in scope at revoke depth"

    def test_beyond_revoke_depth_read_only(self):
        """fallback_depth > AUTONOMY_REVOKE_AT_DEPTH -> READ_ONLY."""
        gw = AgentGateway()
        scope = gw.allowed_scope(config.AUTONOMY_REVOKE_AT_DEPTH + 2)
        assert scope == READ_ONLY

    def test_drain_authority_starts_on(self):
        """drain_authority is 'ON' before any revocation."""
        gw = AgentGateway()
        assert gw.drain_authority == "ON"

    def test_drain_authority_flips_off_at_revoke_depth(self):
        """drain_authority becomes 'OFF' once revoked."""
        gw = AgentGateway()
        gw.allowed_scope(config.AUTONOMY_REVOKE_AT_DEPTH)
        assert gw.drain_authority == "OFF"

    def test_revoked_flag_set_on_scope_revocation(self):
        """gw.revoked becomes True after scope is revoked."""
        gw = AgentGateway()
        assert gw.revoked is False
        gw.allowed_scope(config.AUTONOMY_REVOKE_AT_DEPTH)
        assert gw.revoked is True

    def test_kill_switch_below_threshold_no_revoke(self):
        """trip_kill_switch(0.4) -> returns False, scope unchanged."""
        gw = AgentGateway()
        tripped = gw.trip_kill_switch(0.4)
        assert tripped is False
        # Scope should still be full
        scope = gw.allowed_scope(0)
        assert scope == FULL_SCOPE

    def test_kill_switch_at_threshold_revokes(self):
        """trip_kill_switch(0.5) -> revokes scope; returns True."""
        gw = AgentGateway()
        tripped = gw.trip_kill_switch(0.5)
        assert tripped is True
        assert gw.revoked is True

    def test_kill_switch_above_threshold_revokes(self):
        """trip_kill_switch(0.9) -> revokes scope; returns True."""
        gw = AgentGateway()
        tripped = gw.trip_kill_switch(0.9)
        assert tripped is True
        scope = gw.allowed_scope(0)   # even depth 0 -> READ_ONLY now
        assert scope == READ_ONLY

    def test_kill_switch_latches(self):
        """Once kill_switch tripped, stays revoked regardless of subsequent calls."""
        gw = AgentGateway()
        gw.trip_kill_switch(1.0)
        # Calling trip_kill_switch below threshold afterward doesn't un-trip it
        result = gw.trip_kill_switch(0.1)
        assert result is True   # still tripped
        assert gw.revoked is True

    def test_read_only_scope_contains_expected_tools(self):
        """READ_ONLY contains the expected safe tools."""
        expected = {"cw.get_metrics", "logs.query", "k8s.describe", "statuspage.post"}
        assert READ_ONLY == expected

    def test_full_scope_contains_all_destructive_tools(self):
        """FULL_SCOPE contains all destructive tools."""
        for tool in config.DESTRUCTIVE_TOOLS:
            assert tool in FULL_SCOPE, f"{tool} missing from FULL_SCOPE"

    def test_revocation_latches_on_lower_depth(self):
        """Revocation LATCHES (monotonic): once destructive scope is revoked by depth,
        a later healthy depth=0 call does NOT silently re-grant FULL_SCOPE. Reconstruct
        the gateway to get a fresh FULL_SCOPE — this closes the leaky re-grant Raven flagged."""
        gw = AgentGateway()
        gw.allowed_scope(config.AUTONOMY_REVOKE_AT_DEPTH)   # revoke at high depth
        # Even with a healthy depth the gate stays closed (latched revocation).
        scope_after = gw.allowed_scope(0)
        assert scope_after == READ_ONLY
        assert gw.revoked is True
        # A fresh gateway re-grants full scope.
        fresh = AgentGateway()
        assert fresh.allowed_scope(0) == FULL_SCOPE

    def test_kill_switch_latch_persists_across_depth_changes(self):
        """Once kill_switch tripped, calling allowed_scope(0) still returns READ_ONLY."""
        gw = AgentGateway()
        gw.trip_kill_switch(1.0)        # arm the latch
        scope_at_zero = gw.allowed_scope(0)
        assert scope_at_zero == READ_ONLY
