"""Tests 2: Resume reconciliation.

PENDING-not-COMMITTED action where the system-of-record shows the side effect
already happened -> reconciled, NOT re-run.
"""
from __future__ import annotations
import pytest

import deadman.state as state_module
from deadman.state import DurableState, AuditLog
from deadman.world import World
from deadman.chaos import Chaos
from deadman.commander import Deadman, action_key
from deadman.mcp_gateway import KillSignal


class TestResumeReconciliation:
    """PENDING-not-COMMITTED: world shows effect done -> reconcile, no re-run."""

    def test_pending_not_committed_reconciled(self, isolated_state):
        """If state has pending action and world already reverted, resume commits without re-run."""
        incident_id = "test-reconcile"
        state_module.reset(incident_id)

        # Manually plant a PENDING record in durable state (simulates crash after
        # side-effect but before COMMIT write — the window that needs reconciliation).
        revert_key = action_key(incident_id, "revert_pr", "PR-1337")
        ds = DurableState(incident_id)
        ds.set_pending("github.revert_pr", revert_key)

        # The world shows the effect already happened (it did — before the crash).
        world = World()
        world.revert_pr("PR-1337", revert_key)  # side effect already in system-of-record

        chaos = Chaos()  # no kill
        agent = Deadman(incident_id, world, chaos)
        sb = agent.run(resume=True)

        # Reconciliation note must appear
        assert any("reconciled" in n.lower() or "system-of-record" in n.lower() for n in sb.notes), (
            f"Expected reconciliation note, got: {sb.notes}"
        )
        # PR was NOT re-run (world already had 1 entry; after reconcile still 1)
        assert world.count("revert_pr") == 1

    def test_pending_committed_skipped_on_resume(self, isolated_state):
        """If audit log already has COMMITTED for the key, resume skips re-execution."""
        incident_id = "test-reconcile-committed"
        state_module.reset(incident_id)

        # Plant pending state + committed audit entry
        revert_key = action_key(incident_id, "revert_pr", "PR-1337")
        ds = DurableState(incident_id)
        ds.set_pending("github.revert_pr", revert_key)
        audit = AuditLog(incident_id)
        audit.write({"status": "COMMITTED", "tool": "github.revert_pr", "key": revert_key})

        world = World()
        # world does NOT have the revert — simulates a different restart scenario
        chaos = Chaos()
        agent = Deadman(incident_id, world, chaos)
        sb = agent.run(resume=True)

        # The run should see COMMITTED in audit and skip — world still shows 0 re-executions
        # from the resume path (the COMMITTED skip note should appear)
        skip_notes = [n for n in sb.notes if "committed" in n.lower() or "skip" in n.lower()]
        assert skip_notes, f"Expected a skip/committed note on resume, got: {sb.notes}"

    def test_fresh_start_no_pending_runs_normally(self, isolated_state):
        """Without a pending action, resume=True is equivalent to a normal run."""
        incident_id = "test-fresh-resume"
        state_module.reset(incident_id)
        world = World()
        chaos = Chaos()
        agent = Deadman(incident_id, world, chaos)
        sb = agent.run(resume=True)

        # Should still complete and record exactly one revert
        assert sb.survived is True
        assert world.count("revert_pr") == 1
