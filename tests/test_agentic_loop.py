"""Tests for the real agentic reason→act→observe loop (run_agentic).

The AIGateway.complete is monkeypatched to feed a scripted sequence of
Completion-like objects with JSON-action text.  This keeps the tests
deterministic while exercising the full planner/tools/MCP-gateway stack.

conftest.py's `isolated_state` autouse fixture handles STATE_DIR isolation.
"""
from __future__ import annotations

import pytest

import deadman.state as state_module
from deadman.world import World
from deadman.chaos import Chaos
from deadman.commander import Deadman, action_key
from deadman.mcp_gateway import KillSignal
from deadman.ai_gateway import Completion


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _completion(text: str) -> Completion:
    """Build a minimal Completion whose .text is the JSON-action string."""
    return Completion(text=text, backend="mock-tier-0", tier=0, from_cache=False)


def _patch_ai(agent: Deadman, responses: list[str]):
    """Replace agent.ai.complete with a callable that cycles through `responses`."""
    it = iter(responses)

    def _mock_complete(prompt: str) -> Completion:  # noqa: ARG001
        try:
            text = next(it)
        except StopIteration:
            # Return a done signal if we run out of scripted responses
            text = '{"done": true, "rationale": "scripted responses exhausted"}'
        return _completion(text)

    agent.ai.complete = _mock_complete  # type: ignore[method-assign]


INCIDENT = "test-agentic-loop-001"


# ---------------------------------------------------------------------------
# 1. Happy path: diagnose → revert → done
# ---------------------------------------------------------------------------

class TestAgenticHappyPath:
    """Model emits: read metric → revert PR-42 → done."""

    def test_tools_executed_in_order(self, isolated_state):
        state_module.reset(INCIDENT)
        world = World()
        agent = Deadman(INCIDENT, world)

        _patch_ai(agent, [
            '{"tool": "cw.get_metrics", "args": {}, "rationale": "check cpu first", "done": false}',
            '{"tool": "github.revert_pr", "args": {"pr": "PR-42"}, "rationale": "bad deploy", "done": false}',
            '{"done": true, "rationale": "revert applied, incident resolved"}',
        ])

        sb = agent.run_agentic("High CPU after deploy of PR-42")

        assert sb.survived is True
        assert world.count("revert_pr") == 1, "revert_pr should fire exactly once"
        # The world records the revert with the pr arg
        assert any(r[1] == "PR-42" for r in world.applied if r[0] == "revert_pr")

    def test_scoreboard_fields_populated(self, isolated_state):
        state_module.reset(INCIDENT + "-sb")
        world = World()
        agent = Deadman(INCIDENT + "-sb", world)

        _patch_ai(agent, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-10"}, "rationale": "revert it", "done": false}',
            '{"done": true, "rationale": "done"}',
        ])

        sb = agent.run_agentic("Incident", max_steps=8)
        assert sb.survived is True
        assert sb.double_executions == 0
        assert isinstance(sb.notes, list)
        assert len(sb.notes) >= 1


# ---------------------------------------------------------------------------
# 2. Exactly-once in the agentic path: kill mid-revert → resume → revert==1
# ---------------------------------------------------------------------------

class TestAgenticExactlyOnce:
    """Arm a kill after the revert side effect; resume; assert world.count == 1."""

    def test_kill_then_resume_exactly_once(self, isolated_state):
        incident_id = "test-agentic-eo-001"
        state_module.reset(incident_id)
        world = World()

        revert_key = action_key(incident_id, "github.revert_pr", "PR-99")
        chaos = Chaos()
        chaos.kill_process_after(revert_key)

        agent1 = Deadman(incident_id, world, chaos)
        _patch_ai(agent1, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-99"}, "rationale": "revert it", "done": false}',
            '{"done": true, "rationale": "done"}',
        ])

        with pytest.raises(KillSignal):
            agent1.run_agentic("Incident — revert PR-99")

        # Side effect happened once before the kill
        assert world.count("revert_pr") == 1

        # Fresh agent resumes
        chaos.kill_after = None
        agent2 = Deadman(incident_id, world, chaos)
        _patch_ai(agent2, [
            # After reconcile the key is already committed; model can just say done
            '{"done": true, "rationale": "already resolved on resume"}',
        ])
        sb = agent2.run_agentic("Incident — revert PR-99", resume=True)

        assert world.count("revert_pr") == 1, (
            f"EXACTLY-ONCE VIOLATED in agentic path: revert ran {world.count('revert_pr')} times"
        )
        assert sb.survived is True


