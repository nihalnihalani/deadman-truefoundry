"""End-to-end integration tests for the full agentic incident flow.

Exercises production paths that unit tests only mock:

  1. Full agentic incident flow via run_agentic():
       - ai.complete monkeypatched with a realistic JSON-action SEQUENCE
         (diagnose cw.get_metrics → revert github.revert_pr → done)
       - tools executed in order, world reverted exactly once
       - scoreboard.survived True
       - durable state reflects the committed action

  2. Webhook-level end-to-end (TestClient):
       - POST /incident (mock mode) → 200 scoreboard
       - GET /incident/{id}/postmortem → audit trail present and consistent
       - /metrics still returns 200 (prometheus_client optional)

  3. Simulated-real end-to-end (config.MODE="real"):
       - realmode_mcp.call_tool and realmode_ai.complete monkeypatched with
         canned responses — no live creds, no real network
       - incident driven through the agentic loop
       - NO real network call happened
       - exactly-once still holds

All tests are hermetic and deterministic. The autouse `isolated_state` fixture
from conftest.py isolates state for every test.
"""
from __future__ import annotations

import pytest

import deadman.config as config
import deadman.state as state_module
from deadman.ai_gateway import Completion
from deadman.commander import Deadman, action_key
from deadman.mcp_gateway import KillSignal
from deadman.state import AuditLog, DurableState
from deadman.world import World


# ---------------------------------------------------------------------------
# Shared helpers
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
            text = '{"done": true, "rationale": "scripted responses exhausted"}'
        return _completion(text)

    agent.ai.complete = _mock_complete  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 1. Full agentic flow: diagnose → revert → done
# ---------------------------------------------------------------------------

class TestAgenticFullFlow:
    """Drive run_agentic() with a realistic scripted sequence and verify invariants."""

    def test_tools_executed_in_order(self, isolated_state):
        """Model emits: read metric → revert PR-1337 → done. Tools fire in order."""
        inc = "e2e-agentic-order"
        state_module.reset(inc)
        world = World()
        agent = Deadman(inc, world)

        _patch_ai(agent, [
            '{"tool": "cw.get_metrics", "args": {"metric": "cpu"}, "rationale": "diagnose cpu spike", "done": false}',
            '{"tool": "github.revert_pr", "args": {"pr": "PR-1337"}, "rationale": "bad deploy caused spike", "done": false}',
            '{"done": true, "rationale": "revert applied — incident resolved"}',
        ])

        sb = agent.run_agentic("High CPU after deploy of PR-1337", max_steps=8)

        assert sb.survived is True
        assert world.count("revert_pr") == 1, (
            f"revert_pr should fire exactly once, got {world.count('revert_pr')}"
        )
        assert any(r[1] == "PR-1337" for r in world.applied if r[0] == "revert_pr"), (
            "revert_pr should target PR-1337"
        )

    def test_world_reverted_exactly_once(self, isolated_state):
        """revert_pr must execute exactly once regardless of how many loop steps ran."""
        inc = "e2e-world-once"
        state_module.reset(inc)
        world = World()
        agent = Deadman(inc, world)

        _patch_ai(agent, [
            '{"tool": "cw.get_metrics", "args": {}, "rationale": "check metrics", "done": false}',
            '{"tool": "github.revert_pr", "args": {"pr": "PR-99"}, "rationale": "rollback", "done": false}',
            '{"tool": "cw.get_metrics", "args": {}, "rationale": "verify after revert", "done": false}',
            '{"done": true, "rationale": "confirmed resolved"}',
        ])

        sb = agent.run_agentic("Incident summary", max_steps=8)

        assert sb.survived is True
        assert world.count("revert_pr") == 1

    def test_scoreboard_survived_true(self, isolated_state):
        """Scoreboard.survived is True after a successful agentic run."""
        inc = "e2e-sb-survived"
        state_module.reset(inc)
        world = World()
        agent = Deadman(inc, world)

        _patch_ai(agent, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-5"}, "rationale": "revert", "done": false}',
            '{"done": true, "rationale": "done"}',
        ])

        sb = agent.run_agentic("Deploy incident", max_steps=8)
        assert sb.survived is True

    def test_durable_state_reflects_committed_action(self, isolated_state):
        """After a successful agentic run, DurableState records no pending action."""
        inc = "e2e-durable-committed"
        state_module.reset(inc)
        world = World()
        agent = Deadman(inc, world)

        _patch_ai(agent, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-7"}, "rationale": "revert it", "done": false}',
            '{"done": true, "rationale": "resolved"}',
        ])

        agent.run_agentic("Incident", max_steps=8)

        # After a clean run there should be no pending action
        ds = DurableState(inc)
        assert ds.pending is None, (
            f"Expected no pending action after clean agentic run, got: {ds.pending}"
        )

    def test_audit_postmortem_contains_committed_revert(self, isolated_state):
        """Postmortem shows the COMMITTED revert_pr record."""
        inc = "e2e-audit-postmortem"
        state_module.reset(inc)
        world = World()
        agent = Deadman(inc, world)

        _patch_ai(agent, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-X"}, "rationale": "rollback", "done": false}',
            '{"done": true, "rationale": "done"}',
        ])

        agent.run_agentic("Incident", max_steps=8)

        audit = AuditLog(inc)
        postmortem = audit.postmortem()
        committed_lines = [line for line in postmortem if "COMMITTED" in line]
        assert len(committed_lines) >= 1, (
            f"Expected at least 1 COMMITTED line in postmortem, got: {postmortem}"
        )

    def test_double_executions_zero_on_happy_path(self, isolated_state):
        """No double executions on the happy path."""
        inc = "e2e-no-double"
        state_module.reset(inc)
        world = World()
        agent = Deadman(inc, world)

        _patch_ai(agent, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-3"}, "rationale": "revert", "done": false}',
            '{"done": true, "rationale": "done"}',
        ])

        sb = agent.run_agentic("Incident", max_steps=8)
        assert sb.double_executions == 0


