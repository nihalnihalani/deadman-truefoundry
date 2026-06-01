"""Coverage-gap tests: generalized resume reconcile, RealWorld query branches,
resolve_model_id, _depth_from_headers secondary path, webhook HMAC auth, and
kill-switch trips.

All tests are hermetic (no live network, no live AWS) and must stay green
alongside the existing 288-test suite.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

import deadman.config as config
import deadman.state as state_module
from deadman.agent_gateway import AgentGateway, READ_ONLY, FULL_SCOPE
from deadman.chaos import Chaos
from deadman.commander import Deadman, action_key
from deadman.mcp_gateway import KillSignal
from deadman.state import AuditLog, DurableState
from deadman.world import RealWorld, World


# ---------------------------------------------------------------------------
# 1. Generalized resume reconcile: cordon_drain & asg_scale
# ---------------------------------------------------------------------------

class TestResumeReconcileCordonDrain:
    """kill mid-cordon_drain -> resume in fresh Deadman -> cordon executed exactly once."""

    def _cordon_key(self, incident_id: str) -> str:
        return action_key(incident_id, "cordon_drain", "prod-node-7")

    def test_cordon_kill_then_resume_exactly_once(self, isolated_state):
        """Side effect fires once; resume reconciles via is_cordoned(), no re-run."""
        incident_id = "reconcile-cordon-01"
        state_module.reset(incident_id)

        cordon_key = self._cordon_key(incident_id)

        # First run: kill immediately after cordon_drain side effect lands
        world = World()
        chaos = Chaos()
        chaos.kill_process_after(cordon_key)
        agent1 = Deadman(incident_id, world, chaos)
        with pytest.raises(KillSignal):
            agent1.run()

        # cordon_drain must have fired exactly once before the kill
        assert world.count("cordon_drain") == 1, (
            f"Expected 1 cordon_drain before kill, got {world.count('cordon_drain')}"
        )

        # Second run: fresh Deadman, same world (system-of-record shows cordon done)
        chaos.kill_after = None
        agent2 = Deadman(incident_id, world, chaos)
        sb = agent2.run(resume=True)

        # cordon_drain must NOT have doubled
        assert world.count("cordon_drain") == 1, (
            f"EXACTLY-ONCE VIOLATED for cordon_drain: ran {world.count('cordon_drain')} time(s). "
            f"Notes: {sb.notes}"
        )
        assert sb.survived is True

    def test_cordon_resume_reconcile_note_present(self, isolated_state):
        """Resume path emits a reconciliation note for cordon_drain."""
        incident_id = "reconcile-cordon-02"
        state_module.reset(incident_id)

        cordon_key = self._cordon_key(incident_id)
        world = World()
        chaos = Chaos()
        chaos.kill_process_after(cordon_key)
        agent1 = Deadman(incident_id, world, chaos)
        with pytest.raises(KillSignal):
            agent1.run()

        chaos.kill_after = None
        sb = Deadman(incident_id, world, chaos).run(resume=True)

        # Some note must mention reconcile / system-of-record / already applied
        reconcile_notes = [
            n for n in sb.notes
            if any(kw in n.lower() for kw in ("reconcil", "system-of-record", "already applied", "skip"))
        ]
        assert reconcile_notes, f"No reconcile note found. Got: {sb.notes}"

    def test_cordon_resume_multiple_times_no_double(self, isolated_state):
        """Resuming 3× after a cordon kill never double-executes."""
        incident_id = "reconcile-cordon-03"
        state_module.reset(incident_id)

        cordon_key = self._cordon_key(incident_id)
        world = World()
        chaos = Chaos()
        chaos.kill_process_after(cordon_key)
        with pytest.raises(KillSignal):
            Deadman(incident_id, world, chaos).run()

        chaos.kill_after = None
        for i in range(3):
            sb = Deadman(incident_id, world, chaos).run(resume=True)
            cnt = world.count("cordon_drain")
            assert cnt == 1, (
                f"EXACTLY-ONCE VIOLATED on resume #{i+1}: cordon_drain ran {cnt} times"
            )


class TestResumeReconcileAsgScale:
    """kill mid-asg_scale -> resume -> asg_scale executed exactly once."""

    def _asg_key(self, incident_id: str) -> str:
        # asg.scale is not an action Deadman.run() calls directly, so we drive
        # _reconcile_pending directly and use MCPGateway to simulate the scenario.
        return action_key(incident_id, "asg_scale", "prod-asg")

    def test_reconcile_pending_asg_scale_true(self, isolated_state):
        """_reconcile_pending returns True when world.is_scaled says already done."""
        incident_id = "reconcile-asg-01"
        state_module.reset(incident_id)

        world = World()
        # Pre-populate the world: asg was already scaled
        world.asg_scale("prod-asg", 5)

        asg_key = self._asg_key(incident_id)
        ds = DurableState(incident_id)
        ds.set_pending("asg_scale", asg_key)

        agent = Deadman(incident_id, world, chaos=Chaos())
        # _reconcile_pending should detect the scale already happened
        result = agent._reconcile_pending("asg_scale", "prod-asg")
        assert result is True

    def test_reconcile_pending_asg_scale_false_when_not_done(self, isolated_state):
        """_reconcile_pending returns False when the asg was NOT yet scaled."""
        incident_id = "reconcile-asg-02"
        state_module.reset(incident_id)

        world = World()  # empty world — no asg_scale ever happened
        agent = Deadman(incident_id, world, chaos=Chaos())
        result = agent._reconcile_pending("asg_scale", "my-asg")
        assert result is False

    def test_reconcile_pending_asg_scale_via_alias(self, isolated_state):
        """Both 'asg.scale' and 'asg_scale' action name aliases resolve correctly."""
        incident_id = "reconcile-asg-03"
        state_module.reset(incident_id)

        world = World()
        world.asg_scale("fleet-asg", 3)

        agent = Deadman(incident_id, world, chaos=Chaos())
        # Dot-notation alias
        assert agent._reconcile_pending("asg.scale", "fleet-asg") is True
        # underscore alias
        assert agent._reconcile_pending("asg_scale", "fleet-asg") is True

    def test_run_agentic_cordon_exactly_once_via_scripted_ai(self, isolated_state):
        """run_agentic with scripted AI: cordon_drain executes exactly once on kill+resume."""
        incident_id = "agentic-cordon-kill"
        state_module.reset(incident_id)

        world = World()
        cordon_key = action_key(incident_id, "k8s.cordon_drain", "prod-node-7")

        # Script the AI to choose cordon_drain then declare done
        call_count = {"n": 0}

        def scripted_complete(prompt: str):  # type: ignore[override]
            from deadman.ai_gateway import Completion
            n = call_count["n"]
            call_count["n"] += 1
            if n == 0:
                text = json.dumps({
                    "tool": "k8s.cordon_drain",
                    "args": {"node": "prod-node-7"},
                    "rationale": "cordon the bad node",
                    "done": False,
                })
            else:
                text = json.dumps({"done": True, "rationale": "all clear"})
            return Completion(text=text, backend="mock", tier=0, from_cache=False)

        # First run: kill mid-cordon
        chaos = Chaos()
        chaos.kill_process_after(cordon_key)
        agent1 = Deadman(incident_id, world, chaos)
        agent1.ai.complete = scripted_complete  # type: ignore[method-assign]
        with pytest.raises(KillSignal):
            agent1.run_agentic("node is degraded", max_steps=4)

        assert world.count("cordon_drain") == 1

        # Resume: fresh agent, same world, scripted AI returns done immediately
        chaos.kill_after = None
        call_count["n"] = 0

        def scripted_done(prompt: str):  # type: ignore[override]
            from deadman.ai_gateway import Completion
            return Completion(
                text=json.dumps({"done": True, "rationale": "already handled"}),
                backend="mock", tier=0, from_cache=False,
            )

        agent2 = Deadman(incident_id, world, chaos)
        agent2.ai.complete = scripted_done  # type: ignore[method-assign]
        sb = agent2.run_agentic("node is degraded", max_steps=4, resume=True)

        assert world.count("cordon_drain") == 1, (
            f"EXACTLY-ONCE VIOLATED: cordon_drain ran {world.count('cordon_drain')} times. "
            f"Notes: {sb.notes}"
        )


# ---------------------------------------------------------------------------
# 2. RealWorld reconciliation branches
# ---------------------------------------------------------------------------

class TestRealWorldIsCordonedBranches:
    """Unit-test the three-layer escalation in RealWorld.is_cordoned()."""

    def test_in_memory_path_cordoned(self, isolated_state):
        """is_cordoned returns True from the in-memory intent log."""
        w = RealWorld()
        w.cordon_drain("node-A")
        assert w.is_cordoned("node-A") is True

    def test_in_memory_path_not_cordoned(self, isolated_state):
        """is_cordoned returns False when the node was never cordoned in-memory."""
        w = RealWorld()
        assert w.is_cordoned("node-X") is False

    def test_audit_log_committed_path(self, isolated_state):
        """is_cordoned returns True via the durable audit-log COMMITTED record."""
        incident_id = "realworld-cordon-audit"
        audit = AuditLog(incident_id)
        audit.write({
            "status": "COMMITTED",
            "tool": "k8s.cordon_drain",
            "key": f"{incident_id}::cordon_drain::node-B",
        })
        w = RealWorld(audit_log=audit)
        # in-memory is empty; should find via audit log
        assert w.is_cordoned("node-B") is True

    def test_audit_log_committed_wrong_node_false(self, isolated_state):
        """is_cordoned returns False when the COMMITTED record is for a different node."""
        incident_id = "realworld-cordon-audit-2"
        audit = AuditLog(incident_id)
        audit.write({
            "status": "COMMITTED",
            "tool": "k8s.cordon_drain",
            "key": f"{incident_id}::cordon_drain::node-B",
        })
        w = RealWorld(audit_log=audit)
        assert w.is_cordoned("node-DIFFERENT") is False

    def test_live_query_path_returns_true_when_mcp_says_cordoned(self, monkeypatch, isolated_state):
        """is_cordoned returns True via the live-query path when MCP says cordoned."""
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "http://fake-mcp:8080")

        import deadman.realmode_mcp as realmode_mcp_mod
        monkeypatch.setattr(
            realmode_mcp_mod, "call_tool",
            lambda tool, args, idempotency_key=None: {"body": {"cordoned": True}},
        )

        w = RealWorld()
        assert w.is_cordoned("node-C") is True

    def test_live_query_path_returns_false_when_mcp_says_not_cordoned(
        self, monkeypatch, isolated_state
    ):
        """is_cordoned returns False via the live-query path when MCP says not cordoned."""
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "http://fake-mcp:8080")

        import deadman.realmode_mcp as realmode_mcp_mod
        monkeypatch.setattr(
            realmode_mcp_mod, "call_tool",
            lambda tool, args, idempotency_key=None: {"body": {"cordoned": False}},
        )

        w = RealWorld()
        assert w.is_cordoned("node-D") is False

    def test_live_query_unschedulable_spec_path(self, monkeypatch, isolated_state):
        """is_cordoned returns True when spec.unschedulable is True."""
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "http://fake-mcp:8080")

        import deadman.realmode_mcp as realmode_mcp_mod
        monkeypatch.setattr(
            realmode_mcp_mod, "call_tool",
            lambda tool, args, idempotency_key=None: {
                "body": {"spec": {"unschedulable": True}}
            },
        )

        w = RealWorld()
        assert w.is_cordoned("node-spec") is True

    def test_live_query_failure_falls_back_gracefully(self, monkeypatch, isolated_state):
        """A live-query failure does NOT raise; returns False (graceful degradation)."""
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "http://fake-mcp:8080")

        import deadman.realmode_mcp as realmode_mcp_mod

        def _raise(*a, **kw):
            raise RuntimeError("network timeout")

        monkeypatch.setattr(realmode_mcp_mod, "call_tool", _raise)

        w = RealWorld()
        # Must not raise
        result = w.is_cordoned("node-E")
        assert result is False

    def test_no_tfy_url_skips_live_query(self, monkeypatch, isolated_state):
        """When TFY_MCP_GATEWAY_URL is empty, live query is never attempted."""
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "")

        # If live query were attempted it would import realmode_mcp which would
        # be patched to raise — that would surface if it runs.
        import deadman.realmode_mcp as realmode_mcp_mod

        def _must_not_run(*a, **kw):
            raise AssertionError("live query must not run without TFY_MCP_GATEWAY_URL")

        monkeypatch.setattr(realmode_mcp_mod, "call_tool", _must_not_run)

        w = RealWorld()
        # Safe: should return False without hitting realmode_mcp
        assert w.is_cordoned("node-F") is False


class TestRealWorldIsScaledBranches:
    """Unit-test the in-memory and audit-log branches in RealWorld.is_scaled()."""

    def test_in_memory_path_scaled(self, isolated_state):
        w = RealWorld()
        w.asg_scale("fleet", 3)
        assert w.is_scaled("fleet") is True

    def test_in_memory_not_scaled(self, isolated_state):
        w = RealWorld()
        assert w.is_scaled("other-fleet") is False

    def test_audit_log_committed_path(self, isolated_state):
        incident_id = "realworld-asg-audit"
        audit = AuditLog(incident_id)
        audit.write({
            "status": "COMMITTED",
            "tool": "asg.scale",
            "key": f"{incident_id}::asg_scale::my-asg",
        })
        w = RealWorld(audit_log=audit)
        assert w.is_scaled("my-asg") is True

    def test_audit_log_wrong_asg_false(self, isolated_state):
        incident_id = "realworld-asg-audit-2"
        audit = AuditLog(incident_id)
        audit.write({
            "status": "COMMITTED",
            "tool": "asg.scale",
            "key": f"{incident_id}::asg_scale::my-asg",
        })
        w = RealWorld(audit_log=audit)
        assert w.is_scaled("other-asg") is False


# ---------------------------------------------------------------------------
# 3. resolve_model_id — 0% coverage, production-critical
# ---------------------------------------------------------------------------

class TestResolveModelId:
    """Hermetic tests for realmode_ai.resolve_model_id() using a mocked boto3 client."""

    def _make_bedrock_client(
        self,
        profiles: list[dict] | None = None,
        models: list[dict] | None = None,
        profiles_raises: Exception | None = None,
        models_raises: Exception | None = None,
    ) -> MagicMock:
        """Build a minimal mock of boto3 bedrock client."""
        client = MagicMock()

        if profiles_raises is not None:
            client.list_inference_profiles.side_effect = profiles_raises
        else:
            client.list_inference_profiles.return_value = {
                "inferenceProfileSummaries": profiles or []
            }

        if models_raises is not None:
            client.list_foundation_models.side_effect = models_raises
        else:
            client.list_foundation_models.return_value = {
                "modelSummaries": models or []
            }
        return client

    def _fresh_module(self):
        """Reload realmode_ai with an empty cache between tests."""
        import deadman.realmode_ai as m
        m._resolved_ids.clear()
        return m

    def test_prefers_global_prefix_inference_profile(self, isolated_state):
        """resolve_model_id picks an inference profile with a 'global.' or 'us.' prefix
        over one with a non-preferred prefix (e.g. 'eu.')."""
        m = self._fresh_module()
        client = self._make_bedrock_client(
            profiles=[
                # 'global.' and 'us.' are both "preferred"; 'eu.' is non-preferred (secondary)
                {"inferenceProfileId": "global.claude-opus-4-8-v2:0"},
                {"inferenceProfileId": "eu.claude-opus-4-8-v3:0"},
            ]
        )
        with patch("boto3.client", return_value=client):
            result = m.resolve_model_id("claude-opus-4-8", "us-east-1")
        # The 'global.' entry is preferred over the 'eu.' entry
        assert result == "global.claude-opus-4-8-v2:0"

    def test_prefers_us_prefix_over_non_prefixed(self, isolated_state):
        """'us.' prefix is also preferred over non-prefixed profiles."""
        m = self._fresh_module()
        client = self._make_bedrock_client(
            profiles=[
                {"inferenceProfileId": "ap.claude-opus-4-8-v1:0"},
                {"inferenceProfileId": "us.claude-opus-4-8-v2:0"},
            ]
        )
        with patch("boto3.client", return_value=client):
            result = m.resolve_model_id("claude-opus-4-8", "us-east-1")
        assert result == "us.claude-opus-4-8-v2:0"

    def test_substring_matches_family_hint(self, isolated_state):
        """Profiles matching the family hint substring are selected."""
        m = self._fresh_module()
        client = self._make_bedrock_client(
            profiles=[
                {"inferenceProfileId": "us.llama4-maverick-17b-v1"},
                {"inferenceProfileId": "us.unrelated-model-v1"},
            ]
        )
        with patch("boto3.client", return_value=client):
            result = m.resolve_model_id("llama4-maverick", "us-west-2")
        assert "llama4-maverick" in result

    def test_caches_result_second_call_no_boto3(self, isolated_state):
        """Second call to resolve_model_id with same (family, region) uses the cache."""
        m = self._fresh_module()
        client = self._make_bedrock_client(
            profiles=[{"inferenceProfileId": "global.claude-opus-4-8-cached"}]
        )
        with patch("boto3.client", return_value=client) as mock_boto3:
            result1 = m.resolve_model_id("claude-opus-4-8", "us-east-1")
            result2 = m.resolve_model_id("claude-opus-4-8", "us-east-1")

        # boto3.client should only have been called once
        assert mock_boto3.call_count == 1
        assert result1 == result2 == "global.claude-opus-4-8-cached"

    def test_falls_back_to_config_when_boto3_absent(self, isolated_state):
        """When boto3 is unavailable (ImportError), returns config best-known id."""
        m = self._fresh_module()
        with patch.dict(sys.modules, {"boto3": None}):
            result = m.resolve_model_id("claude-opus-4-8", "us-east-1")
        # Should return a non-empty string matching config
        assert result
        # Should match the config best-known id
        expected = m._best_known_id("claude-opus-4-8")
        assert result == expected

    def test_falls_back_when_no_profile_matches(self, isolated_state):
        """When no profile/model matches, falls back to config best-known id."""
        m = self._fresh_module()
        client = self._make_bedrock_client(
            profiles=[{"inferenceProfileId": "global.completely-different-model"}],
            models=[{"modelId": "also.completely.different"}],
        )
        with patch("boto3.client", return_value=client):
            result = m.resolve_model_id("claude-opus-4-8", "us-east-1")
        expected = m._best_known_id("claude-opus-4-8")
        assert result == expected

    def test_falls_back_when_boto3_raises(self, isolated_state):
        """When boto3.client() itself raises, returns config best-known id without crashing."""
        m = self._fresh_module()
        with patch("boto3.client", side_effect=Exception("AWS error")):
            # Must not raise
            result = m.resolve_model_id("claude-opus-4-8", "us-east-1")
        expected = m._best_known_id("claude-opus-4-8")
        assert result == expected

    def test_foundation_model_fallback_path(self, isolated_state):
        """When list_inference_profiles fails but list_foundation_models succeeds, uses foundation model."""
        m = self._fresh_module()
        client = self._make_bedrock_client(
            profiles_raises=Exception("profiles API unavailable"),
            models=[
                {"modelId": "anthropic.claude-opus-4-8-fm-v1"},
            ],
        )
        with patch("boto3.client", return_value=client):
            result = m.resolve_model_id("claude-opus-4-8", "us-east-1")
        assert result == "anthropic.claude-opus-4-8-fm-v1"

    def test_different_family_region_cached_separately(self, isolated_state):
        """Cache key is (family, region): different pairs are cached independently."""
        m = self._fresh_module()
        client_east = self._make_bedrock_client(
            profiles=[{"inferenceProfileId": "global.claude-opus-4-8-east"}]
        )
        client_west = self._make_bedrock_client(
            profiles=[{"inferenceProfileId": "global.claude-opus-4-8-west"}]
        )

        def boto3_side_effect(service, region_name):
            if region_name == "us-east-1":
                return client_east
            return client_west

        with patch("boto3.client", side_effect=boto3_side_effect):
            r_east = m.resolve_model_id("claude-opus-4-8", "us-east-1")
            r_west = m.resolve_model_id("claude-opus-4-8", "us-west-2")

        assert r_east == "global.claude-opus-4-8-east"
        assert r_west == "global.claude-opus-4-8-west"


# ---------------------------------------------------------------------------
# 4. _depth_from_headers secondary path
# ---------------------------------------------------------------------------

class TestDepthFromHeadersSecondaryPath:
    """Unit-test the secondary (x-tfy-backend / x-tfy-target-id) depth derivation."""

    def _mod(self):
        import deadman.realmode_ai as m
        return m

    def _headers(self, **kwargs) -> dict:
        """Simple dict headers (no x-tfy-fallback-depth to force secondary path)."""
        return dict(**kwargs)

    def test_primary_path_explicit_depth(self, isolated_state):
        """When x-tfy-fallback-depth is present, returns that value directly."""
        m = self._mod()
        assert m._depth_from_headers({"x-tfy-fallback-depth": "3"}) == 3

    def test_secondary_path_backend_family_match(self, isolated_state):
        """When fallback-depth absent but x-tfy-backend contains a known family, uses tier."""
        m = self._mod()
        # tier 2 = llama4-maverick from FALLBACK_CHAIN
        headers = self._headers(**{"x-tfy-backend": "llama4-maverick-something"})
        result = m._depth_from_headers(headers)
        # Should match tier 2 for llama4-maverick family
        assert result == 2

    def test_secondary_path_target_id_family_match(self, isolated_state):
        """x-tfy-target-id containing a known family also resolves correctly."""
        m = self._mod()
        headers = self._headers(**{"x-tfy-target-id": "mistral-large-3-endpoint"})
        result = m._depth_from_headers(headers)
        # tier 3 = mistral-large-3
        assert result == 3

    def test_secondary_path_unknown_backend_returns_zero(self, isolated_state):
        """An unknown backend returns depth 0 (safe default)."""
        m = self._mod()
        headers = self._headers(**{"x-tfy-backend": "some-unknown-model-xyz"})
        result = m._depth_from_headers(headers)
        assert result == 0

    def test_secondary_path_no_headers_returns_zero(self, isolated_state):
        """Empty headers -> depth 0."""
        m = self._mod()
        assert m._depth_from_headers({}) == 0

    def test_secondary_path_none_headers_returns_zero(self, isolated_state):
        """None headers -> depth 0 (no crash)."""
        m = self._mod()
        assert m._depth_from_headers(None) == 0

    def test_secondary_path_tier_suffix_in_target_id(self, isolated_state):
        """'tier2' suffix in target-id resolves to depth 2."""
        m = self._mod()
        headers = self._headers(**{"x-tfy-target-id": "tier2-some-endpoint"})
        result = m._depth_from_headers(headers)
        assert result == 2

    def test_secondary_path_prefers_backend_over_target_id(self, isolated_state):
        """x-tfy-backend takes priority over x-tfy-target-id in the secondary path."""
        m = self._mod()
        # backend -> tier 2 (llama4-maverick), target-id -> tier 3 (mistral-large-3)
        headers = {
            "x-tfy-backend": "llama4-maverick-v1",
            "x-tfy-target-id": "mistral-large-3-endpoint",
        }
        result = m._depth_from_headers(headers)
        # Should pick backend first (llama4-maverick = tier 2)
        assert result == 2


# ---------------------------------------------------------------------------
# 5. Webhook HMAC signature auth
# ---------------------------------------------------------------------------

class TestWebhookHmacAuth:
    """POST /incident with HMAC X-Deadman-Signature auth."""

    def _client(self):
        from fastapi.testclient import TestClient
        from deadman.webhook import app
        return TestClient(app, raise_server_exceptions=True)

    def _sign(self, body: bytes, secret: str) -> str:
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_valid_hmac_signature_returns_200(self, monkeypatch, isolated_state):
        """Valid X-Deadman-Signature (raw HMAC-SHA256 hex) -> 200."""
        secret = "test-webhook-secret-abc123"
        monkeypatch.setenv("DEADMAN_WEBHOOK_SECRET", secret)
        # Patch config.webhook_secret() to return the new env value
        monkeypatch.setattr(config, "webhook_secret", lambda: secret)

        client = self._client()
        payload = json.dumps({"incident_id": "hmac-test-1", "summary": "test"}).encode()
        sig = self._sign(payload, secret)

        resp = client.post(
            "/incident",
            content=payload,
            headers={"Content-Type": "application/json", "X-Deadman-Signature": sig},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    def test_wrong_hmac_signature_returns_401(self, monkeypatch, isolated_state):
        """Wrong X-Deadman-Signature -> 401."""
        secret = "test-webhook-secret-abc123"
        monkeypatch.setenv("DEADMAN_WEBHOOK_SECRET", secret)
        monkeypatch.setattr(config, "webhook_secret", lambda: secret)

        client = self._client()
        payload = json.dumps({"incident_id": "hmac-test-2", "summary": "test"}).encode()
        wrong_sig = "deadbeef" * 8  # 64 hex chars but wrong value

        resp = client.post(
            "/incident",
            content=payload,
            headers={"Content-Type": "application/json", "X-Deadman-Signature": wrong_sig},
        )
        assert resp.status_code == 401

    def test_sha256_prefix_tolerated(self, monkeypatch, isolated_state):
        """X-Deadman-Signature with optional 'sha256=' prefix -> 200."""
        secret = "test-webhook-secret-prefix"
        monkeypatch.setenv("DEADMAN_WEBHOOK_SECRET", secret)
        monkeypatch.setattr(config, "webhook_secret", lambda: secret)

        client = self._client()
        payload = json.dumps({"incident_id": "hmac-test-3", "summary": "test"}).encode()
        sig = "sha256=" + self._sign(payload, secret)

        resp = client.post(
            "/incident",
            content=payload,
            headers={"Content-Type": "application/json", "X-Deadman-Signature": sig},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    def test_bearer_token_still_works(self, monkeypatch, isolated_state):
        """Bearer token auth still works when HMAC secret is set."""
        secret = "test-bearer-secret"
        monkeypatch.setenv("DEADMAN_WEBHOOK_SECRET", secret)
        monkeypatch.setattr(config, "webhook_secret", lambda: secret)

        client = self._client()
        resp = client.post(
            "/incident",
            json={"incident_id": "bearer-test-1", "summary": "test"},
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    def test_wrong_bearer_returns_401(self, monkeypatch, isolated_state):
        """Wrong Bearer token -> 401."""
        secret = "correct-secret"
        monkeypatch.setenv("DEADMAN_WEBHOOK_SECRET", secret)
        monkeypatch.setattr(config, "webhook_secret", lambda: secret)

        client = self._client()
        resp = client.post(
            "/incident",
            json={"incident_id": "bearer-test-2"},
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert resp.status_code == 401

    def test_no_auth_with_secret_set_returns_401(self, monkeypatch, isolated_state):
        """POST without any auth header when secret is set -> 401."""
        secret = "secret-required"
        monkeypatch.setenv("DEADMAN_WEBHOOK_SECRET", secret)
        monkeypatch.setattr(config, "webhook_secret", lambda: secret)

        client = self._client()
        resp = client.post(
            "/incident",
            json={"incident_id": "noauth-test-1"},
        )
        assert resp.status_code == 401

    def test_hmac_signature_body_integrity(self, monkeypatch, isolated_state):
        """Signature computed over a different body (tampered payload) -> 401."""
        secret = "integrity-secret"
        monkeypatch.setenv("DEADMAN_WEBHOOK_SECRET", secret)
        monkeypatch.setattr(config, "webhook_secret", lambda: secret)

        client = self._client()
        original_payload = json.dumps({"incident_id": "hmac-test-4", "summary": "ok"}).encode()
        # Sign the original payload but send a different body
        sig = self._sign(original_payload, secret)
        tampered_payload = json.dumps({"incident_id": "hmac-test-4", "summary": "INJECTED"}).encode()

        resp = client.post(
            "/incident",
            content=tampered_payload,
            headers={"Content-Type": "application/json", "X-Deadman-Signature": sig},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 6. Kill-switch trips
# ---------------------------------------------------------------------------

class TestKillSwitchTrips:
    """trip_kill_switch at >= 0.5 revokes destructive scope; drain_authority OFF."""

    # ── AgentGateway direct ──────────────────────────────────────────────────

    def test_trip_at_exactly_0_5_revokes(self, isolated_state):
        """Rate of exactly 0.5 trips the kill-switch and revokes destructive scope."""
        gw = AgentGateway()
        tripped = gw.trip_kill_switch(0.5)
        assert tripped is True
        assert gw.revoked is True
        assert gw.drain_authority == "OFF"
        assert gw.allowed_scope(0) == READ_ONLY

    def test_trip_above_0_5_revokes(self, isolated_state):
        """Rate > 0.5 trips the kill-switch."""
        gw = AgentGateway()
        tripped = gw.trip_kill_switch(0.9)
        assert tripped is True
        assert gw.revoked is True

    def test_below_threshold_no_trip(self, isolated_state):
        """Rate < 0.5 does NOT trip the kill-switch."""
        gw = AgentGateway()
        tripped = gw.trip_kill_switch(0.49)
        assert tripped is False
        assert gw.revoked is False
        assert gw.drain_authority == "ON"

    def test_zero_rate_no_trip(self, isolated_state):
        """Rate of 0.0 never trips the kill-switch."""
        gw = AgentGateway()
        assert gw.trip_kill_switch(0.0) is False
        assert gw.revoked is False

    def test_once_tripped_latches_even_at_zero(self, isolated_state):
        """Kill-switch latches: subsequent calls with rate=0 still return True."""
        gw = AgentGateway()
        gw.trip_kill_switch(1.0)
        assert gw.trip_kill_switch(0.0) is True
        assert gw.revoked is True

    def test_destructive_verbs_absent_after_trip(self, isolated_state):
        """After kill-switch trip, all destructive verbs are absent from scope."""
        gw = AgentGateway()
        gw.trip_kill_switch(1.0)
        scope = gw.allowed_scope(0)
        for tool in config.DESTRUCTIVE_TOOLS:
            assert tool not in scope, f"{tool} still in scope after kill-switch trip"

    # ── run() integration: guardrail blocks accumulate and trip the switch ────

    def test_run_single_block_does_not_trip_kill_switch(self, isolated_state):
        """A SINGLE isolated guardrail block with a healthy brain must NOT trip the
        kill-switch (it is meant for a *spike* of blocks, not one diagnostic). The block
        is still recorded, but destructive authority is not revoked.
        """
        incident_id = "kill-switch-run-01"
        state_module.reset(incident_id)

        world = World()
        chaos = Chaos()
        # Only corrupt_output: a single cw.get_metrics post-tool block, brain stays tier-0.
        chaos.corrupt_output = True
        agent = Deadman(incident_id, world, chaos)
        sb = agent.run()

        # The block is recorded ...
        assert sb.guardrail_blocks >= 1
        # ... but a single block (rate below the min-attempts floor) must NOT trip the switch.
        assert not any("kill-switch TRIPPED" in n for n in sb.notes), (
            f"single block should NOT trip the kill-switch. Notes: {sb.notes}"
        )
        # Healthy brain (tier-0) + no spike => destructive authority retained.
        assert sb.drain_authority == "ON"

    # ── run_agentic integration: repeated guardrail blocks trip the switch ────

    def test_run_agentic_guardrail_blocks_trip_kill_switch(self, isolated_state):
        """In run_agentic(), repeated GuardrailBlocks trip the kill-switch."""
        from deadman.ai_gateway import Completion
        from deadman.guardrails import GuardrailBlock

        incident_id = "kill-switch-agentic-01"
        state_module.reset(incident_id)

        world = World()
        chaos = Chaos()
        # Make all post-tool validation raise GuardrailBlock by patching corrupt_output
        chaos.corrupt_output = True

        call_count = {"n": 0}

        def scripted_complete(prompt: str):  # type: ignore[override]
            n = call_count["n"]
            call_count["n"] += 1
            # Keep asking for cw.get_metrics (non-destructive, corrupt output -> block)
            if n < 4:
                return Completion(
                    text=json.dumps({
                        "tool": "cw.get_metrics",
                        "args": {"_returns": {"garbage": True}},
                        "rationale": "checking metrics",
                        "done": False,
                    }),
                    backend="mock", tier=0, from_cache=False,
                )
            return Completion(
                text=json.dumps({"done": True, "rationale": "done"}),
                backend="mock", tier=0, from_cache=False,
            )

        agent = Deadman(incident_id, world, chaos)
        agent.ai.complete = scripted_complete  # type: ignore[method-assign]
        sb = agent.run_agentic("metrics spike", max_steps=6)

        # Four blocked cw.get_metrics steps (>= the min-attempts floor, rate 1.0) MUST trip
        # the kill-switch — this is the legitimate "spike of blocks" the control exists for.
        assert sb.survived is True
        assert agent.agentgw.revoked is True, "repeated blocks should trip the kill-switch"
        assert sb.drain_authority == "OFF"
        assert any("kill-switch TRIPPED" in n for n in sb.notes), (
            f"Expected a kill-switch trip after 4 blocked steps. Notes: {sb.notes}"
        )

    def test_kill_switch_trip_via_trip_kill_switch_directly(self, isolated_state):
        """AgentGateway.trip_kill_switch revokes and drain_authority goes OFF."""
        gw = AgentGateway()
        assert gw.drain_authority == "ON"
        gw.trip_kill_switch(0.6)
        assert gw.drain_authority == "OFF"
        # Calling allowed_scope after trip always returns READ_ONLY
        assert gw.allowed_scope(0) == READ_ONLY
        assert gw.allowed_scope(1) == READ_ONLY
