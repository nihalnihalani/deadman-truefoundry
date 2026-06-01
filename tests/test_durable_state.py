"""Tests 9: Durable state FileBackend round-trips + AuditLog behavior.

set_pending/commit/note round-trip survives reconstruction (new DurableState reads
prior data). AuditLog is_committed/pending_keys/postmortem correct.
"""
from __future__ import annotations
import pytest

import deadman.config as config
import deadman.state as state_module
from deadman.state import DurableState, AuditLog, FileBackend


class TestDurableStateFileBackend:

    def test_set_pending_persists(self, isolated_state):
        """set_pending is visible from a freshly constructed DurableState."""
        incident_id = "test-state-pending"
        ds = DurableState(incident_id)
        ds.set_pending("github.revert_pr", "key-abc")

        # Reconstruct from disk
        ds2 = DurableState(incident_id)
        assert ds2.pending is not None
        assert ds2.pending["action"] == "github.revert_pr"
        assert ds2.pending["key"] == "key-abc"

    def test_commit_clears_pending(self, isolated_state):
        """commit() clears pending_action and adds to actions_committed."""
        incident_id = "test-state-commit"
        ds = DurableState(incident_id)
        ds.set_pending("github.revert_pr", "key-commit")
        ds.commit("github.revert_pr", "key-commit")

        ds2 = DurableState(incident_id)
        assert ds2.pending is None
        assert len(ds2.data["actions_committed"]) == 1
        committed = ds2.data["actions_committed"][0]
        assert committed["action"] == "github.revert_pr"
        assert committed["key"] == "key-commit"

    def test_note_appends_to_timeline(self, isolated_state):
        """note() appends to the timeline and is visible on reconstruction."""
        incident_id = "test-state-note"
        ds = DurableState(incident_id)
        ds.note("step 1")
        ds.note("step 2")

        ds2 = DurableState(incident_id)
        assert "step 1" in ds2.data["timeline"]
        assert "step 2" in ds2.data["timeline"]

    def test_pending_is_none_by_default(self, isolated_state):
        """Fresh DurableState has no pending action."""
        incident_id = "test-state-fresh"
        ds = DurableState(incident_id)
        assert ds.pending is None

    def test_set_pending_then_commit_then_new_pending(self, isolated_state):
        """Multiple set_pending/commit cycles work correctly."""
        incident_id = "test-state-multi"
        ds = DurableState(incident_id)

        ds.set_pending("action-a", "key-a")
        ds.commit("action-a", "key-a")
        ds.set_pending("action-b", "key-b")

        ds2 = DurableState(incident_id)
        assert ds2.pending["key"] == "key-b"
        assert len(ds2.data["actions_committed"]) == 1

    def test_path_property_returns_file_path(self, isolated_state):
        """DurableState.path returns a string path (file backend)."""
        incident_id = "test-state-path"
        ds = DurableState(incident_id)
        assert isinstance(ds.path, str)
        assert incident_id in ds.path

    def test_reset_clears_state_and_audit(self, isolated_state):
        """state_module.reset() removes both state and audit files."""
        import os
        incident_id = "test-state-reset"
        ds = DurableState(incident_id)
        ds.set_pending("act", "key-x")
        audit = AuditLog(incident_id)
        audit.write({"status": "PENDING", "tool": "act", "key": "key-x"})

        state_module.reset(incident_id)

        ds2 = DurableState(incident_id)
        assert ds2.pending is None
        audit2 = AuditLog(incident_id)
        assert audit2._entries() == []


class TestAuditLog:

    def test_is_committed_false_initially(self, isolated_state):
        """is_committed returns False when no audit entries exist."""
        incident_id = "test-audit-fresh"
        audit = AuditLog(incident_id)
        assert audit.is_committed("key-missing") is False

    def test_write_and_is_committed_pending(self, isolated_state):
        """PENDING entry does not satisfy is_committed."""
        incident_id = "test-audit-pending"
        audit = AuditLog(incident_id)
        audit.write({"status": "PENDING", "tool": "act", "key": "key-p"})
        assert audit.is_committed("key-p") is False

    def test_write_committed_and_is_committed(self, isolated_state):
        """COMMITTED entry satisfies is_committed."""
        incident_id = "test-audit-committed"
        audit = AuditLog(incident_id)
        audit.write({"status": "COMMITTED", "tool": "github.revert_pr", "key": "key-c"})
        assert audit.is_committed("key-c") is True

    def test_is_committed_key_specific(self, isolated_state):
        """is_committed is key-specific; a different key is not committed."""
        incident_id = "test-audit-key-specific"
        audit = AuditLog(incident_id)
        audit.write({"status": "COMMITTED", "tool": "act", "key": "key-1"})
        assert audit.is_committed("key-2") is False

    def test_pending_keys_returns_uncommitted(self, isolated_state):
        """pending_keys returns keys that are PENDING but not COMMITTED."""
        incident_id = "test-audit-pending-keys"
        audit = AuditLog(incident_id)
        audit.write({"status": "PENDING", "tool": "act", "key": "key-p1"})
        audit.write({"status": "PENDING", "tool": "act", "key": "key-p2"})
        audit.write({"status": "COMMITTED", "tool": "act", "key": "key-p1"})

        pending = audit.pending_keys()
        assert "key-p1" not in pending
        assert "key-p2" in pending

    def test_pending_keys_empty_when_all_committed(self, isolated_state):
        """pending_keys is empty when every PENDING key also has a COMMITTED entry."""
        incident_id = "test-audit-no-pending"
        audit = AuditLog(incident_id)
        audit.write({"status": "PENDING", "tool": "act", "key": "k"})
        audit.write({"status": "COMMITTED", "tool": "act", "key": "k"})
        assert audit.pending_keys() == []

    def test_postmortem_format(self, isolated_state):
        """postmortem() returns lines with status, tool, key."""
        incident_id = "test-audit-postmortem"
        audit = AuditLog(incident_id)
        audit.write({"status": "PENDING", "tool": "github.revert_pr", "key": "k1"})
        audit.write({"status": "COMMITTED", "tool": "github.revert_pr", "key": "k1"})

        pm = audit.postmortem()
        assert len(pm) == 2
        assert any("PENDING" in line for line in pm)
        assert any("COMMITTED" in line for line in pm)
        assert any("github.revert_pr" in line for line in pm)
        assert any("k1" in line for line in pm)

    def test_audit_log_path_property(self, isolated_state):
        """AuditLog.path returns a string path (file backend)."""
        incident_id = "test-audit-path"
        audit = AuditLog(incident_id)
        assert isinstance(audit.path, str)
        assert incident_id in audit.path

    def test_audit_survives_reconstruction(self, isolated_state):
        """Entries written to AuditLog are visible from a freshly constructed instance."""
        incident_id = "test-audit-persist"
        audit1 = AuditLog(incident_id)
        audit1.write({"status": "COMMITTED", "tool": "act", "key": "k"})

        audit2 = AuditLog(incident_id)
        assert audit2.is_committed("k") is True
        assert len(audit2._entries()) == 1

    def test_multiple_denied_entries(self, isolated_state):
        """Multiple DENIED entries accumulate in the audit log."""
        incident_id = "test-audit-denied-multi"
        audit = AuditLog(incident_id)
        audit.write({"status": "DENIED", "tool": "github.revert_pr", "key": "k1"})
        audit.write({"status": "DENIED", "tool": "asg.scale", "key": "k2"})

        entries = audit._entries()
        denied = [e for e in entries if e.get("status") == "DENIED"]
        assert len(denied) == 2