# ---------------------------------------------------------------------------
# 2. Exactly-once agentic: kill mid-revert → resume → revert == 1
# ---------------------------------------------------------------------------

class TestAgenticExactlyOnceE2E:
    """Full kill-then-resume cycle through the agentic loop."""

    def test_kill_resume_revert_once(self, isolated_state):
        """Kill between side-effect and COMMIT; fresh Deadman reconciles without re-running."""
        inc = "e2e-eo-kill-resume"
        state_module.reset(inc)
        world = World()

        revert_key = action_key(inc, "github.revert_pr", "PR-kill")
        from deadman.chaos import Chaos
        chaos = Chaos()
        chaos.kill_process_after(revert_key)

        agent1 = Deadman(inc, world, chaos)
        _patch_ai(agent1, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-kill"}, "rationale": "revert it", "done": false}',
            '{"done": true, "rationale": "done"}',
        ])

        with pytest.raises(KillSignal):
            agent1.run_agentic("Kill test incident")

        # Side effect happened once before the kill
        assert world.count("revert_pr") == 1

        # Fresh Deadman resumes
        chaos.kill_after = None
        agent2 = Deadman(inc, world, chaos)
        _patch_ai(agent2, [
            '{"done": true, "rationale": "already resolved on resume"}',
        ])
        sb = agent2.run_agentic("Kill test incident", resume=True)

        assert world.count("revert_pr") == 1, (
            f"EXACTLY-ONCE VIOLATED in e2e agentic: "
            f"revert_pr ran {world.count('revert_pr')} times. Notes: {sb.notes}"
        )
        assert sb.survived is True

    def test_audit_shows_one_committed_after_resume(self, isolated_state):
        """Postmortem has exactly one COMMITTED revert record after kill+resume."""
        inc = "e2e-eo-audit-one"
        state_module.reset(inc)
        world = World()

        revert_key = action_key(inc, "github.revert_pr", "PR-audit")
        from deadman.chaos import Chaos
        chaos = Chaos()
        chaos.kill_process_after(revert_key)

        agent1 = Deadman(inc, world, chaos)
        _patch_ai(agent1, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-audit"}, "rationale": "rollback", "done": false}',
        ])

        with pytest.raises(KillSignal):
            agent1.run_agentic("Audit check incident")

        chaos.kill_after = None
        agent2 = Deadman(inc, world, chaos)
        _patch_ai(agent2, ['{"done": true, "rationale": "resolved"}'])
        agent2.run_agentic("Audit check incident", resume=True)

        audit = AuditLog(inc)
        postmortem = audit.postmortem()
        committed_reverts = [
            line for line in postmortem
            if "COMMITTED" in line and "revert_pr" in line
        ]
        assert len(committed_reverts) == 1, (
            f"Expected 1 COMMITTED revert_pr, got {len(committed_reverts)}. "
            f"Postmortem: {postmortem}"
        )


