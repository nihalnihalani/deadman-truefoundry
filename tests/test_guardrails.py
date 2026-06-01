"""Tests 5 + 6: Pre-Tool and Post-Tool guardrails.

Tests 5: asg.scale below MIN_REPLICA_FLOOR raises GuardrailBlock; cordon_drain on
         prod-critical namespace without elevation blocked. Direct tests of guardrails.py.
Tests 6: Post-Tool corrupt/truncated output blocked.
"""
from __future__ import annotations
import pytest

import deadman.config as config
from deadman.guardrails import (
    GuardrailBlock,
    pre_tool_validate,
    post_tool_validate,
    PROD_CRITICAL_NAMESPACES,
)
from deadman.mcp_gateway import MCPGateway
from deadman.state import AuditLog
from deadman.world import World
from deadman.chaos import Chaos
import deadman.state as state_module


# ---------------------------------------------------------------------------
# Pre-Tool tests
# ---------------------------------------------------------------------------

class TestPreToolGuardrail:

    def test_asg_scale_below_floor_raises(self):
        """asg.scale replicas=0 raises GuardrailBlock (floor=2)."""
        with pytest.raises(GuardrailBlock, match="MIN_REPLICA_FLOOR"):
            pre_tool_validate("asg.scale", {"replicas": 0})

    def test_asg_scale_below_floor_explicit(self):
        """asg.scale replicas=1 raises GuardrailBlock when floor=2."""
        with pytest.raises(GuardrailBlock):
            pre_tool_validate("asg.scale", {"replicas": 1})

    def test_asg_scale_at_floor_passes(self):
        """asg.scale replicas=MIN_REPLICA_FLOOR passes the guardrail."""
        # Should not raise
        pre_tool_validate("asg.scale", {"replicas": config.MIN_REPLICA_FLOOR})

    def test_asg_scale_above_floor_passes(self):
        """asg.scale replicas=10 passes."""
        pre_tool_validate("asg.scale", {"replicas": 10})

    def test_cordon_drain_prod_namespace_no_elevation_raises(self):
        """k8s.cordon_drain on 'production' without elevation raises GuardrailBlock."""
        with pytest.raises(GuardrailBlock, match="elevation"):
            pre_tool_validate("k8s.cordon_drain", {"node": "n1", "namespace": "production"})

    def test_cordon_drain_prod_us_namespace_raises(self):
        """k8s.cordon_drain on 'prod-us' without elevation raises GuardrailBlock."""
        with pytest.raises(GuardrailBlock):
            pre_tool_validate("k8s.cordon_drain", {"node": "n1", "namespace": "prod-us"})

    def test_cordon_drain_with_elevation_passes(self):
        """k8s.cordon_drain on prod with elevation token passes."""
        pre_tool_validate(
            "k8s.cordon_drain",
            {"node": "n1", "namespace": "production", "elevation": "tok-xyz"},
        )

    def test_cordon_drain_non_prod_namespace_passes(self):
        """k8s.cordon_drain on a non-prod namespace passes without elevation."""
        pre_tool_validate("k8s.cordon_drain", {"node": "n1", "namespace": "staging"})

    def test_all_prod_critical_namespaces_blocked(self):
        """Every PROD_CRITICAL_NAMESPACES entry blocks cordon_drain without elevation."""
        for ns in PROD_CRITICAL_NAMESPACES:
            with pytest.raises(GuardrailBlock):
                pre_tool_validate("k8s.cordon_drain", {"node": "n1", "namespace": ns})

    def test_other_tool_not_affected(self):
        """A non-guarded tool passes pre_tool_validate without raising."""
        pre_tool_validate("cw.get_metrics", {"metric": "cpu"})
        pre_tool_validate("logs.query", {"query": "error"})

    def test_statuspage_post_redacts_common_secrets(self):
        """statuspage.post mutates its payload to remove common secret forms."""
        args = {
            "message": (
                "incident update token=abc123 "
                "Authorization: Bearer secret-token "
                "AKIA1234567890ABCDEF"
            ),
            "nested": {"api_key": "sk-live-123"},
        }

        pre_tool_validate("statuspage.post", args)

        assert "abc123" not in args["message"]
        assert "secret-token" not in args["message"]
        assert "AKIA1234567890ABCDEF" not in args["message"]
        assert args["nested"]["api_key"] == "[REDACTED]"

    # ---- integration: MCPGateway tracks guardrail_blocks count ----

    def test_mcp_gateway_tracks_asg_scale_block(self, isolated_state):
        """MCPGateway.guardrail_blocks increments when asg.scale fails pre-tool."""
        incident_id = "test-pretool-mcp"
        state_module.reset(incident_id)
        world = World()
        audit = AuditLog(incident_id)
        gw = MCPGateway(world, audit, Chaos())
        full_scope = {"asg.scale", "cw.get_metrics", "logs.query", "k8s.cordon_drain",
                      "github.revert_pr", "statuspage.post", "k8s.describe"}

        with pytest.raises(GuardrailBlock):
            gw.execute("asg.scale", {"asg": "my-asg", "replicas": 0}, "key-guard", full_scope)

        assert gw.guardrail_blocks == 1

    def test_mcp_gateway_cordon_prod_without_elevation_blocked(self, isolated_state):
        """MCPGateway blocks cordon_drain on prod-critical namespace without elevation."""
        incident_id = "test-pretool-cordon"
        state_module.reset(incident_id)
        world = World()
        audit = AuditLog(incident_id)
        gw = MCPGateway(world, audit, Chaos())
        full_scope = {"k8s.cordon_drain", "cw.get_metrics"}

        with pytest.raises(GuardrailBlock):
            gw.execute(
                "k8s.cordon_drain",
                {"node": "n1", "namespace": "prod-critical"},
                "key-cordon-prod",
                full_scope,
            )
        assert gw.guardrail_blocks == 1

    def test_side_effect_never_hits_world_when_pretool_blocks(self, isolated_state):
        """World.applied must stay empty when pre_tool_validate raises."""
        incident_id = "test-pretool-noworld"
        state_module.reset(incident_id)
        world = World()
        audit = AuditLog(incident_id)
        gw = MCPGateway(world, audit, Chaos())
        full_scope = {"asg.scale"}

        with pytest.raises(GuardrailBlock):
            gw.execute("asg.scale", {"asg": "my-asg", "replicas": 1}, "key-nw", full_scope)

        # asg_scale side effect must NOT have been applied to the world
        assert world.count("asg_scale") == 0
        assert world.applied == []


