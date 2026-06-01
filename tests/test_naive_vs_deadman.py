"""Tests 3: Naive vs Deadman contrast.

Naive double-executes under correlated blackout + kill_bedrock.
Deadman stays exactly-once across the same conditions.
"""
from __future__ import annotations
import pytest

import deadman.state as state_module
from deadman.world import World
from deadman.chaos import Chaos
from deadman.commander import NaiveAgent, Deadman, REVERT_KEY
from deadman.mcp_gateway import KillSignal


class TestNaiveVsDeadman:

    def test_naive_double_executes_under_blackout(self, isolated_state):
        """NaiveAgent double-executes when correlated_blackout + kill_bedrock."""
        chaos = Chaos()
        chaos.correlated_blackout()
        chaos.kill_bedrock()
        world = World()

        sb = NaiveAgent(world).run(chaos)

        assert sb.double_executions >= 1, (
            f"Naive should double-execute. double_executions={sb.double_executions}"
        )
        assert world.count("revert_pr") == 2, (
            f"Naive world should show 2 revert_pr calls, got {world.count('revert_pr')}"
        )

    def test_naive_scoreboard_records_state_loss(self, isolated_state):
        """NaiveAgent scoreboard records state_losses > 0 on blackout."""
        chaos = Chaos()
        chaos.correlated_blackout()
        chaos.kill_bedrock()
        world = World()

        sb = NaiveAgent(world).run(chaos)
        assert sb.state_losses >= 1

    def test_deadman_exactly_once_under_same_chaos(self, isolated_state):
        """Deadman under the same correlated_blackout stays exactly-once (0 double executions)."""
        incident_id = "test-naive-vs-deadman"
        state_module.reset(incident_id)

        world = World()
        chaos = Chaos()
        chaos.correlated_blackout()
        chaos.kill_process_after(REVERT_KEY)

        agent = Deadman(incident_id, world, chaos)
        try:
            agent.run()
        except KillSignal:
            pass

        chaos.kill_after = None
        resumed = Deadman(incident_id, world, chaos)
        sb = resumed.run(resume=True)

        assert world.count("revert_pr") == 1
        assert sb.double_executions == 0

    def test_naive_vs_deadman_contrast_demo(self, isolated_state):
        """Full contrast: naive >= 1 double executions; deadman == 0."""
        # ---- Naive ----
        naive_chaos = Chaos()
        naive_chaos.correlated_blackout()
        naive_chaos.kill_bedrock()
        naive_world = World()
        naive_sb = NaiveAgent(naive_world).run(naive_chaos)

        # ---- Deadman ----
        incident_id = "demo-contrast"
        state_module.reset(incident_id)
        dm_world = World()
        dm_chaos = Chaos()
        dm_chaos.correlated_blackout()
        dm_chaos.kill_process_after(REVERT_KEY)
        dm_agent = Deadman(incident_id, dm_world, dm_chaos)
        try:
            dm_agent.run()
        except KillSignal:
            pass
        dm_chaos.kill_after = None
        dm_sb = Deadman(incident_id, dm_world, dm_chaos).run(resume=True)

        assert naive_sb.double_executions >= 1, "Naive must double-execute in the demo"
        assert dm_sb.double_executions == 0, "Deadman must never double-execute"
