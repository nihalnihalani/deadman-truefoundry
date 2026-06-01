"""Tests 4: Cedar default-deny / scope enforcement.

A destructive tool not in allowed_scope raises ScopeDenied and writes a DENIED audit entry.
"""
from __future__ import annotations
import pytest

import deadman.state as state_module
from deadman.state import AuditLog
from deadman.world import World
from deadman.chaos import Chaos
from deadman.mcp_gateway import MCPGateway, ScopeDenied
import deadman.config as config


class TestCedarScope:

    def _make_gateway(self, incident_id: str, world=None):
        if world is None:
            world = World()
        audit = AuditLog(incident_id)
        chaos = Chaos()
        return MCPGateway(world, audit, chaos), audit

    def test_destructive_tool_outside_scope_raises_scope_denied(self, isolated_state):
        """github.revert_pr not in allowed_scope -> ScopeDenied."""
        incident_id = "test-cedar-deny"
        state_module.reset(incident_id)
        gw, audit = self._make_gateway(incident_id)

        read_only = {"cw.get_metrics", "logs.query", "k8s.describe", "statuspage.post"}
        with pytest.raises(ScopeDenied):
            gw.execute("github.revert_pr", {"pr": "PR-1337"}, "key-1", read_only)

    def test_denied_audit_entry_written(self, isolated_state):
        """ScopeDenied writes a DENIED entry to the audit log."""
        incident_id = "test-cedar-denied-audit"
        state_module.reset(incident_id)
        gw, audit = self._make_gateway(incident_id)

        read_only = {"cw.get_metrics", "logs.query", "k8s.describe", "statuspage.post"}
        with pytest.raises(ScopeDenied):
            gw.execute("github.revert_pr", {"pr": "PR-1337"}, "key-denied", read_only)

        entries = audit._entries()
        denied = [e for e in entries if e.get("status") == "DENIED"]
        assert len(denied) == 1, f"Expected 1 DENIED entry, got: {denied}"
        assert denied[0]["tool"] == "github.revert_pr"
        assert denied[0]["key"] == "key-denied"

    def test_scope_denied_for_asg_scale(self, isolated_state):
        """asg.scale denied when not in allowed_scope."""
        incident_id = "test-cedar-asg"
        state_module.reset(incident_id)
        gw, _ = self._make_gateway(incident_id)

        with pytest.raises(ScopeDenied):
            gw.execute("asg.scale", {"asg": "my-asg", "replicas": 3}, "key-asg",
                       allowed_scope={"cw.get_metrics"})

    def test_scope_denied_for_cordon_drain(self, isolated_state):
        """k8s.cordon_drain denied when not in allowed_scope."""
        incident_id = "test-cedar-cordon"
        state_module.reset(incident_id)
        gw, _ = self._make_gateway(incident_id)

        with pytest.raises(ScopeDenied):
            gw.execute("k8s.cordon_drain", {"node": "n1", "namespace": "default"}, "key-cordon",
                       allowed_scope={"cw.get_metrics", "logs.query"})

    def test_non_destructive_tool_allowed_without_scope(self, isolated_state):
        """cw.get_metrics is not in DESTRUCTIVE_TOOLS so it passes scope even with empty scope."""
        incident_id = "test-cedar-pass"
        state_module.reset(incident_id)
        world = World()
        gw, _ = self._make_gateway(incident_id, world)

        # cw.get_metrics not in DESTRUCTIVE_TOOLS -> no scope check applies
        result = gw.execute("cw.get_metrics", {"_returns": {"cpu": 0.5}}, "key-read",
                            allowed_scope=set())
        assert result.status == "EXECUTED"

    def test_all_destructive_tools_in_config(self, isolated_state):
        """Verify that all three destructive tools trigger ScopeDenied when missing from scope."""
        incident_id = "test-cedar-all-destructive"
        state_module.reset(incident_id)
        world = World()
        audit = AuditLog(incident_id)
        gw = MCPGateway(world, audit, Chaos())

        empty_scope: set = set()
        for tool in config.DESTRUCTIVE_TOOLS:
            if tool == "github.revert_pr":
                args = {"pr": "PR-0"}
            elif tool == "k8s.cordon_drain":
                args = {"node": "n0", "namespace": "default"}
            else:  # asg.scale
                args = {"asg": "asg-0", "replicas": 3}
            with pytest.raises(ScopeDenied, match=tool):
                gw.execute(tool, args, f"key-{tool}", empty_scope)