# ---------------------------------------------------------------------------
# Post-Tool tests
# ---------------------------------------------------------------------------

class TestPostToolGuardrail:

    def test_corrupt_flag_on_cw_raises(self):
        """corrupt=True on a cw.* tool raises GuardrailBlock."""
        with pytest.raises(GuardrailBlock, match="corrupt"):
            post_tool_validate("cw.get_metrics", {"cpu": 0.9}, corrupt=True)

    def test_corrupt_flag_on_logs_raises(self):
        """corrupt=True on logs.* tool raises GuardrailBlock."""
        with pytest.raises(GuardrailBlock):
            post_tool_validate("logs.query", '{"result": "ok"}', corrupt=True)

    def test_corrupt_flag_on_non_metrics_tool_does_not_block(self):
        """corrupt=True on github.revert_pr: the corrupt branch only fires for cw./logs.*.
        A non-metrics tool result must pass regardless of the corrupt flag or payload shape."""
        # Dict payload passes.
        result = post_tool_validate("github.revert_pr", {"status": "reverted"}, corrupt=True)
        assert result == {"status": "reverted"}

    def test_non_metrics_plaintext_string_passes(self):
        """A destructive/non-metrics tool returning a plain-text success body must PASS.

        Structural JSON validation is gated to cw./logs.* tools only; validating a
        non-metrics plain-text body would wrongly block AFTER the side effect ran but
        BEFORE the COMMIT — the exact PENDING-not-COMMITTED state that enables
        double-execution. So github.revert_pr returning 'Reverted PR #42' must pass."""
        result = post_tool_validate("github.revert_pr", "Reverted PR #42 successfully")
        assert result == "Reverted PR #42 successfully"
        # Even with corrupt=True (only meaningful for metrics/logs), it must pass.
        result2 = post_tool_validate("k8s.cordon_drain", "node n1 cordoned", corrupt=True)
        assert result2 == "node n1 cordoned"

    def test_invalid_json_string_on_metrics_raises(self):
        """A non-JSON string from a cw.* metrics tool raises GuardrailBlock."""
        with pytest.raises(GuardrailBlock):
            post_tool_validate("cw.get_metrics", "this is not json")

    def test_truncated_json_raises(self):
        """A JSON string with unbalanced braces (truncated) raises GuardrailBlock."""
        truncated = '{"cpu": 0.9, "mem": {'   # missing closing braces
        with pytest.raises(GuardrailBlock):
            post_tool_validate("cw.get_metrics", truncated)

    def test_valid_json_string_passes(self):
        """A valid JSON string passes post_tool_validate."""
        valid = '{"cpu": 0.9, "mem": 0.7}'
        result = post_tool_validate("cw.get_metrics", valid)
        assert result == valid

    def test_dict_result_passes(self):
        """A dict result (not a str) passes post_tool_validate regardless of corrupt flag."""
        result = post_tool_validate("cw.get_metrics", {"cpu": 0.9}, corrupt=False)
        assert result == {"cpu": 0.9}

    def test_none_result_passes(self):
        """None result is not a str, passes post_tool_validate."""
        result = post_tool_validate("github.revert_pr", None)
        assert result is None

    def test_corrupt_output_chaos_via_gateway(self, isolated_state):
        """chaos.corrupt_output=True triggers GuardrailBlock via the MCPGateway post-tool path."""
        incident_id = "test-post-chaos"
        state_module.reset(incident_id)
        world = World()
        audit = AuditLog(incident_id)
        chaos = Chaos()
        chaos.corrupt_output = True
        gw = MCPGateway(world, audit, chaos)
        full_scope = {"cw.get_metrics"}

        with pytest.raises(GuardrailBlock):
            gw.execute("cw.get_metrics", {"_returns": "garbage"}, "key-corrupt", full_scope)

        assert gw.guardrail_blocks == 1