# ---------------------------------------------------------------------------
# 3. Malformed model output → planner safe-hold, no crash, no destructive call
# ---------------------------------------------------------------------------

class TestAgenticMalformedOutput:
    """Garbage text → planner returns safe-hold → loop never crashes, no side effect."""

    def test_malformed_no_crash(self, isolated_state):
        incident_id = "test-agentic-malformed"
        state_module.reset(incident_id)
        world = World()
        agent = Deadman(incident_id, world)

        _patch_ai(agent, [
            "this is not json at all !!!",
            "also bad output {broken json",
            '{"done": true, "rationale": "gave up"}',
        ])

        # Should not raise
        sb = agent.run_agentic("Some incident", max_steps=8)

        assert sb.survived is True
        # No destructive tools should have been called
        assert world.count("revert_pr") == 0
        assert world.count("cordon_drain") == 0

    def test_empty_output_safe_hold(self, isolated_state):
        incident_id = "test-agentic-empty"
        state_module.reset(incident_id)
        world = World()
        agent = Deadman(incident_id, world)

        _patch_ai(agent, [
            "",
            '{"done": true, "rationale": "ok"}',
        ])

        sb = agent.run_agentic("Incident", max_steps=4)
        assert sb.survived is True
        assert world.count("revert_pr") == 0


# ---------------------------------------------------------------------------
# 4. Budget: a model that never says done stops after max_steps
# ---------------------------------------------------------------------------

class TestAgenticBudget:
    """A model that always returns a read-only tool and never says done."""

    def test_stops_at_max_steps(self, isolated_state):
        incident_id = "test-agentic-budget"
        state_module.reset(incident_id)
        world = World()
        agent = Deadman(incident_id, world)

        # Always return a diagnostic tool — never done
        infinite_responses = [
            '{"tool": "cw.get_metrics", "args": {}, "rationale": "checking", "done": false}'
        ] * 20

        _patch_ai(agent, infinite_responses)

        sb = agent.run_agentic("Never resolving incident", max_steps=5)

        # Should return without crashing
        assert sb.survived is True
        # Should not have executed destructive tools
        assert world.count("revert_pr") == 0

    def test_max_steps_zero_returns_immediately(self, isolated_state):
        incident_id = "test-agentic-budget-zero"
        state_module.reset(incident_id)
        world = World()
        agent = Deadman(incident_id, world)

        _patch_ai(agent, ['{"done": true, "rationale": "resolved"}'])

        sb = agent.run_agentic("Incident", max_steps=0)
        assert sb.survived is True
        assert world.count("revert_pr") == 0


# ---------------------------------------------------------------------------
# 5. Scope / auto-leash: revoked destructive scope → ScopeDenied → world untouched
# ---------------------------------------------------------------------------

def _patch_ai_with_depth(agent: Deadman, responses: list[str], depth: int):
    """Like _patch_ai but also forces ai.max_depth to `depth` each call.

    This lets tests simulate a degraded fallback depth without going through real
    chaos-injected tier health probing (which would bypass the monkeypatched complete).
    """
    it = iter(responses)

    def _mock_complete(prompt: str) -> Completion:  # noqa: ARG001
        try:
            text = next(it)
        except StopIteration:
            text = '{"done": true, "rationale": "scripted responses exhausted"}'
        agent.ai.max_depth = max(agent.ai.max_depth, depth)
        return _completion(text)

    agent.ai.complete = _mock_complete  # type: ignore[method-assign]


