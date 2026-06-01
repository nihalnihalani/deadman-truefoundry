"""Tests: incident lifecycle states, transitions, TTL helpers, and state integration.

Coverage:
- Valid transitions pass validate_transition without error.
- Invalid transitions raise ValueError.
- Terminal states (CLOSED, FAILED) cannot transition further.
- ttl_epoch() returns a future Unix epoch.
- FileBackend.sweep() deletes old files and keeps fresh ones.
- DurableState.set_phase() persists the phase and validates transitions.
"""
from __future__ import annotations

import os
import time

import pytest

import deadman.config as config
import deadman.lifecycle as lc
from deadman.lifecycle import (
    ALL_STATES,
    ALLOWED_TRANSITIONS,
    CLOSED,
    FAILED,
    MITIGATING,
    RESOLVED,
    TERMINAL_STATES,
    TRIAGE,
    ttl_epoch,
    validate_transition,
)
from deadman.state import DurableState, FileBackend


# ---------------------------------------------------------------------------
# validate_transition
# ---------------------------------------------------------------------------


class TestValidateTransition:

    def test_triage_to_mitigating(self):
        validate_transition(TRIAGE, MITIGATING)  # must not raise

    def test_mitigating_to_resolved(self):
        validate_transition(MITIGATING, RESOLVED)

    def test_resolved_to_closed(self):
        validate_transition(RESOLVED, CLOSED)

    def test_any_to_failed(self):
        for state in (TRIAGE, MITIGATING, RESOLVED):
            validate_transition(state, FAILED)  # all non-terminal -> FAILED is allowed

    def test_mitigating_regression_to_triage(self):
        """Re-escalation (mitigating -> triage) is an allowed regression."""
        validate_transition(MITIGATING, TRIAGE)

    def test_resolved_can_reopen_to_mitigating(self):
        """Re-open after apparent resolution."""
        validate_transition(RESOLVED, MITIGATING)

    def test_triage_to_resolved_is_invalid(self):
        """Cannot skip mitigating."""
        with pytest.raises(ValueError, match="triage"):
            validate_transition(TRIAGE, RESOLVED)

    def test_triage_to_closed_is_invalid(self):
        with pytest.raises(ValueError):
            validate_transition(TRIAGE, CLOSED)

    def test_closed_is_terminal(self):
        for state in ALL_STATES:
            if state == CLOSED:
                continue
            with pytest.raises(ValueError, match="terminal|Allowed|Invalid"):
                validate_transition(CLOSED, state)

    def test_failed_is_terminal(self):
        for state in ALL_STATES:
            if state == FAILED:
                continue
            with pytest.raises(ValueError):
                validate_transition(FAILED, state)

    def test_unknown_old_state_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            validate_transition("nonexistent", TRIAGE)

    def test_unknown_new_state_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            validate_transition(TRIAGE, "nonexistent")

    def test_all_valid_transitions_in_map_are_accepted(self):
        """Every (old, new) pair listed in ALLOWED_TRANSITIONS passes."""
        for old, allowed in ALLOWED_TRANSITIONS.items():
            for new in allowed:
                validate_transition(old, new)  # must not raise

    def test_terminal_states_have_empty_allowed_set(self):
        for ts in TERMINAL_STATES:
            assert ALLOWED_TRANSITIONS[ts] == frozenset(), (
                f"Terminal state {ts!r} should have no allowed transitions"
            )


# ---------------------------------------------------------------------------
# ttl_epoch
# ---------------------------------------------------------------------------


class TestTtlEpoch:

    def test_returns_future_epoch(self):
        now = int(time.time())
        result = ttl_epoch(3600)
        assert result > now

    def test_default_is_30_days(self):
        now = int(time.time())
        result = ttl_epoch()
        thirty_days = 30 * 24 * 3600
        assert result >= now + thirty_days - 5  # small tolerance
        assert result <= now + thirty_days + 5

    def test_custom_retention(self):
        now = int(time.time())
        result = ttl_epoch(60)
        assert now + 55 <= result <= now + 65

    def test_returns_int(self):
        assert isinstance(ttl_epoch(100), int)

    def test_zero_seconds_returns_now_ish(self):
        now = int(time.time())
        result = ttl_epoch(0)
        assert now - 1 <= result <= now + 1

    def test_negative_retention_raises(self):
        with pytest.raises(ValueError):
            ttl_epoch(-1)


# ---------------------------------------------------------------------------
# FileBackend.sweep
# ---------------------------------------------------------------------------


