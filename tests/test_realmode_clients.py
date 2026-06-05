"""Tests 11: Real-mode clients with mocked transports (no live calls).

Unit-tests deadman/realmode_ai.complete by monkeypatching the openai client.
Unit-tests deadman/realmode_mcp.call_tool by monkeypatching requests.post.
"""
from __future__ import annotations
import types
import pytest
from unittest.mock import MagicMock, patch, call

import deadman.config as config
import deadman.realmode_ai as realmode_ai
import deadman.realmode_mcp as realmode_mcp
from deadman.realmode_mcp import MCPGatewayError


# ---------------------------------------------------------------------------
# Helpers for mock openai objects
# ---------------------------------------------------------------------------

def _make_message(content: str = "plan text"):
    msg = MagicMock()
    msg.content = content
    return msg


def _make_choice(content: str = "plan text"):
    ch = MagicMock()
    ch.message = _make_message(content)
    return ch


def _make_completion_obj(model: str = "deadman-resilient-bedrock", content: str = "plan text"):
    comp = MagicMock()
    comp.model = model
    comp.choices = [_make_choice(content)]
    return comp


def _make_headers(overrides: dict | None = None) -> dict:
    """Returns a plain dict that _header() can look up."""
    base = {}
    if overrides:
        base.update(overrides)
    return base


def _make_raw_response(completion, headers: dict):
    raw = MagicMock()
    raw.parse.return_value = completion
    # Make headers a real dict-like object
    raw.headers = headers
    return raw


def _make_openai_client(raw_response):
    """Build a fake openai.OpenAI() with chat.completions.with_raw_response.create wired up."""
    client = MagicMock()
    client.chat.completions.with_raw_response.create.return_value = raw_response
    return client


# ---------------------------------------------------------------------------
# realmode_ai.complete — mocked transport
# ---------------------------------------------------------------------------

class TestRealmodeAI:

    def _patch_openai(self, monkeypatch, headers: dict, text: str = "response text",
                      model: str = "deadman-resilient-bedrock"):
        comp = _make_completion_obj(model=model, content=text)
        raw_resp = _make_raw_response(comp, headers)
        fake_client = _make_openai_client(raw_resp)

        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "test-key")
        monkeypatch.setattr(config, "TFY_GATEWAY_BASE_URL", "https://fake.gateway/")

        def _fake_openai_cls(**kwargs):
            return fake_client

        import openai
        monkeypatch.setattr(openai, "OpenAI", _fake_openai_cls)
        return fake_client

    def test_complete_returns_text(self, monkeypatch, isolated_state):
        """complete() extracts text from the choices[0].message.content."""
        self._patch_openai(monkeypatch, headers={}, text="my plan")
        result = realmode_ai.complete("diagnose")
        assert result["text"] == "my plan"

    def test_complete_fallback_depth_from_header(self, monkeypatch, isolated_state):
        """x-tfy-fallback-depth header is parsed correctly."""
        self._patch_openai(monkeypatch, headers={"x-tfy-fallback-depth": "2"})
        result = realmode_ai.complete("diagnose")
        assert result["fallback_depth"] == 2

    def test_complete_fallback_depth_zero_when_header_missing(self, monkeypatch, isolated_state):
        """Missing x-tfy-fallback-depth -> depth defaults to 0."""
        self._patch_openai(monkeypatch, headers={})
        result = realmode_ai.complete("diagnose")
        assert result["fallback_depth"] == 0

    def test_complete_cache_hit_detected(self, monkeypatch, isolated_state):
        """x-tfy-cache: hit -> from_cache=True and depth==SEMANTIC_CACHE_TIER."""
        self._patch_openai(monkeypatch, headers={"x-tfy-cache": "hit"})
        result = realmode_ai.complete("diagnose")
        assert result["from_cache"] is True
        assert result["fallback_depth"] == config.SEMANTIC_CACHE_TIER

    def test_complete_cache_miss(self, monkeypatch, isolated_state):
        """x-tfy-cache absent -> from_cache=False."""
        self._patch_openai(monkeypatch, headers={})
        result = realmode_ai.complete("diagnose")
        assert result["from_cache"] is False

    def test_complete_cache_hit_header_case_insensitive(self, monkeypatch, isolated_state):
        """x-tfy-cache: HIT (upper) still registers as a cache hit."""
        self._patch_openai(monkeypatch, headers={"x-tfy-cache": "HIT"})
        result = realmode_ai.complete("diagnose")
        assert result["from_cache"] is True

    def test_complete_served_by_from_header(self, monkeypatch, isolated_state):
        """x-tfy-backend header is returned as served_by."""
        self._patch_openai(
            monkeypatch,
            headers={"x-tfy-backend": "claude-opus-4-8@us-east-1", "x-tfy-fallback-depth": "0"},
        )
        result = realmode_ai.complete("diagnose")
        assert result["served_by"] == "claude-opus-4-8@us-east-1"

    def test_complete_served_by_fallback_to_model_field(self, monkeypatch, isolated_state):
        """When no x-tfy-backend header, served_by falls back to completion.model."""
        self._patch_openai(monkeypatch, headers={}, model="some-model-id")
        result = realmode_ai.complete("diagnose")
        assert result["served_by"] == "some-model-id"

    def test_complete_result_has_raw_key(self, monkeypatch, isolated_state):
        """complete() result dict includes 'raw' key with the completion object."""
        self._patch_openai(monkeypatch, headers={})
        result = realmode_ai.complete("diagnose")
        assert "raw" in result

    def test_complete_fallback_depth_1(self, monkeypatch, isolated_state):
        """x-tfy-fallback-depth: 1 sets fallback_depth=1 and from_cache=False."""
        self._patch_openai(monkeypatch, headers={"x-tfy-fallback-depth": "1"})
        result = realmode_ai.complete("plan")
        assert result["fallback_depth"] == 1
        assert result["from_cache"] is False