class TestAgenticAutoLeash:
    """When fallback depth revokes destructive scope, the model's destructive action
    is ScopeDenied and the world is NOT modified."""

    def test_destructive_denied_when_scope_revoked(self, isolated_state):
        incident_id = "test-agentic-leash"
        state_module.reset(incident_id)
        world = World()

        agent = Deadman(incident_id, world)

        # Simulate the AI gateway having fallen back to depth=2, which equals
        # AUTONOMY_REVOKE_AT_DEPTH (default 2) → AgentGateway revokes destructive scope.
        _patch_ai_with_depth(agent, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-55"}, "rationale": "revert it", "done": false}',
            '{"done": true, "rationale": "scope was denied, incident partially mitigated"}',
        ], depth=2)

        sb = agent.run_agentic("Incident", max_steps=8)

        # The world must be untouched by the destructive tool
        assert world.count("revert_pr") == 0, (
            "Destructive tool should not execute when scope is revoked"
        )
        assert sb.survived is True

    def test_read_only_still_works_when_scope_revoked(self, isolated_state):
        """Read-only tools must still execute even after destructive scope is revoked."""
        incident_id = "test-agentic-leash-read"
        state_module.reset(incident_id)
        world = World()

        agent = Deadman(incident_id, world)

        # Depth=2 revokes destructive scope; read-only tools (cw.get_metrics) remain allowed.
        _patch_ai_with_depth(agent, [
            '{"tool": "cw.get_metrics", "args": {}, "rationale": "diagnose", "done": false}',
            '{"done": true, "rationale": "done after diagnosis"}',
        ], depth=2)

        sb = agent.run_agentic("Incident", max_steps=8)
        assert sb.survived is True
        # No destructive tool should have run
        assert world.count("revert_pr") == 0
        assert world.count("cordon_drain") == 0


# ---------------------------------------------------------------------------
# 6. Unknown tool name → safe-hold, no crash
# ---------------------------------------------------------------------------

class TestAgenticUnknownTool:
    def test_unknown_tool_is_safe_held(self, isolated_state):
        incident_id = "test-agentic-unknown"
        state_module.reset(incident_id)
        world = World()
        agent = Deadman(incident_id, world)

        _patch_ai(agent, [
            '{"tool": "nonexistent.tool", "args": {}, "rationale": "try unknown", "done": false}',
            '{"done": true, "rationale": "done"}',
        ])

        sb = agent.run_agentic("Incident", max_steps=8)
        assert sb.survived is True
        assert world.count("revert_pr") == 0


# ---------------------------------------------------------------------------
# 7. Idempotency: second run with same incident_id skips already-committed keys
# ---------------------------------------------------------------------------

class TestAgenticIdempotentReplay:
    """A second run against the same incident and same key is skipped (not double-executed)."""

    def test_second_run_skips_committed_key(self, isolated_state):
        incident_id = "test-agentic-idem"
        state_module.reset(incident_id)
        world = World()
        agent = Deadman(incident_id, world)

        _patch_ai(agent, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-77"}, "rationale": "revert", "done": false}',
            '{"done": true, "rationale": "resolved"}',
        ])
        agent.run_agentic("Incident", max_steps=8)
        assert world.count("revert_pr") == 1

        # Second run — re-use the same agent (same audit state)
        _patch_ai(agent, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-77"}, "rationale": "revert again", "done": false}',
            '{"done": true, "rationale": "resolved again"}',
        ])
        agent.run_agentic("Incident", max_steps=8)

        # Must still be exactly 1 — the key is already committed
        assert world.count("revert_pr") == 1, (
            f"Expected 1 revert, got {world.count('revert_pr')}"
        )


# ---------------------------------------------------------------------------
# 8. Existing run() and NaiveAgent.run() are completely unaffected
# ---------------------------------------------------------------------------