class TestFileBackendSweep:

    def test_sweep_deletes_old_state_file(self, isolated_state, tmp_path):
        """sweep() removes a .state.json file older than the cutoff."""
        state_dir = isolated_state

        old_file = os.path.join(state_dir, "incident-old.state.json")
        with open(old_file, "w") as f:
            f.write("{}")
        # Age the file by setting mtime to 200 seconds in the past.
        past = time.time() - 200
        os.utime(old_file, (past, past))

        deleted = FileBackend.sweep(older_than_seconds=100, state_dir=state_dir)
        assert old_file in deleted
        assert not os.path.exists(old_file)

    def test_sweep_keeps_fresh_files(self, isolated_state):
        """sweep() leaves recently-modified files untouched."""
        state_dir = isolated_state

        fresh_file = os.path.join(state_dir, "incident-fresh.state.json")
        with open(fresh_file, "w") as f:
            f.write("{}")
        # mtime is now (just written) — well within the cutoff.

        deleted = FileBackend.sweep(older_than_seconds=3600, state_dir=state_dir)
        assert fresh_file not in deleted
        assert os.path.exists(fresh_file)

    def test_sweep_handles_all_suffixes(self, isolated_state):
        """sweep() removes .audit.jsonl and .lock files too."""
        state_dir = isolated_state
        past = time.time() - 500

        for suffix in (".state.json", ".audit.jsonl", ".lock"):
            p = os.path.join(state_dir, f"inc-x{suffix}")
            with open(p, "w") as f:
                f.write("")
            os.utime(p, (past, past))

        deleted = FileBackend.sweep(older_than_seconds=300, state_dir=state_dir)
        assert len(deleted) == 3

    def test_sweep_ignores_unrelated_files(self, isolated_state):
        """sweep() only touches state/audit/lock files, not arbitrary content."""
        state_dir = isolated_state
        past = time.time() - 1000

        unrelated = os.path.join(state_dir, "readme.txt")
        with open(unrelated, "w") as f:
            f.write("not a state file")
        os.utime(unrelated, (past, past))

        deleted = FileBackend.sweep(older_than_seconds=100, state_dir=state_dir)
        assert unrelated not in deleted
        assert os.path.exists(unrelated)

    def test_sweep_missing_directory_returns_empty(self, tmp_path):
        """sweep() on a non-existent directory returns [] without error."""
        nonexistent = str(tmp_path / "does_not_exist")
        deleted = FileBackend.sweep(older_than_seconds=60, state_dir=nonexistent)
        assert deleted == []

    def test_sweep_does_not_affect_running_tests(self, isolated_state):
        """Calling sweep with a very long cutoff never deletes fresh state."""
        ds = DurableState("sweep-test-incident")
        ds.set_pending("act", "k")
        # Sweep with a 1-day cutoff — the file was just written so it stays.
        deleted = FileBackend.sweep(older_than_seconds=86400, state_dir=isolated_state)
        assert not any("sweep-test-incident" in p for p in deleted)
        # The state is still readable.
        ds2 = DurableState("sweep-test-incident")
        assert ds2.pending is not None


# ---------------------------------------------------------------------------
# DurableState.set_phase
# ---------------------------------------------------------------------------


class TestDurableStateSetPhase:

    def test_set_phase_persists(self, isolated_state):
        """set_phase() writes the new phase to durable state and it survives rehydration."""
        ds = DurableState("phase-test-1")
        ds.set_phase(MITIGATING)

        ds2 = DurableState("phase-test-1")
        assert ds2.data["phase"] == MITIGATING

    def test_set_phase_full_valid_chain(self, isolated_state):
        """Walk through the happy path: triage -> mitigating -> resolved -> closed."""
        ds = DurableState("phase-chain-1")
        assert ds.data.get("phase", TRIAGE) == TRIAGE  # fresh default

        ds.set_phase(MITIGATING)
        ds.set_phase(RESOLVED)
        ds.set_phase(CLOSED)

        ds2 = DurableState("phase-chain-1")
        assert ds2.data["phase"] == CLOSED

    def test_set_phase_to_failed(self, isolated_state):
        """Any state -> FAILED is allowed."""
        ds = DurableState("phase-failed-1")
        ds.set_phase(MITIGATING)
        ds.set_phase(FAILED)
        ds2 = DurableState("phase-failed-1")
        assert ds2.data["phase"] == FAILED

    def test_set_phase_invalid_transition_raises(self, isolated_state):
        """Invalid transition raises ValueError and does NOT write to disk."""
        ds = DurableState("phase-invalid-1")
        # triage -> closed is not allowed
        with pytest.raises(ValueError):
            ds.set_phase(CLOSED)
        # Phase must still be triage (unchanged on disk).
        ds2 = DurableState("phase-invalid-1")
        assert ds2.data.get("phase", TRIAGE) == TRIAGE

    def test_set_phase_invalid_state_string_raises(self, isolated_state):
        """Passing an unknown state string raises ValueError."""
        ds = DurableState("phase-unknown-1")
        with pytest.raises(ValueError, match="Unknown"):
            ds.set_phase("bogus_state")

    def test_set_phase_does_not_break_pending_or_committed(self, isolated_state):
        """set_phase() is additive — pending/committed data is preserved."""
        ds = DurableState("phase-compat-1")
        ds.set_pending("act", "key-x")
        ds.set_phase(MITIGATING)

        ds2 = DurableState("phase-compat-1")
        assert ds2.data["phase"] == MITIGATING
        assert ds2.pending is not None
        assert ds2.pending["key"] == "key-x"
