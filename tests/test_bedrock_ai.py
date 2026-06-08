"""Tests: direct-Bedrock AI client with a mocked boto3 transport (no live calls).

Unit-tests deadman/bedrock_ai.complete by monkeypatching boto3.client so no real AWS
call is made. Covers the happy path, tier fallback on per-tier failure, region client
caching, text extraction shape-tolerance, and total-outage behaviour.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

import deadman.config as config
import deadman.bedrock_ai as bedrock_ai
from deadman.bedrock_ai import BedrockOutage


@pytest.fixture(autouse=True)
def _clear_client_cache():
    """bedrock_ai memoises clients per region; reset between tests for isolation."""
    bedrock_ai._clients.clear()
    yield
    bedrock_ai._clients.clear()


def _converse_response(text: str) -> dict:
    """Shape of a successful bedrock-runtime Converse response."""
    return {"output": {"message": {"content": [{"text": text}]}}}


def _patch_boto3(monkeypatch, *, per_model=None, default=None):
    """Install a fake boto3.client('bedrock-runtime') whose .converse() is driven by
    `per_model` (modelId -> response|exception) with an optional `default` fallthrough.

    Returns the shared fake client so tests can assert on call counts.
    """
    per_model = per_model or {}
    client = MagicMock()

    def _converse(modelId, **kwargs):  # noqa: N803 — boto3 uses camelCase kwargs
        outcome = per_model.get(modelId, default)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise RuntimeError(f"no canned outcome for {modelId}")
        return outcome

    client.converse.side_effect = _converse

    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    return client


class TestBedrockComplete:

    def test_primary_tier_answers(self, monkeypatch):
        """Tier 0 succeeds -> depth 0, served_by names the tier-0 family/region."""
        chain = config.BEDROCK_FALLBACK_CHAIN
        top = chain[0]
        _patch_boto3(monkeypatch, per_model={top["model"]: _converse_response("mitigation plan")})

        result = bedrock_ai.complete("diagnose the outage")

        assert result["text"] == "mitigation plan"
        assert result["fallback_depth"] == 0
        assert result["from_cache"] is False
        assert result["served_by"] == f'{top["family"]}@{top["region"]}'
        assert "raw" in result

    def test_falls_back_to_second_tier(self, monkeypatch):
        """Tier 0 raises (e.g. AccessDenied) -> tier 1 answers, depth becomes 1."""
        chain = config.BEDROCK_FALLBACK_CHAIN
        _patch_boto3(
            monkeypatch,
            per_model={
                chain[0]["model"]: RuntimeError("AccessDeniedException"),
                chain[1]["model"]: _converse_response("fallback plan"),
            },
        )

        result = bedrock_ai.complete("diagnose")

        assert result["text"] == "fallback plan"
        assert result["fallback_depth"] == 1
        assert result["served_by"] == f'{chain[1]["family"]}@{chain[1]["region"]}'

    def test_skips_multiple_dead_tiers(self, monkeypatch):
        """First two tiers down -> depth climbs to the first healthy tier (2)."""
        chain = config.BEDROCK_FALLBACK_CHAIN
        _patch_boto3(
            monkeypatch,
            per_model={
                chain[0]["model"]: RuntimeError("throttled"),
                chain[1]["model"]: RuntimeError("throttled"),
                chain[2]["model"]: _converse_response("third-tier plan"),
            },
        )

        result = bedrock_ai.complete("diagnose")

        assert result["fallback_depth"] == 2
        assert result["text"] == "third-tier plan"

    def test_all_tiers_down_raises_outage(self, monkeypatch):
        """Every tier fails -> BedrockOutage, surfacing the last error."""
        _patch_boto3(monkeypatch, default=RuntimeError("ServiceUnavailableException"))

        with pytest.raises(BedrockOutage, match="ServiceUnavailableException"):
            bedrock_ai.complete("diagnose")

    def test_runtime_client_cached_per_region(self, monkeypatch):
        """boto3.client is created once per region and reused."""
        chain = config.BEDROCK_FALLBACK_CHAIN
        top = chain[0]
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")

        import boto3
        with patch.object(boto3, "client", return_value=client) as mk:
            bedrock_ai.complete("a")
            bedrock_ai.complete("b")
            # Both tier-0 calls hit us-east-1 -> only one client constructed for that region.
            regions = {kw.get("region_name") for _, kw in mk.call_args_list}
            assert top["region"] in regions
            # Second complete() reused the cached client, so no new construction for that region.
            calls_for_top_region = [
                1 for _, kw in mk.call_args_list if kw.get("region_name") == top["region"]
            ]
            assert sum(calls_for_top_region) == 1

    def test_empty_content_yields_empty_text(self, monkeypatch):
        """A response missing the content blocks degrades to empty text, not a crash."""
        chain = config.BEDROCK_FALLBACK_CHAIN
        _patch_boto3(
            monkeypatch,
            per_model={chain[0]["model"]: {"output": {"message": {}}}},
        )

        result = bedrock_ai.complete("diagnose")

        assert result["text"] == ""
        assert result["fallback_depth"] == 0

    def test_max_tokens_passed_through(self, monkeypatch):
        """The configured token cap reaches the Converse inferenceConfig."""
        chain = config.BEDROCK_FALLBACK_CHAIN
        monkeypatch.setattr(config, "BEDROCK_MAX_TOKENS", 256)
        client = _patch_boto3(
            monkeypatch, per_model={chain[0]["model"]: _converse_response("ok")}
        )

        bedrock_ai.complete("diagnose")

        _, kwargs = client.converse.call_args
        assert kwargs["inferenceConfig"]["maxTokens"] == 256
        assert kwargs["inferenceConfig"]["temperature"] == 0


class TestAIGatewayBedrockDispatch:
    """AIGateway.complete should route to bedrock_ai when LLM_BACKEND == 'bedrock'."""

    def test_real_mode_bedrock_backend_dispatch(self, monkeypatch):
        import deadman.ai_gateway as ai_gateway

        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "LLM_BACKEND", "bedrock")

        fake = {
            "text": "plan",
            "served_by": "claude-sonnet-4-6@us-east-1",
            "fallback_depth": 1,
            "from_cache": False,
            "raw": {},
        }
        monkeypatch.setattr(bedrock_ai, "complete", lambda prompt: fake)

        gw = ai_gateway.AIGateway()
        completion = gw.complete("diagnose")

        assert completion.text == "plan"
        assert completion.backend == "claude-sonnet-4-6@us-east-1"
        assert completion.tier == 1
        assert gw.max_depth == 1
        assert gw.fallbacks == 1