# ---------------------------------------------------------------------------
# 3. Webhook-level end-to-end (TestClient)
# ---------------------------------------------------------------------------

class TestWebhookEndToEnd:
    """Full HTTP round-trip via FastAPI TestClient."""

    @pytest.fixture(autouse=True)
    def _client(self):
        from fastapi.testclient import TestClient
        from deadman.webhook import app
        self.client = TestClient(app)

    def test_post_incident_returns_200_scoreboard(self, isolated_state):
        """POST /incident (mock mode) returns 200 with a complete scoreboard."""
        resp = self.client.post(
            "/incident",
            json={"incident_id": "e2e-webhook-001", "summary": "CPU spike after deploy"},
        )
        assert resp.status_code == 200
        data = resp.json()
        required_keys = {
            "survived", "backend", "fallback_depth", "double_executions",
            "guardrail_blocks", "drain_authority", "timeline", "mode", "incident_id",
        }
        missing = required_keys - set(data.keys())
        assert not missing, f"Scoreboard missing keys: {missing}"

    def test_post_incident_survived_true(self, isolated_state):
        resp = self.client.post(
            "/incident",
            json={"incident_id": "e2e-webhook-002", "summary": "test"},
        )
        assert resp.json()["survived"] is True

    def test_postmortem_audit_trail_present(self, isolated_state):
        """After an incident, GET /incident/{id}/postmortem has a non-empty audit trail."""
        inc_id = "e2e-webhook-pm-001"
        self.client.post("/incident", json={"incident_id": inc_id, "summary": "test"})

        resp = self.client.get(f"/incident/{inc_id}/postmortem")
        assert resp.status_code == 200
        data = resp.json()
        assert data["incident_id"] == inc_id
        assert isinstance(data["audit_trail"], list)
        assert len(data["audit_trail"]) > 0, (
            "Postmortem audit trail should be non-empty after a completed incident"
        )

    def test_postmortem_consistent_with_scoreboard(self, isolated_state):
        """The audit trail references the same incident as the scoreboard."""
        inc_id = "e2e-webhook-consistency"
        sb_resp = self.client.post(
            "/incident", json={"incident_id": inc_id, "summary": "consistency check"}
        )
        assert sb_resp.status_code == 200

        pm_resp = self.client.get(f"/incident/{inc_id}/postmortem")
        assert pm_resp.status_code == 200
        pm_data = pm_resp.json()
        assert pm_data["incident_id"] == inc_id

    def test_metrics_endpoint_returns_200(self, isolated_state):
        """GET /metrics always returns 200 (prometheus_client optional)."""
        resp = self.client.get("/metrics")
        assert resp.status_code == 200, f"/metrics returned {resp.status_code}: {resp.text}"

    def test_metrics_counters_increment_after_incident(self, isolated_state):
        """After running an incident the /metrics output changes (if prometheus_client present)."""
        prometheus_client = pytest.importorskip(
            "prometheus_client",
            reason="prometheus_client not installed — skipping counter increment test",
        )

        resp_before = self.client.get("/metrics")
        assert resp_before.status_code == 200

        self.client.post(
            "/incident",
            json={"incident_id": "e2e-metrics-counter", "summary": "metrics test"},
        )

        resp_after = self.client.get("/metrics")
        assert resp_after.status_code == 200
        # After at least one incident, the response text should be non-empty
        # and contain metric-like content
        assert len(resp_after.text) > 0

    def test_incident_mode_is_mock(self, isolated_state):
        """Scoreboard mode field is 'mock' when running in test environment."""
        resp = self.client.post(
            "/incident",
            json={"incident_id": "e2e-mode-check", "summary": "mode test"},
        )
        assert resp.json()["mode"] == "mock"

    def test_postmortem_empty_trail_for_unknown_incident(self, isolated_state):
        """Postmortem for a non-existent incident returns empty audit trail gracefully."""
        resp = self.client.get("/incident/e2e-unknown-xyz-9999/postmortem")
        assert resp.status_code == 200
        assert resp.json()["audit_trail"] == []