class TestLegacyRunUntouched:
    """The deterministic run() path must work identically after adding run_agentic."""

    def test_deadman_run_still_works(self, isolated_state):
        incident_id = "test-legacy-run"
        state_module.reset(incident_id)
        world = World()
        chaos = Chaos()

        agent = Deadman(incident_id, world, chaos)
        sb = agent.run()

        assert sb.survived is True
        assert world.count("revert_pr") == 1

    def test_naive_run_still_works(self, isolated_state):
        from deadman.commander import NaiveAgent
        world = World()
        chaos = Chaos()
        chaos.kill_bedrock()

        sb = NaiveAgent(world).run(chaos)
        assert sb.double_executions >= 1


# ---------------------------------------------------------------------------
# 9. P1-1 fix: model omits required args → no crash, no side effect, recovers
# ---------------------------------------------------------------------------

class TestAgenticInvalidArgs:
    """A destructive tool with missing required args must NOT crash the loop nor
    execute any side effect; a later valid turn must still succeed."""

    def test_missing_required_args_does_not_crash(self, isolated_state):
        incident_id = "test-agentic-invalid-args"
        state_module.reset(incident_id)
        world = World()
        agent = Deadman(incident_id, world)

        _patch_ai(agent, [
            # Bad turn: github.revert_pr with no `pr` arg
            '{"tool": "github.revert_pr", "args": {}, "rationale": "oops no pr", "done": false}',
            # Recovery turn: valid revert
            '{"tool": "github.revert_pr", "args": {"pr": "PR-88"}, "rationale": "now correct", "done": false}',
            '{"done": true, "rationale": "resolved"}',
        ])

        # Must not raise (the bad turn would KeyError without the fix)
        sb = agent.run_agentic("Incident", max_steps=8)

        assert sb.survived is True
        # Exactly one revert — the bad turn executed nothing, the valid turn ran once
        assert world.count("revert_pr") == 1
        assert any(r[1] == "PR-88" for r in world.applied if r[0] == "revert_pr")
        # An invalid-args observation/note was recorded
        assert any("invalid-args" in n for n in sb.notes)

    def test_invalid_args_records_observation_and_no_side_effect_when_only_bad(self, isolated_state):
        incident_id = "test-agentic-invalid-only"
        state_module.reset(incident_id)
        world = World()
        agent = Deadman(incident_id, world)

        _patch_ai(agent, [
            # cordon_drain missing required `node`
            '{"tool": "k8s.cordon_drain", "args": {}, "rationale": "drain something", "done": false}',
            '{"done": true, "rationale": "giving up cleanly"}',
        ])

        sb = agent.run_agentic("Incident", max_steps=8)
        assert sb.survived is True
        assert world.count("cordon_drain") == 0
        assert any("invalid-args" in n for n in sb.notes)


# ---------------------------------------------------------------------------
# 10. P2-3 fix: read tools re-fetch on every step (not idempotency-deduped)
# ---------------------------------------------------------------------------

class TestAgenticReadToolRefetch:
    """A non-destructive diagnostic tool must execute on EVERY step, never SKIPPED."""

    def test_read_tool_executes_on_two_separate_steps(self, isolated_state):
        incident_id = "test-agentic-refetch"
        state_module.reset(incident_id)
        world = World()
        agent = Deadman(incident_id, world)

        _patch_ai(agent, [
            '{"tool": "cw.get_metrics", "args": {}, "rationale": "fetch 1", "done": false}',
            '{"tool": "cw.get_metrics", "args": {}, "rationale": "fetch 2", "done": false}',
            '{"done": true, "rationale": "done"}',
        ])

        sb = agent.run_agentic("Incident", max_steps=8)
        assert sb.survived is True

        # Both metric fetches must have EXECUTED — neither skipped-idempotent.
        executed = [n for n in sb.notes if n.startswith("[executed] cw.get_metrics")]
        skipped = [n for n in sb.notes if n.startswith("[skipped-idempotent] cw.get_metrics")]
        assert len(executed) == 2, f"Expected 2 executed reads, notes={sb.notes}"
        assert len(skipped) == 0, f"Reads must not be deduped, notes={sb.notes}"
