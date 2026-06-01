"""Tests 1 (crown jewel): exactly-once execution across process-kill.

Mirrors scripts/prove_exactly_once.py but runs inside pytest with isolated state.
"""
from __future__ import annotations
import pytest

import deadman.state as state_module
from deadman.world import World
from deadman.chaos import Chaos
from deadman.commander import Deadman, REVERT_KEY
from deadman.mcp_gateway import KillSignal


def _run_kill_then_resume(incident_id: str, world: World):
    """Run Deadman until kill, then resume fresh. Returns (first_agent, resumed_scoreboard)."""
    state_module.reset(incident_id)
    chaos = Chaos()
    chaos.kill_process_after(REVERT_KEY)

    # First run: dies mid-rollback
    agent1 = Deadman(incident_id, world, chaos)
    with pytest.raises(KillSignal):
        agent1.run()

    # Fresh process: new Deadman instance rehydrates from durable state + audit log
    chaos.kill_after = None
    agent2 = Deadman(incident_id, world, chaos)
    sb = agent2.run(resume=True)
    return agent1, sb


class TestExactlyOnce:
    """Scenario test: kill mid-rollback, resume, assert world.count('revert_pr') == 1."""

    def test_exactly_once_scenario(self, isolated_state):
        world = World()
        incident_id = "test-exactly-once"
        _, sb = _run_kill_then_resume(incident_id, world)

        total = world.count("revert_pr")
        assert total == 1, (
            f"EXACTLY-ONCE VIOLATED: revert_pr ran {total} time(s). "
            f"Scoreboard notes: {sb.notes}"
        )
        assert sb.survived is True

    def test_kill_fires_exactly_once_before_resume(self, isolated_state):
        """After kill, exactly one side effect has already happened in the world."""
        world = World()
        incident_id = "test-kill-count"
        state_module.reset(incident_id)
        chaos = Chaos()
        chaos.kill_process_after(REVERT_KEY)

        agent1 = Deadman(incident_id, world, chaos)
        with pytest.raises(KillSignal):
            agent1.run()

        # The side effect happened once before the kill
        assert world.count("revert_pr") == 1

    def test_property_resume_3x_never_double_executes(self, isolated_state):
        """Property test: resuming N times after a kill never produces a second revert."""
        world = World()
        incident_id = "test-property-resume"
        state_module.reset(incident_id)
        chaos = Chaos()
        chaos.kill_process_after(REVERT_KEY)

        # Initial kill run
        agent1 = Deadman(incident_id, world, chaos)
        with pytest.raises(KillSignal):
            agent1.run()

        chaos.kill_after = None

        # Resume 3 times — exactly-once must hold every time
        for i in range(3):
            agent_resume = Deadman(incident_id, world, chaos)
            sb = agent_resume.run(resume=True)
            count = world.count("revert_pr")
            assert count == 1, (
                f"EXACTLY-ONCE VIOLATED on resume #{i + 1}: "
                f"revert_pr ran {count} times. Notes: {sb.notes}"
            )

    def test_audit_log_shows_exactly_one_committed(self, isolated_state):
        """After kill+resume, the audit log has exactly one COMMITTED record for REVERT_KEY."""
        from deadman.state import AuditLog
        world = World()
        incident_id = "test-audit-committed"
        _run_kill_then_resume(incident_id, world)

        audit = AuditLog(incident_id)
        committed_count = sum(
            1 for line in audit.postmortem()
            if "COMMITTED" in line and "revert_pr" in line
        )
        assert committed_count == 1, f"Expected 1 COMMITTED revert_pr, got {committed_count}"

    def test_no_kill_runs_normally_once(self, isolated_state):
        """Without chaos kill the revert fires exactly once in a normal run."""
        world = World()
        incident_id = "test-normal-run"
        state_module.reset(incident_id)
        chaos = Chaos()  # no kill_after

        agent = Deadman(incident_id, world, chaos)
        sb = agent.run()

        assert world.count("revert_pr") == 1
        assert sb.survived is True
