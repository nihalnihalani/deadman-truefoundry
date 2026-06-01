"""DEADMAN test configuration.

Every test that touches state gets an isolated temp dir for STATE_DIR via the
`isolated_state` fixture so we never pollute the repo's .deadman_state directory.
"""
from __future__ import annotations
import os
import pytest
import deadman.config as config
import deadman.state as state_module


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point config.STATE_DIR at a per-test tmp dir and ensure it exists."""
    state_dir = str(tmp_path / "deadman_state")
    os.makedirs(state_dir, exist_ok=True)
    monkeypatch.setenv("DEADMAN_STATE_DIR", state_dir)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    # Also ensure MODE is mock (tests never make real network calls)
    monkeypatch.setattr(config, "MODE", "mock")
    yield state_dir


@pytest.fixture
def fresh_world():
    """A clean in-memory World (system-of-record stub)."""
    from deadman.world import World
    return World()


@pytest.fixture
def fresh_chaos():
    """A clean Chaos instance (no toggles armed)."""
    from deadman.chaos import Chaos
    return Chaos()