# ---------------------------------------------------------------------------
# realmode_mcp.call_tool — mocked requests.post
# ---------------------------------------------------------------------------

class TestRealmodeMCP:

    def _make_mock_response(self, status_code: int, json_body=None, text: str = ""):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        if json_body is not None:
            resp.json.return_value = json_body
        else:
            resp.json.side_effect = Exception("no json")
        return resp

    def test_200_success(self, monkeypatch, isolated_state):
        """200 response with JSON body -> status EXECUTED, skipped_idempotent=False."""
        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "key")
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://fake.mcp/")

        mock_resp = self._make_mock_response(200, json_body={"result": "ok"})
        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = realmode_mcp.call_tool("github.revert_pr", {"pr": "PR-1"}, "key-1")
            mock_post.assert_called_once()

        assert result["status_code"] == 200
        assert result["skipped_idempotent"] is False
        assert result["body"] == {"result": "ok"}

    def test_409_signals_idempotent(self, monkeypatch, isolated_state):
        """HTTP 409 -> skipped_idempotent=True (gateway deduplication signal)."""
        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "key")
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://fake.mcp/")

        mock_resp = self._make_mock_response(409, json_body={"message": "already done"})
        with patch("requests.post", return_value=mock_resp):
            result = realmode_mcp.call_tool("github.revert_pr", {"pr": "PR-1"}, "key-dup")

        assert result["skipped_idempotent"] is True
        assert result["status_code"] == 409

    def test_body_flag_signals_idempotent(self, monkeypatch, isolated_state):
        """200 with {'idempotent_replay': True} in body -> skipped_idempotent=True."""
        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "key")
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://fake.mcp/")

        mock_resp = self._make_mock_response(200, json_body={"idempotent_replay": True})
        with patch("requests.post", return_value=mock_resp):
            result = realmode_mcp.call_tool("github.revert_pr", {"pr": "PR-1"}, "key-replay")

        assert result["skipped_idempotent"] is True

    def test_500_then_200_retries(self, monkeypatch, isolated_state):
        """500 on first attempt -> retry -> 200 succeeds."""
        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "key")
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://fake.mcp/")

        resp_500 = self._make_mock_response(500, text="error")
        resp_200 = self._make_mock_response(200, json_body={"result": "ok"})

        with patch("requests.post", side_effect=[resp_500, resp_200]) as mock_post:
            # suppress actual sleep
            with patch("time.sleep"):
                result = realmode_mcp.call_tool("github.revert_pr", {"pr": "PR-1"}, "key-retry")
            assert mock_post.call_count == 2

        assert result["status_code"] == 200
        assert result["skipped_idempotent"] is False

    def test_exhausted_retries_raises_mcp_gateway_error(self, monkeypatch, isolated_state):
        """All retries return 500 -> raises MCPGatewayError."""
        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "key")
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://fake.mcp/")

        resp_500 = self._make_mock_response(500, text="server error")
        # _MAX_RETRIES = 2 means 3 total attempts (0, 1, 2)
        with patch("requests.post", return_value=resp_500):
            with patch("time.sleep"):
                with pytest.raises(MCPGatewayError, match="500"):
                    realmode_mcp.call_tool("github.revert_pr", {"pr": "PR-1"}, "key-fail")

    def test_non_retryable_4xx_raises_immediately(self, monkeypatch, isolated_state):
        """403 (non-retryable) -> raises MCPGatewayError immediately without retry."""
        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "key")
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://fake.mcp/")

        resp_403 = self._make_mock_response(403, text="forbidden")
        with patch("requests.post", return_value=resp_403) as mock_post:
            with pytest.raises(MCPGatewayError, match="403"):
                realmode_mcp.call_tool("github.revert_pr", {"pr": "PR-1"}, "key-403")
            # Should only be called once (no retry on 4xx)
            assert mock_post.call_count == 1

    def test_network_error_retries(self, monkeypatch, isolated_state):
        """Network-level exception retries up to _MAX_RETRIES then raises MCPGatewayError."""
        import requests as req_lib
        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "key")
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://fake.mcp/")

        network_err = req_lib.exceptions.ConnectionError("unreachable")
        with patch("requests.post", side_effect=network_err):
            with patch("time.sleep"):
                with pytest.raises(MCPGatewayError):
                    realmode_mcp.call_tool("github.revert_pr", {}, "key-net")

    def test_idempotency_key_in_request_headers(self, monkeypatch, isolated_state):
        """Idempotency-Key is passed as a request header."""
        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "key")
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://fake.mcp/")

        resp_200 = self._make_mock_response(200, json_body={"result": "ok"})
        with patch("requests.post", return_value=resp_200) as mock_post:
            realmode_mcp.call_tool("github.revert_pr", {}, "my-unique-key")
            _, kwargs = mock_post.call_args
            headers = kwargs.get("headers", {})
            assert headers.get("Idempotency-Key") == "my-unique-key"

    def test_tool_url_constructed_correctly(self, monkeypatch, isolated_state):
        """URL is constructed as base_url/tools/{tool}."""
        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "key")
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://mcp.example.com/")

        resp_200 = self._make_mock_response(200, json_body={})
        with patch("requests.post", return_value=resp_200) as mock_post:
            realmode_mcp.call_tool("asg.scale", {"replicas": 3}, "key-url")
            args, _ = mock_post.call_args
            assert args[0] == "https://mcp.example.com/tools/asg.scale"

    def test_auto_transport_uses_rest_for_legacy_base_url(self, monkeypatch, isolated_state):
        monkeypatch.setattr(config, "TFY_MCP_TRANSPORT", "auto")
        monkeypatch.setattr(config, "TFY_MCP_GATEWAY_URL", "https://mcp.example.com/")

        assert realmode_mcp.selected_transport() == "rest"

    def test_auto_transport_uses_mcp_for_tfy_server_url(self, monkeypatch, isolated_state):
        monkeypatch.setattr(config, "TFY_MCP_TRANSPORT", "auto")
        monkeypatch.setattr(
            config,
            "TFY_MCP_GATEWAY_URL",
            "https://gateway.truefoundry.ai/mcp/deadman/server",
        )

        assert realmode_mcp.selected_transport() == "mcp"

    def test_forced_transport_overrides_url_heuristic(self, monkeypatch, isolated_state):
        monkeypatch.setattr(config, "TFY_MCP_TRANSPORT", "rest")
        monkeypatch.setattr(
            config,
            "TFY_MCP_GATEWAY_URL",
            "https://gateway.truefoundry.ai/mcp/deadman/server",
        )

        assert realmode_mcp.selected_transport() == "rest"

    def test_mcp_transport_call_tool_uses_standard_path(self, monkeypatch, isolated_state):
        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "key")
        monkeypatch.setattr(config, "TFY_MCP_TRANSPORT", "mcp")
        monkeypatch.setattr(
            config,
            "TFY_MCP_GATEWAY_URL",
            "https://gateway.truefoundry.ai/mcp/deadman/server",
        )
        calls = []

        async def _fake_call_once(tool, args, key):
            calls.append((tool, args, key))
            return {
                "status_code": 200,
                "body": {"result": "ok"},
                "skipped_idempotent": False,
            }

        monkeypatch.setattr(realmode_mcp, "_call_tool_mcp_once", _fake_call_once)

        result = realmode_mcp.call_tool("cw.get_metrics", {"window": "5m"}, "read-key")

        assert result["body"] == {"result": "ok"}
        assert calls == [("cw.get_metrics", {"window": "5m"}, "read-key")]

    def test_mcp_transport_list_tools_normalizes_results(self, monkeypatch, isolated_state):
        monkeypatch.setattr(config, "MODE", "real")
        monkeypatch.setattr(config, "TFY_API_KEY", "key")
        monkeypatch.setattr(config, "TFY_MCP_TRANSPORT", "mcp")
        monkeypatch.setattr(
            config,
            "TFY_MCP_GATEWAY_URL",
            "https://gateway.truefoundry.ai/mcp/deadman/server",
        )

        async def _fake_list_once():
            return [{"name": "cw.get_metrics"}, {"name": "github.revert_pr"}]

        monkeypatch.setattr(realmode_mcp, "_list_tools_mcp_once", _fake_list_once)

        assert realmode_mcp.list_tools() == [
            {"name": "cw.get_metrics"},
            {"name": "github.revert_pr"},
        ]