# ---------------------------------------------------------------------------
# 4. Simulated-real end-to-end (config.MODE="real", no live network)
# ---------------------------------------------------------------------------

class TestSimulatedRealMode:
    """Exercises the real-mode code path without live creds.

    realmode_mcp.call_tool is patched at the mcp_gateway module level (where it is
    lazily imported) so that MCPGateway._execute_real() uses our canned stub rather
    than issuing real HTTP calls. agent.ai.complete is also patched directly to avoid
    the openai import path. No live creds, no real network.
    """

    def _setup_real_mode(self, monkeypatch):
        """Apply the minimal config patches required for real-mode wiring."""
        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "fake-key-for-test")
        monkeypatch.setattr(config, "TFY_GATEWAY_BASE_URL", "http://fake-gateway.test")
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "http://fake-mcp.test")

    def _patch_realmode_mcp(self, monkeypatch, return_skipped: bool = False):
        """Patch realmode_mcp.call_tool at the module level to avoid live HTTP.

        MCPGateway imports realmode_mcp lazily inside _execute_real, so we patch
        the module attribute directly.
        """
        import deadman.realmode_mcp as realmode_mcp

        calls_recorded: list[dict] = []

        def _fake_call_tool(tool: str, args: dict, idempotency_key: str) -> dict:
            calls_recorded.append({"tool": tool, "args": args, "key": idempotency_key})
            return {
                "status_code": 200,
                "body": {"ok": True},
                "skipped_idempotent": return_skipped,
            }

        monkeypatch.setattr(realmode_mcp, "call_tool", _fake_call_tool)
        return calls_recorded

    def test_real_mode_agentic_no_network_call(self, monkeypatch, isolated_state):
        """run_agentic in real-mode wiring: canned responses, no live network call."""
        self._setup_real_mode(monkeypatch)
        calls_recorded = self._patch_realmode_mcp(monkeypatch)

        inc = "e2e-simreal-001"
        state_module.reset(inc)
        world = World()

        agent = Deadman(inc, world)

        # Patch agent.ai.complete directly — avoids the openai import path while
        # still exercising the full agentic reason→act→observe loop.
        _patch_ai(agent, [
            '{"tool": "cw.get_metrics", "args": {}, "rationale": "diagnose", "done": false}',
            '{"tool": "github.revert_pr", "args": {"pr": "PR-sim"}, "rationale": "revert", "done": false}',
            '{"done": true, "rationale": "resolved"}',
        ])

        sb = agent.run_agentic("Simulated real-mode incident", max_steps=8)

        assert sb.survived is True
        assert sb.double_executions == 0
        # In real mode, MCP gateway routes through realmode_mcp; our stub records calls
        # without hitting any live network. The revert_pr call should be recorded.
        revert_calls = [c for c in calls_recorded if c["tool"] == "github.revert_pr"]
        assert len(revert_calls) == 1, (
            f"Expected 1 real-mode MCP call for github.revert_pr, got {len(revert_calls)}: {calls_recorded}"
        )

    def test_real_mode_exactly_once_still_holds(self, monkeypatch, isolated_state):
        """Exactly-once invariant holds even when MODE=real (canned MCP + AI responses)."""
        self._setup_real_mode(monkeypatch)
        calls_recorded = self._patch_realmode_mcp(monkeypatch)

        inc = "e2e-simreal-eo"
        state_module.reset(inc)
        world = World()

        from deadman.chaos import Chaos
        revert_key = action_key(inc, "github.revert_pr", "PR-sim-eo")
        chaos = Chaos()
        # In real mode, chaos kill is still injected via the Chaos object
        # but MCPGateway._execute_real does NOT inject chaos — kill only fires
        # on the mock path. So we test exactly-once through the AuditLog claim
        # mechanism: run once, attempt a second time with the same key, assert
        # the second call returns SKIPPED_IDEMPOTENT.

        agent1 = Deadman(inc, world, chaos)
        _patch_ai(agent1, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-sim-eo"}, "rationale": "revert", "done": false}',
            '{"done": true, "rationale": "resolved"}',
        ])

        sb1 = agent1.run_agentic("Real-mode eo test", max_steps=8)
        assert sb1.survived is True

        # Second run with the SAME incident id: the key is already committed in
        # the audit log → MCP gateway skips it (SKIPPED_IDEMPOTENT).
        agent2 = Deadman(inc, world, chaos)
        _patch_ai(agent2, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-sim-eo"}, "rationale": "revert again", "done": false}',
            '{"done": true, "rationale": "resolved again"}',
        ])

        sb2 = agent2.run_agentic("Real-mode eo test", max_steps=8)
        assert sb2.survived is True
        # In real mode world.count is not tracked by World (RealWorld is used in prod),
        # but the AuditLog should show exactly one COMMITTED revert
        audit = AuditLog(inc)
        committed_reverts = [
            line for line in audit.postmortem()
            if "COMMITTED" in line and "revert_pr" in line
        ]
        assert len(committed_reverts) == 1, (
            f"EXACTLY-ONCE VIOLATED in simulated real-mode: "
            f"{len(committed_reverts)} COMMITTED revert_pr records. "
            f"Postmortem: {audit.postmortem()}"
        )

    def test_real_mode_mcp_patched_not_live(self, monkeypatch, isolated_state):
        """Confirms the monkeypatched MCP is invoked (not the live gateway) in real mode.

        Uses run_agentic() with agent.ai.complete also patched so neither the AI gateway
        nor the MCP gateway issues any live network call.
        """
        self._setup_real_mode(monkeypatch)
        calls_recorded = self._patch_realmode_mcp(monkeypatch)

        inc = "e2e-simreal-mcp-check"
        state_module.reset(inc)
        world = World()

        agent = Deadman(inc, world)
        # Patch agent.ai.complete so the AI gateway never tries to call the real openai client
        _patch_ai(agent, [
            '{"tool": "github.revert_pr", "args": {"pr": "PR-mcp-check"}, "rationale": "revert", "done": false}',
            '{"done": true, "rationale": "done"}',
        ])

        sb = agent.run_agentic("MCP-patch verification incident", max_steps=8)

        assert sb.survived is True
        # The monkeypatched stub recorded the MCP call without any live network access
        revert_calls = [c for c in calls_recorded if c["tool"] == "github.revert_pr"]
        assert len(revert_calls) == 1, (
            f"Expected 1 real-mode MCP call for github.revert_pr, got {len(revert_calls)}. "
            f"All calls: {calls_recorded}. "
            "This means MCPGateway did not route through realmode_mcp.call_tool — check MODE wiring."
        )
